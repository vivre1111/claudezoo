'use strict';
/**
 * 只读扫描 ~/.claude/projects 下的会话索引。
 *
 * 本模块只做三件事：打开文件、读、关文件。
 * 不含任何写入 / 删除 / 重命名逻辑，也不 import 能执行系统命令的东西。
 *
 * 性能取舍：全部 123 个 jsonl 加起来 ~835 MB，逐行全量解析要几十秒。
 * 但标题（ai-title / custom-title）出现在文件前几行，最近对话和最后时间戳
 * 在文件尾部，所以每个文件只读「头 64 KB + 尾 192 KB」两段就够，
 * 整体扫描降到 1~2 秒。
 */

const fsp = require('fs').promises;
const path = require('path');
const os = require('os');

const CLAUDE_DIR = path.join(os.homedir(), '.claude');
const PROJECTS_DIR = path.join(CLAUDE_DIR, 'projects');

const HEAD_BYTES = 64 * 1024;
const TAIL_INIT_BYTES = 192 * 1024;  // 尾段起始窗口
const TAIL_MAX_BYTES = 8 * 1024 * 1024; // 不够用时的扩展上限
// 两种长度：展开行时先只给「我最近说的」，短平快；点开完整对话才用长的。
const USER_TEXT_MAX = 220;  // 「我最近说的」每条上限
const TURN_TEXT_MAX = 800;  // 完整对话里每条上限

const UNKNOWN_CWD = '(未知目录)'; // 极少数会话读不到 cwd，归到这一组

// 展示策略：assistant 的消息往往又长又碎（每步工具调用之间都要说一句），
// 一段尾部切片很容易被它们占满 —— 结果界面上只看得到 Claude 说了什么、
// 看不到用户说了什么。所以要求最近这几条里至少有 USER_QUOTA 条用户消息，
// 不够就把读取窗口往前扩。
const USER_QUOTA = 3;       // 展开行时显示我自己最近说的几条
const MIN_TURNS = 6;
const MAX_TURNS = 12;       // 完整对话最多给这么多条

const CONCURRENCY = 16; // 同时打开的文件数，别把 fd 用光

const FULL_TURNS = 100;                  // 「展开完整对话」时给多少条
const FULL_READ_MAX = 64 * 1024 * 1024;  // 按需读取时最多往回翻多少字节

// ---------------------------------------------------------------- 文本处理

// 这些是 Claude Code 塞进消息里的内部标记，不该出现在摘要里
const NOISE = [
  /<system-reminder>[\s\S]*?<\/system-reminder>/g,
  // 跑本地命令时 Claude Code 会塞这么一段英文告诫进来。不滤掉的话，
  // 没有 ai-title 的会话就会拿它当标题显示，整行都是一句内部提示。
  /<local-command-caveat>[\s\S]*?<\/local-command-caveat>/g,
  /<local-command-stdout>[\s\S]*?<\/local-command-stdout>/g,
  /<local-command-stderr>[\s\S]*?<\/local-command-stderr>/g,
  /<command-name>[\s\S]*?<\/command-name>/g,
  /<command-message>[\s\S]*?<\/command-message>/g,
  /<command-args>[\s\S]*?<\/command-args>/g,
  /<user-prompt-submit-hook>[\s\S]*?<\/user-prompt-submit-hook>/g,
  // 后台任务完成时注入的通知。它挂在 user 角色下，不滤掉的话会在
  // 「我最近说的」里冒充一句你说的话，整条都是文件路径。
  /<task-notification>[\s\S]*?<\/task-notification>/g,
];

function cleanText(s) {
  let t = String(s);
  for (const re of NOISE) t = t.replace(re, ' ');
  return t.replace(/\s+/g, ' ').trim();
}

