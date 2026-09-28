'use strict';
/**
 * 只读的本地服务：把 ~/.claude 的会话索引渲染成一个网页。
 *
 * 关于安全 —— 这个服务刻意「没有」以下能力：
 *   - 只认 GET / HEAD，其余方法一律 405，没有任何写路由
 *   - 不 import child_process，不调用外部命令
 *   - 不接受浏览器传来的任何路径（所有路径都出自 scanner 的扫描结果）
 *   - 只监听 127.0.0.1，不暴露到局域网
 * 所以即使有人对着这个端口乱发请求，也删不掉任何东西。
 * 「删除命令」只是拼出来给人看的一段文本，执行与否永远由你在终端决定。
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { scanAll, readFullTurns, PROJECTS_DIR } = require('./scanner');

const PORT = Number(process.env.PORT) || 8756;
const PUBLIC_DIR = path.join(__dirname, 'public');
const CACHE_TTL_MS = 30 * 1000;

let cache = null;
let inflight = null;

/** 30 秒内复用缓存；force 为真时强扫（界面上的「重新扫描」） */
async function getIndex(force) {
  const fresh = cache && Date.now() - cache._at < CACHE_TTL_MS;
  if (!force && fresh) return cache;
  if (!inflight) {
    inflight = scanAll()
      .then((data) => {
        data._at = Date.now();
        cache = data;
        return data;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

function send(res, code, body, type) {
  res.writeHead(code, {
    'Content-Type': type,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  let url;
  try {
    url = new URL(req.url, `http://localhost:${PORT}`);
  } catch {
    return send(res, 400, 'Bad request', 'text/plain; charset=utf-8');
  }

  if (req.method !== 'GET' && req.method !== 'HEAD') {
    return send(res, 405, '这个服务是只读的，不接受任何写操作。', 'text/plain; charset=utf-8');
  }

  if (url.pathname === '/api/sessions') {
    try {
      const data = await getIndex(url.searchParams.has('refresh'));
      return send(res, 200, JSON.stringify(data), 'application/json; charset=utf-8');
    } catch (err) {
      return send(res, 500, JSON.stringify({ error: err.message }), 'application/json; charset=utf-8');
    }
  }

  // 「展开完整对话」时才走这里。只读：按 id 从扫描结果里找到文件，再读它。
  if (url.pathname === '/api/session-turns') {
    const sid = url.searchParams.get('id') || '';
    if (!sid) {
      return send(res, 400, JSON.stringify({ error: '缺少 id' }), 'application/json; charset=utf-8');
    }
    try {
      const data = await getIndex(false);
      let sess = null;
      for (const w of data.workspaces) {
        sess = w.sessions.find((s) => s.id === sid);
        if (sess) break;
      }
      if (!sess) {
        return send(res, 404, JSON.stringify({ error: '没找到这个会话' }), 'application/json; charset=utf-8');
      }
      const turns = await readFullTurns(sess.paths.transcript);
      return send(res, 200, JSON.stringify({ id: sid, turns }), 'application/json; charset=utf-8');
    } catch (err) {
      return send(res, 500, JSON.stringify({ error: err.message }), 'application/json; charset=utf-8');
    }
  }

  if (url.pathname === '/' || url.pathname === '/index.html') {
    try {
      return send(res, 200, fs.readFileSync(path.join(PUBLIC_DIR, 'index.html')), 'text/html; charset=utf-8');
    } catch (err) {
      return send(res, 500, '读不到 public/index.html：' + err.message, 'text/plain; charset=utf-8');
    }
  }

  send(res, 404, '没有这个页面', 'text/plain; charset=utf-8');
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('');
  console.log('  Claude 会话管理已启动');
  console.log('  →  http://localhost:' + PORT);
  console.log('');
  console.log('  数据目录：' + PROJECTS_DIR);
  console.log('  这个服务只能读，不能删。关闭本窗口即停止。');
  console.log('');
});

server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`端口 ${PORT} 被占用了。换个端口再试：set PORT=8757 && node app.js`);
  } else {
    console.error(err);
  }
  process.exit(1);
});
