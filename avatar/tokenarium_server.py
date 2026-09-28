# -*- coding: utf-8 -*-
"""Tokenarium local server.

Turns local Codex thread metadata into a small, privacy-conscious habitat.
It never contacts the network, never reads auth.json, and opens Codex SQLite
databases in read-only mode.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "tokenarium.html"
DEFAULT_PORT = 8788
PORT = DEFAULT_PORT
RECENT_SECONDS = 72 * 3600
STALE_ACTIVE_SECONDS = 20 * 60
HELPER_RECENT_SECONDS = 30 * 60
UI = {"want": "max"}


MOVIES = [
    {"title": "完美的日子", "en": "Perfect Days", "year": 2023,
     "tags": ["quiet", "waiting"], "note": "适合任务都收尾之后，看日常如何重新长出光。"},
    {"title": "火星救援", "en": "The Martian", "year": 2015,
     "tags": ["working", "thinking"], "note": "把大问题拆成下一件能做的小事。工程师的舒适电影。"},
    {"title": "阿波罗13号", "en": "Apollo 13", "year": 1995,
     "tags": ["working", "error"], "note": "约束很多、时间很少，但协作和冷静仍然有用。"},
    {"title": "她", "en": "Her", "year": 2013,
     "tags": ["thinking", "waiting"], "note": "关于人与操作系统，也关于亲密关系里那些无法编译的部分。"},
    {"title": "银翼杀手2049", "en": "Blade Runner 2049", "year": 2017,
     "tags": ["thinking", "sleeping"], "note": "霓虹、记忆与身份；很适合这座深夜温室。"},
    {"title": "月球", "en": "Moon", "year": 2009,
     "tags": ["thinking", "paused"], "note": "一座基地、一个人，以及逐渐不对劲的自我认识。"},
    {"title": "机械姬", "en": "Ex Machina", "year": 2014,
     "tags": ["thinking", "error"], "note": "界面很干净，问题很危险：你究竟在评估谁？"},
    {"title": "降临", "en": "Arrival", "year": 2016,
     "tags": ["thinking", "waiting"], "note": "理解一种新语言，也会改变理解时间和选择的方式。"},
    {"title": "社交网络", "en": "The Social Network", "year": 2010,
     "tags": ["working"], "note": "高速编码与缓慢崩坏的人际关系形成了锋利反差。"},
    {"title": "点球成金", "en": "Moneyball", "year": 2011,
     "tags": ["working", "paused"], "note": "当旧经验不够用，就换一种方式衡量问题。"},
    {"title": "战争游戏", "en": "WarGames", "year": 1983,
     "tags": ["error", "thinking"], "note": "轻快的冷战技术寓言，也提醒我们别把游戏和现实混在一起。"},
    {"title": "窃听风暴", "en": "The Lives of Others", "year": 2006,
     "tags": ["quiet", "thinking"], "note": "观看别人生活的边界，正适合一款坚持隐私的本地仪表盘。"},
    {"title": "千钧一发", "en": "Gattaca", "year": 1997,
     "tags": ["working", "paused"], "note": "不是所有限制都能定义一个人最终能走多远。"},
    {"title": "机器人总动员", "en": "WALL·E", "year": 2008,
     "tags": ["quiet", "sleeping"], "note": "安静、温柔，机器也可以有自己的小收藏和小坚持。"},
    {"title": "大都会", "en": "Metropolis", "year": 1927,
     "tags": ["working", "error"], "note": "机器城市的祖先级想象，画面至今仍像来自未来。"},
    {"title": "东京物语", "en": "Tokyo Story", "year": 1953,
     "tags": ["quiet", "sleeping"], "note": "当所有通知都安静下来，才听见时间真正经过的声音。"},
    {"title": "暖暖内含光", "en": "Eternal Sunshine of the Spotless Mind", "year": 2004,
     "tags": ["paused", "thinking"], "note": "如果记忆可以删掉，留下的选择还属于你吗？"},
    {"title": "楚门的世界", "en": "The Truman Show", "year": 1998,
     "tags": ["waiting", "thinking"], "note": "当世界像一套界面，真正重要的是找到它的出口。"},
    {"title": "布达佩斯大饭店", "en": "The Grand Budapest Hotel", "year": 2014,
     "tags": ["waiting", "quiet"], "note": "秩序、色彩和一点荒诞，适合完成一轮工作后的晚上。"},
    {"title": "龙猫", "en": "My Neighbor Totoro", "year": 1988,
     "tags": ["quiet", "sleeping"], "note": "森林里并不是没有事情发生，只是发生得很轻。"},
    {"title": "黑客帝国", "en": "The Matrix", "year": 1999,
     "tags": ["working", "thinking"], "note": "代码、选择与现实层级。每隔几年重看都会看到不同的问题。"},
    {"title": "十二怒汉", "en": "12 Angry Men", "year": 1957,
     "tags": ["thinking", "paused"], "note": "推理不是更大声，而是愿意重新检查最初的假设。"},
    {"title": "这个男人来自地球", "en": "The Man from Earth", "year": 2007,
     "tags": ["quiet", "thinking"], "note": "几个人、一间屋子、一个假设，就足够撑起整个宇宙。"},
    {"title": "帕特森", "en": "Paterson", "year": 2016,
     "tags": ["quiet", "waiting"], "note": "重复的日子并不重复，细微观察本身就是一种创作。"},
]


PET_SPECIES = [
    ("🦊", "赤狐"), ("🦉", "夜枭"), ("🦦", "水獭"), ("🐈", "夜猫"),
    ("🦎", "壁虎"), ("🐙", "章鱼"), ("🦔", "刺猬"), ("🐇", "月兔"),
    ("🦝", "浣熊"), ("🐢", "陆龟"), ("🦜", "鹦鹉"), ("🐼", "熊猫"),
]


ACTION_LABELS = {
    "reasoning": ("thinking", "在温室里推演"),
    "commandExecution": ("working", "摆弄终端机关"),
    "fileChange": ("working", "修剪代码枝叶"),
    "mcpToolCall": ("working", "借用外部工具"),
    "webSearch": ("working", "翻阅远方资料"),
    "agentMessage": ("thinking", "整理要说的话"),
    "collabAgentToolCall": ("working", "照看协作伙伴"),
    "imageGeneration": ("working", "培育一幅图像"),
}


@dataclass
class StorePaths:
    root: Path
    state: Path | None
    history: Path | None


def _versioned_file(root: Path, prefix: str) -> Path | None:
    candidates = list(root.glob(f"{prefix}_*.sqlite"))
    if not candidates:
        return None

    def key(path: Path) -> tuple[int, int]:
        hit = re.search(r"_(\d+)\.sqlite$", path.name)
        return (int(hit.group(1)) if hit else -1, path.stat().st_mtime_ns)

    return max(candidates, key=key)


def discover_store(root: str | os.PathLike[str] | None = None) -> StorePaths:
    base = Path(root or os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return StorePaths(base, _versioned_file(base, "state"),
                      _versioned_file(base, "thread_history"))


def _connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}


def _select_expr(columns: set[str], name: str, fallback: str = "NULL") -> str:
    return f'"{name}"' if name in columns else f"{fallback} AS \"{name}\""


def _strip_extended_path(value: str | None) -> str:
    value = value or ""
    return value[4:] if value.startswith("\\\\?\\") else value


def _safe_timestamp(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n > 10_000_000_000:
        n /= 1000
    return n


def _user_text(item: dict[str, Any]) -> str:
    """Extract only the human-written part of a local userMessage item."""
    parts: list[str] = []
    for block in item.get("content") or []:
        if not isinstance(block, dict) or block.get("type") not in ("text", "input_text"):
            continue
        value = block.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value)
    text = "\n".join(parts).strip()
    marker = "## My request:"
    if marker in text:
        text = text.rsplit(marker, 1)[1].strip()
    return " ".join(text.split())[:360]


def _load_threads(paths: StorePaths) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    if not paths.state or not paths.state.exists():
        return [], []
    con = _connect_ro(paths.state)
    try:
        cols = _columns(con, "threads")
        wanted = [
            "id", "cwd", "title", "name", "preview", "created_at", "updated_at",
            "recency_at", "tokens_used", "model", "reasoning_effort", "thread_source",
            "source", "agent_nickname", "agent_path", "is_pinned", "archived",
            "git_branch", "project_id",
        ]
        select = ", ".join(_select_expr(cols, name) for name in wanted)
        rows = [dict(row) for row in con.execute(f"SELECT {select} FROM threads")]
        edges: list[tuple[str, str]] = []
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "thread_spawn_edges" in tables:
            edges = [(row[0], row[1]) for row in con.execute(
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges")]
        return rows, edges
    finally:
        con.close()


def _load_history(paths: StorePaths) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[int]]]:
    turns: dict[str, dict[str, Any]] = {}
    items: dict[str, dict[str, Any]] = {}
    beats: dict[str, list[int]] = {}
    if not paths.history or not paths.history.exists():
        return turns, items, beats
    con = _connect_ro(paths.history)
    try:
        for row in con.execute("""
            SELECT thread_id, status, started_at, completed_at, rollout_ordinal
            FROM thread_turns ORDER BY thread_id, rollout_ordinal
        """):
            turns[row["thread_id"]] = dict(row)
            stamp = _safe_timestamp(row["completed_at"] or row["started_at"])
            if stamp:
                beats.setdefault(row["thread_id"], []).append(int(stamp))

        for row in con.execute("""
            SELECT thread_id, item_type, item_json, rollout_ordinal, created_at_ms
            FROM thread_items ORDER BY thread_id, rollout_ordinal
        """):
            try:
                raw = json.loads(row["item_json"] or "{}")
            except (ValueError, TypeError):
                raw = {}
            tid = row["thread_id"]
            previous_ask = (items.get(tid) or {}).get("ask", "")
            last_message_at = (items.get(tid) or {}).get("lastMessageAt")
            item_type = row["item_type"] or raw.get("type") or ""
            ask = _user_text(raw) if item_type == "userMessage" else ""
            created_at = _safe_timestamp(row["created_at_ms"])
            if item_type in ("userMessage", "agentMessage"):
                last_message_at = created_at
            items[row["thread_id"]] = {
                "type": item_type,
                "status": raw.get("status"),
                "createdAt": created_at,
                "lastMessageAt": last_message_at,
                "ask": ask or previous_ask,
            }
        return turns, items, beats
    finally:
        con.close()


def _pet_for(thread_id: str) -> tuple[str, str, int]:
    digest = hashlib.sha256(thread_id.encode("utf-8", "replace")).digest()
    emoji, species = PET_SPECIES[digest[0] % len(PET_SPECIES)]
    return emoji, species, digest[1] % 360


def _activity_buckets(stamps: Iterable[int], now: float) -> list[int]:
    out = [0] * 12
    for stamp in stamps:
        age = now - stamp
        if 0 <= age < 12 * 3600:
            out[11 - int(age // 3600)] += 1
    return out


def _classify(turn: dict[str, Any] | None, item: dict[str, Any] | None,
              locked: bool, age: float) -> tuple[str, str]:
    status = (turn or {}).get("status")
    item_type = (item or {}).get("type") or ""
    item_status = (item or {}).get("status")

    if status == "failed" or item_status == "failed":
        return "error", "碰到了一根带刺的枝条"
    if status == "interrupted":
        return "paused", "停在半路，等你决定方向"
    if status == "inProgress":
        if not locked and age > STALE_ACTIVE_SECONDS:
            return "paused", "记录仍在进行，但心跳已经离开"
        state, label = ACTION_LABELS.get(item_type, ("working", "正在照料当前任务"))
        return state, label
    if status == "completed":
        if locked:
            return "waiting", "这一轮完成了，正望着入口"
        return "sleeping", "收好工具，在苔藓上休息"
    if locked:
        return "waiting", "已经来到园里，还没有新动作"
    return "quiet", "留下了一枚安静的足迹"


def _display_name(row: dict[str, Any], cwd: str, emoji: str) -> str:
    for key in ("name", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:72]
    folder = Path(cwd).name if cwd else "未命名栖息地"
    return f"{emoji} {folder}"


def _sort_cards(cards: list[dict[str, Any]]) -> None:
    """Newest activity first; status and pinning never override recency."""
    cards.sort(key=lambda card: -(card.get("lastSeen") or 0))


def _latest_activity(turn: dict[str, Any] | None,
                     item: dict[str, Any] | None) -> float:
    """Execution freshness is separate from the displayed message timestamp."""
    return max(filter(None, (
        _safe_timestamp((turn or {}).get("completed_at")),
        _safe_timestamp((turn or {}).get("started_at")),
        _safe_timestamp((item or {}).get("createdAt")),
    )), default=0)


def _helper_is_recent(status: str | None, locked: bool, age: float) -> bool:
    """Keep live helpers and a short afterglow for recently finished ones."""
    if status == "inProgress":
        return locked or age <= STALE_ACTIVE_SECONDS
    return age <= HELPER_RECENT_SECONDS


def _movie_for(day: dt.date, states: list[str]) -> dict[str, Any]:
    mood = "quiet"
    for candidate in ("error", "working", "thinking", "paused", "waiting", "sleeping"):
        if candidate in states:
            mood = candidate
            break
    matching = [movie for movie in MOVIES if mood in movie["tags"]] or MOVIES
    seed = int(hashlib.sha256(f"{day.isoformat()}:{mood}".encode()).hexdigest()[:12], 16)
    movie = dict(matching[seed % len(matching)])
    movie["mood"] = mood
    movie["index"] = MOVIES.index(next(m for m in MOVIES if m["title"] == movie["title"])) + 1
    movie["total"] = len(MOVIES)
    return movie


def collect(root: str | os.PathLike[str] | None = None, *, include_old: bool = False) -> dict[str, Any]:
    now = time.time()
    paths = discover_store(root)
    threads, edges = _load_threads(paths)
    turns, items, beats = _load_history(paths)
    children: dict[str, list[str]] = {}
    child_ids = set()
    for parent, child in edges:
        children.setdefault(parent, []).append(child)
        child_ids.add(child)

    by_id = {row.get("id"): row for row in threads if row.get("id")}
    cards: list[dict[str, Any]] = []
    openable: dict[str, str] = {}
    for row in threads:
        tid = row.get("id")
        if not tid or tid in child_ids or row.get("archived"):
            continue
        cwd = _strip_extended_path(row.get("cwd"))
        updated = _latest_activity(turns.get(tid), items.get(tid))
        age = max(0, now - updated) if updated else float("inf")
        message_at = (items.get(tid) or {}).get("lastMessageAt")
        message_age = max(0, now - message_at) if message_at else None
        locked = (paths.root / "thread-writer-locks" / f"{tid}.lock").exists()
        if not include_old and not locked and age > RECENT_SECONDS:
            continue

        emoji, species, hue = _pet_for(tid)
        state, activity = _classify(turns.get(tid), items.get(tid), locked, age)
        all_child_rows = [by_id[cid] for cid in children.get(tid, []) if cid in by_id]
        child_rows = []
        for child in all_child_rows:
            child_id = child.get("id")
            child_updated = _latest_activity(turns.get(child_id), items.get(child_id))
            child_age = max(0, now - child_updated) if child_updated else float("inf")
            child_locked = (paths.root / "thread-writer-locks" / f"{child_id}.lock").exists()
            child_status = (turns.get(child_id) or {}).get("status")
            if _helper_is_recent(child_status, child_locked, child_age):
                child_rows.append(child)
        active_helpers = sum(
            1 for child in child_rows
            if (turns.get(child.get("id")) or {}).get("status") == "inProgress"
        )
        card = {
            "id": tid,
            "shortId": tid[:8],
            "name": _display_name(row, cwd, emoji),
            "cwd": cwd,
            "folder": Path(cwd).name if cwd else "未知目录",
            "canOpen": bool(cwd and Path(cwd).is_dir()),
            "state": state,
            "activity": activity,
            "ask": (items.get(tid) or {}).get("ask") or "",
            "turnStatus": (turns.get(tid) or {}).get("status") or "unknown",
            "itemType": (items.get(tid) or {}).get("type") or "",
            "locked": locked,
            "lastSeen": message_at,
            "ageSeconds": round(message_age) if message_age is not None else None,
            "activityAgeSeconds": round(age) if age != float("inf") else None,
            "model": row.get("model") or "",
            "reasoning": row.get("reasoning_effort") or "",
            "tokens": row.get("tokens_used") or 0,
            "branch": row.get("git_branch") or "",
            "pinned": bool(row.get("is_pinned")),
            "emoji": emoji,
            "species": species,
            "hue": hue,
            "helpers": len(child_rows),
            "activeHelpers": active_helpers,
            "beats": _activity_buckets(beats.get(tid, []), now),
        }
        cards.append(card)
        if card["canOpen"]:
            openable[tid] = cwd

    # The habitat is a recency view: whichever thread moved most recently belongs
    # at the entrance. State remains visible on the card but never overrides time.
    _sort_cards(cards)
    states = [card["state"] for card in cards]
    return {
        "now": now,
        "name": "Tokenarium",
        "subtitle": "令牌生态园",
        "privacy": "local-metadata-only",
        "source": {
            "root": str(paths.root),
            "stateDb": paths.state.name if paths.state else None,
            "historyDb": paths.history.name if paths.history else None,
            "available": bool(paths.state),
        },
        "summary": {
            "visible": len(cards),
            "busy": sum(s in ("working", "thinking") for s in states),
            "needsYou": sum(s in ("waiting", "paused", "error") for s in states),
            "helpers": sum(card["helpers"] for card in cards),
        },
        "sessions": cards,
        "movie": _movie_for(dt.datetime.now().date(), states),
        "_openable": openable,
    }


OPENABLE: dict[str, str] = {}


def public_snapshot(root: str | os.PathLike[str] | None = None,
                    *, include_old: bool = False) -> dict[str, Any]:
    global OPENABLE
    data = collect(root, include_old=include_old)
    OPENABLE = data.pop("_openable")
    return data


def open_folder(thread_id: str) -> bool:
    path = OPENABLE.get(thread_id)
    if not path or not Path(path).is_dir():
        return False
    try:
        if sys.platform == "win32":
            explorer = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe")
            subprocess.Popen([explorer, path], creationflags=subprocess.CREATE_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        print("open_folder %s -> %s" % (thread_id[:8], path), flush=True)
        return True
    except OSError as exc:
        print("open_folder %s failed: %s" % (thread_id[:8], exc), flush=True)
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "Tokenarium/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            try:
                body = HTML_PATH.read_bytes()
            except OSError:
                self._send(500, b"tokenarium.html missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/snapshot":
            try:
                payload = public_snapshot(include_old=query.get("all") == ["1"])
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except (OSError, sqlite3.Error) as exc:
                body = json.dumps({"error": type(exc).__name__, "sessions": []}).encode()
                self._send(503, body, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/open-folder":
            tid = (query.get("id") or [""])[0]
            ok = open_folder(tid)
            self._send(200 if ok else 404, json.dumps({"ok": ok}).encode(),
                       "application/json")
            return
        if parsed.path == "/api/ui":
            wanted = (query.get("set") or [""])[0]
            if wanted == "toggle":
                wanted = "min" if UI["want"] == "max" else "max"
            if wanted in ("max", "min", "close"):
                UI["want"] = wanted
            self._send(200, b'{"ok":true}', "application/json")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def serve(port: int = DEFAULT_PORT) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Tokenarium is listening on http://127.0.0.1:{port}")
    print("Local metadata only. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