function truncate(s, n = MAX_TEXT) {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

/**
 * 从一条 message 里取纯文本。
 * content 可能是字符串，也可能是 block 数组；数组里只认 text，
 * tool_use / tool_result / thinking 一律不进摘要（否则界面全是噪音）。
 */
function textOf(message) {
  if (!message) return '';
  const c = message.content;
  if (typeof c === 'string') return cleanText(c);
  if (!Array.isArray(c)) return '';
  const parts = [];
  for (const b of c) {
    if (b && typeof b === 'object' && b.type === 'text' && typeof b.text === 'string') {
      parts.push(b.text);
    }
  }
  return cleanText(parts.join('\n'));
}

/** 把一条记录转成对话摘要；不是可见对话就返回 null */
function turnOf(rec, limit = TURN_TEXT_MAX) {
  if (!rec || rec.isSidechain) return null;          // 子 agent 的对话不算
  if (rec.type !== 'user' && rec.type !== 'assistant') return null;
  const text = textOf(rec.message);
  if (!text) return null;                             // 纯 tool_result 会被这里滤掉
  return { role: rec.type, text: truncate(text, limit), ts: rec.timestamp || null };
}

// ---------------------------------------------------------------- 文件读取

async function readAt(fh, position, length) {
  const buf = Buffer.alloc(length);
  const { bytesRead } = await fh.read(buf, 0, length, position);
  return buf.subarray(0, bytesRead).toString('utf8');
}

/**
 * 切行。头段要丢掉最后一行、尾段要丢掉第一行 —— 因为截断处那一行是残的。
 * 注意：多字节字符也可能被切断，但残行反正会被丢掉，不影响结果。
 */
function linesOf(text, { dropFirst = false, dropLast = false } = {}) {
  let lines = text.split('\n');
  if (dropFirst) lines = lines.slice(1);
  if (dropLast) lines = lines.slice(0, -1);
  return lines;
}

function parseJsonLines(lines) {
  const out = [];
  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;
    try {
      out.push(JSON.parse(s));
    } catch {
      /* 残行 / 非法行，跳过 */
    }
  }
  return out;
}

/**
 * 从尾部往回读一个窗口；窗口里可展示的对话太少、或者一条用户消息都没有，
 * 就把窗口放大四倍重读，直到够用或到达上限。
 */
async function readTailRecords(fh, size, headLen, headRecords) {
  if (size <= headLen) return headRecords;

  let window = TAIL_INIT_BYTES;
  for (;;) {
    const start = Math.max(headLen, size - window);
    const text = await readAt(fh, start, size - start);
    const records = parseJsonLines(linesOf(text, { dropFirst: start > 0 }));

    const turns = collectTurns(records);
    const users = turns.filter((t) => t.role === 'user').length;
    const enough = turns.length >= MIN_TURNS && users >= USER_QUOTA;

    if (enough || start === headLen || window >= TAIL_MAX_BYTES) return records;
    window *= 4;
  }
}

/**
 * 按需读取一个会话的最近若干条对话 —— 界面上点「展开完整对话」时才调。
 *
 * 和扫描时一样从尾部往回扩窗，攒够就收手，不整个文件全解析：几百 MB 的
 * 会话有的一整个 68 MB，全读一遍要好几秒，而通常最近的 100 条躲在最后
 * 几 MB 里就够了。窗口上限放宽到 64 MB，真不够才一路读到文件开头。
 */
async function readFullTurns(transcriptPath, want = FULL_TURNS) {
  const fh = await fsp.open(transcriptPath, 'r');
  try {
    const { size } = await fh.stat();
    const headLen = Math.min(HEAD_BYTES, size);

    let window = TAIL_INIT_BYTES;
    for (;;) {
      const start = Math.max(headLen, size - window);
      const text = await readAt(fh, start, size - start);
      const records = parseJsonLines(linesOf(text, { dropFirst: start > 0 }));
      // 这里刻意不用 pickRecentTurns —— 那个是给扫描时的短摘要用的，
      // 会把结果卡在 MAX_TURNS 条，攒到 100 永远攒不满。
      const turns = collectTurns(records, TURN_TEXT_MAX);

      if (turns.length >= want || start === headLen || window >= FULL_READ_MAX) {
        return turns.slice(-want);
      }
      window *= 4;
    }
  } finally {
    await fh.close();
  }
}

// ---------------------------------------------------------------- 字段提取

function extractCwd(records) {
  for (const r of records) {
    if (typeof r.cwd === 'string' && r.cwd) return r.cwd;
  }
  return null;
}

/** custom-title 是用户手动改的名字，优先级高于自动生成的 ai-title */
function extractTitle(head, tail) {
  for (const r of [...tail, ...head]) {
    if (r.type === 'custom-title' && r.customTitle) {
      return { title: r.customTitle, custom: true };
    }
  }
  for (const r of [...head, ...tail]) {
    if (r.type === 'ai-title' && r.aiTitle) {
      return { title: r.aiTitle, custom: false };
    }
  }
  return null;
}

function extractTimestamps(...recordSets) {
  let min = null;
  let max = null;
  for (const records of recordSets) {
    for (const r of records) {
      const t = r && r.timestamp;
      if (typeof t !== 'string') continue;
      if (!min || t < min) min = t;
      if (!max || t > max) max = t;
    }
  }
  return { min, max };
}

function collectTurns(records, limit = TURN_TEXT_MAX) {
  const turns = [];
  for (const r of records) {
    const turn = turnOf(r, limit);
    if (turn) turns.push(turn);
  }
  return turns;
}

/** 从后往前取最近几条，但保证里面包含用户的发言（见文件顶部 WANT_USERS 的说明） */
function pickRecentTurns(turns) {
  if (!turns.length) return turns;
  let start = Math.max(0, turns.length - MIN_TURNS);
  let users = 0;
  for (let i = start; i < turns.length; i++) if (turns[i].role === 'user') users++;
  while (users < USER_QUOTA && start > 0 && turns.length - start < MAX_TURNS) {
    start--;
    if (turns[start].role === 'user') users++;
  }
  return turns.slice(start);
}

// ---------------------------------------------------------------- 单会话扫描

async function pathExists(p) {
  try {
    await fsp.stat(p);
    return true;
  } catch {
    return false;
  }
}

async function scanSession(projectDir, fileName) {
  const file = path.join(projectDir, fileName);
  const id = fileName.replace(/\.jsonl$/, '');

  const fh = await fsp.open(file, 'r');
  let size;
  let mtime;
  let headRecords;
  let tailRecords;
  try {
    const st = await fh.stat();
    size = st.size;
    mtime = st.mtime.toISOString();

    const headLen = Math.min(HEAD_BYTES, size);
    const headText = await readAt(fh, 0, headLen);
    headRecords = parseJsonLines(linesOf(headText, { dropLast: size > headLen }));

    tailRecords = await readTailRecords(fh, size, headLen, headRecords);
  } finally {
    await fh.close();
  }

  const title = extractTitle(headRecords, tailRecords);
  const { min, max } = extractTimestamps(headRecords, tailRecords);

  // 完整对话（要再点一下才看）：最近若干条，每条给足长度
  let turns = pickRecentTurns(collectTurns(tailRecords, TURN_TEXT_MAX));
  if (!turns.length) turns = pickRecentTurns(collectTurns(headRecords, TURN_TEXT_MAX));

  // 「我最近说的」：展开一行的第一眼只看这个。判断一个会话在干什么，看自己
  // 说过什么远比看 Claude 那些又长又碎的回复有用，所以只取用户消息、只留
  // 最近几条、每条截得短。
  let recentUser = collectTurns(tailRecords, USER_TEXT_MAX)
    .filter((t) => t.role === 'user')
    .slice(-USER_QUOTA);
  if (!recentUser.length) {
    recentUser = collectTurns(headRecords, USER_TEXT_MAX)
      .filter((t) => t.role === 'user')
      .slice(-USER_QUOTA);
  }

  // 首条用户消息 —— 标题缺失时拿它当兜底显示
  let firstUser = null;
  for (const r of headRecords) {
    if (r.type === 'user' && !r.isSidechain) {
      const t = textOf(r.message);
      if (t) { firstUser = truncate(t, 120); break; }
    }
  }

  // 这个 session 在磁盘上牵连到的其它位置。
  // 注意：projects/<项目>/memory/ 是「项目级」的，不属于任何单个会话，
  // 所以刻意不出现在这里 —— 生成删除命令时不会碰它。
  const extras = {
    subagents: await pathExists(path.join(projectDir, id)),
    fileHistory: await pathExists(path.join(CLAUDE_DIR, 'file-history', id)),
    tasks: await pathExists(path.join(CLAUDE_DIR, 'tasks', id)),
    sessionEnv: await pathExists(path.join(CLAUDE_DIR, 'session-env', id)),
  };

  return {
    id,
    cwd: extractCwd(headRecords),
    title: title ? title.title : firstUser || (size < 2048 ? '(空会话)' : '(无标题)'),
    titleIsCustom: title ? title.custom : false,
    createdAt: min,
    updatedAt: max || mtime,
    bytes: size,
    recentUser,
    turns,
    extras,
    paths: {
      transcript: file,
      subagents: path.join(projectDir, id),
      fileHistory: path.join(CLAUDE_DIR, 'file-history', id),
      tasks: path.join(CLAUDE_DIR, 'tasks', id),
      sessionEnv: path.join(CLAUDE_DIR, 'session-env', id),
    },
  };
}

// ---------------------------------------------------------------- 汇总扫描

async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const idx = cursor++;
      results[idx] = await fn(items[idx], idx);
    }
  });
  await Promise.all(workers);
  return results;
}

async function scanAll() {
  let projectDirs;
  try {
    const entries = await fsp.readdir(PROJECTS_DIR, { withFileTypes: true });
    projectDirs = entries.filter((e) => e.isDirectory()).map((e) => e.name);
  } catch (err) {
    throw new Error(`读不到目录 ${PROJECTS_DIR}：${err.message}`);
  }

  // 1. 先把所有 jsonl 都找出来
  const targets = [];
  for (const dirName of projectDirs) {
    const projectDir = path.join(PROJECTS_DIR, dirName);
    let names;
    try {
      const entries = await fsp.readdir(projectDir, { withFileTypes: true });
      names = entries.filter((e) => e.isFile() && e.name.endsWith('.jsonl')).map((e) => e.name);
    } catch {
      continue;
    }
    for (const name of names) targets.push({ dirName, projectDir, name });
  }

  // 2. 并发扫（每个文件只读头尾两段，见文件顶部说明）
  const sessions = await mapLimit(targets, CONCURRENCY, async ({ dirName, projectDir, name }) => {
    let s;
    try {
      s = await scanSession(projectDir, name);
    } catch (err) {
      s = {
        id: name.replace(/\.jsonl$/, ''),
        cwd: null,
        title: `(读取失败：${err.message})`,
        createdAt: null,
        updatedAt: new Date(0).toISOString(),
        bytes: 0,
        turns: [],
        extras: {},
        paths: { transcript: path.join(projectDir, name) },
        broken: true,
      };
    }
    s.sourceDir = dirName;
    return s;
  });

  // 3. 按每条会话自己记的 cwd 分组。
  //
  //    这里不能用 ~/.claude/projects/ 下的目录名分组：那个名字是「不可逆转义」的
  //    （非 ASCII 字符一律压成 -），实测两头都会错——
  //      · 同一个 folder 散在多个目录里：…\workspace\电机在线监测 的 31 个会话
  //        被拆成 30 + 1 两个目录；D:\程序\rotor-monitor 同样被拆开
  //      · 一个目录里混着多个 folder：D------ 里同时装着 D:\红外测温、D:\识别统计、
  //        D:\学习材料（都是 4 个中文字，压出来一个样）
  //    所以目录名只当「文件放在哪」用，分组一律看 cwd。
  const groups = new Map();
  for (const s of sessions) {
    const key = s.cwd || UNKNOWN_CWD;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }

  const workspaces = [];
  for (const [cwd, list] of groups) {
    list.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));

    const dirs = [...new Set(list.map((s) => s.sourceDir))];

    // memory/ 挂在转义目录下（不是会话级）：这个 folder 沾到的任一目录里有，就算有
    let hasMemory = false;
    for (const d of dirs) {
      if (await pathExists(path.join(PROJECTS_DIR, d, 'memory'))) {
        hasMemory = true;
        break;
      }
    }

    workspaces.push({
      key: cwd,
      cwd,
      dirs,
      hasMemory,
      bytes: list.reduce((n, s) => n + s.bytes, 0),
      updatedAt: list[0] ? list[0].updatedAt : null,
      sessions: list,
    });
  }

  workspaces.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));

  return {
    projectsDir: PROJECTS_DIR,
    claudeDir: CLAUDE_DIR,
    workspaces,
    totals: {
      workspaces: workspaces.length,
      sessions: workspaces.reduce((n, w) => n + w.sessions.length, 0),
      bytes: workspaces.reduce((n, w) => n + w.bytes, 0),
    },
  };
}

module.exports = { scanAll, scanSession, readFullTurns, CLAUDE_DIR, PROJECTS_DIR };
