# -*- coding: utf-8 -*-
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tokenarium_server as zoo
import zoo_server as claude_zoo


class TokenariumTests(unittest.TestCase):
    def test_card_glow_cannot_intercept_folder_button(self):
        html = (Path(__file__).parent / "tokenarium.html").read_text(encoding="utf-8")
        self.assertRegex(html, r"\.habitat:after\{[^}]*pointer-events:none")

    def test_trend_is_hidden_but_creature_still_floats(self):
        html = (Path(__file__).parent / "tokenarium.html").read_text(encoding="utf-8")
        self.assertNotIn('class="activity"', html)
        self.assertNotIn("querySelector('.activity')", html)
        self.assertRegex(html, r"\.creature\{[^}]*animation:float")

    def make_store(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        state = sqlite3.connect(root / "state_1.sqlite")
        state.executescript("""
          CREATE TABLE threads (
            id TEXT, cwd TEXT, title TEXT, name TEXT, preview TEXT,
            created_at INTEGER, updated_at INTEGER, recency_at INTEGER,
            tokens_used INTEGER, model TEXT, reasoning_effort TEXT,
            thread_source TEXT, source TEXT, agent_nickname TEXT,
            agent_path TEXT, is_pinned INTEGER, archived INTEGER,
            git_branch TEXT, project_id TEXT
          );
          CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT, child_thread_id TEXT, status TEXT
          );
        """)
        now = int(time.time())
        state.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("root-123456", str(root), "Synthetic garden", None, None,
                       now-100, now, now, 42000, "test-model", "medium", "user",
                       None, None, None, 0, 0, "test-branch", None))
        state.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("child-12345", str(root), "Synthetic helper", None, None,
                       now-50, now, now, 1000, "test-model", "low", "subagent",
                       None, "Helper", "/root/helper", 0, 0, "", None))
        state.execute("INSERT INTO thread_spawn_edges VALUES (?,?,?)",
                      ("root-123456", "child-12345", "active"))
        state.commit(); state.close()

        history = sqlite3.connect(root / "thread_history_1.sqlite")
        history.executescript("""
          CREATE TABLE thread_turns (
            thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT,
            error_json TEXT, started_at INTEGER, completed_at INTEGER
          );
          CREATE TABLE thread_items (
            thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER,
            created_at_ms INTEGER, item_json TEXT, item_type TEXT,
            updated_at_ordinal INTEGER
          );
        """)
        history.execute("INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?)",
                        ("root-123456", "turn-1", 1, "inProgress", None, now, None))
        history.execute("INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?)",
                        ("child-12345", "turn-2", 1, "inProgress", None, now, None))
        history.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)",
                        ("root-123456", "turn-1", "user-1", 0, (now-1)*1000,
                         json.dumps({"type":"userMessage","content":[
                             {"type":"text","text":"## My request:\nKeep the garden calm."}
                         ]}), "userMessage", 0))
        history.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)",
                        ("root-123456", "turn-1", "item-1", 1, now*1000,
                         json.dumps({"type":"commandExecution","status":"inProgress"}),
                         "commandExecution", 1))
        history.commit(); history.close()
        (root / "thread-writer-locks").mkdir()
        (root / "thread-writer-locks" / "root-123456.lock").touch()
        return temp, root

    def test_collects_root_and_attaches_helper(self):
        temp, root = self.make_store()
        self.addCleanup(temp.cleanup)
        data = zoo.public_snapshot(root)
        self.assertEqual(data["summary"]["visible"], 1)
        self.assertEqual(data["sessions"][0]["state"], "working")
        self.assertEqual(data["sessions"][0]["helpers"], 1)
        self.assertEqual(data["sessions"][0]["activeHelpers"], 1)
        self.assertTrue(data["sessions"][0]["canOpen"])
        self.assertEqual(data["sessions"][0]["ask"], "Keep the garden calm.")
        self.assertNotIn("preview", data["sessions"][0])

    def test_claude_zoo_hides_history_but_keeps_loaded_and_running_threads(self):
        sessions = [
            {"id": "loaded", "locked": True, "turnStatus": "completed",
             "state": "waiting", "ageSeconds": 3600},
            {"id": "running", "locked": False, "turnStatus": "inProgress",
             "state": "working", "ageSeconds": 10},
        ]
        for status in ("completed", "interrupted", "failed", "unknown"):
            sessions.append({"id": status, "locked": False,
                             "turnStatus": status, "ageSeconds": 10})
        for age in (zoo.STALE_ACTIVE_SECONDS + 1, None):
            sessions.append({"id": "stale", "locked": False,
                             "turnStatus": "inProgress", "ageSeconds": age})
        with mock.patch.object(zoo, "collect", return_value={"sessions": sessions}):
            cards = claude_zoo._codex_cards(time.time())
        self.assertEqual([c["id"] for c in cards], ["codex:loaded", "codex:running"])

    def test_card_time_only_advances_for_user_or_agent_messages(self):
        temp, root = self.make_store()
        self.addCleanup(temp.cleanup)
        con = sqlite3.connect(root / "thread_history_1.sqlite")
        self.addCleanup(con.close)
        original = con.execute("SELECT created_at_ms FROM thread_items WHERE item_id='user-1'").fetchone()[0] / 1000
        # Newer metadata, a tool call and turn completion must not replace a message.
        con.execute("UPDATE thread_turns SET status='completed', completed_at=?", (original + 100,))
        con.commit()
        card = zoo.collect(root)["sessions"][0]
        self.assertEqual(card["lastSeen"], original)
        for ordinal, kind in ((2, "agentMessage"), (3, "userMessage")):
            stamp = original + ordinal
            con.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)",
                        ("root-123456", "turn-1", f"message-{ordinal}", ordinal, stamp * 1000,
                         json.dumps({"type": kind}), kind, ordinal))
            con.commit()
            self.assertEqual(zoo.collect(root)["sessions"][0]["lastSeen"], stamp)

    def test_no_message_or_missing_message_timestamp_leaves_time_blank(self):
        temp, root = self.make_store()
        self.addCleanup(temp.cleanup)
        con = sqlite3.connect(root / "thread_history_1.sqlite")
        self.addCleanup(con.close)
        con.execute("UPDATE thread_items SET created_at_ms=NULL WHERE item_type='userMessage'")
        con.commit()
        collect = zoo.collect
        for no_message in (False, True):
            if no_message:
                con.execute("DELETE FROM thread_items WHERE item_type='userMessage'")
                con.commit()
            with mock.patch.object(zoo, "collect", side_effect=lambda: collect(root)):
                card = claude_zoo._codex_cards(time.time())[0]
            self.assertIsNone(card["lastTs"])
            self.assertIsNone(card["idle"])
            self.assertEqual(card["state"], "working")

    def test_running_tool_without_recent_chat_still_keeps_session_visible(self):
        temp, root = self.make_store()
        self.addCleanup(temp.cleanup)
        con = sqlite3.connect(root / "thread_history_1.sqlite")
        con.execute("UPDATE thread_items SET created_at_ms=? WHERE item_type='userMessage'",
                    ((time.time() - 7200) * 1000,))
        con.commit()
        con.close()
        (root / "thread-writer-locks" / "root-123456.lock").unlink()
        collect = zoo.collect
        with mock.patch.object(zoo, "collect", side_effect=lambda: collect(root)):
            card = claude_zoo._codex_cards(time.time())[0]
        self.assertEqual(card["state"], "working")
        self.assertGreaterEqual(card["idle"], 7199)

    def test_claude_zoo_removes_finished_thread_when_lock_is_released(self):
        temp, root = self.make_store()
        self.addCleanup(temp.cleanup)
        collect = zoo.collect
        with mock.patch.object(zoo, "collect", side_effect=lambda: collect(root)):
            self.assertEqual(len(claude_zoo._codex_cards(time.time())), 1)
            con = sqlite3.connect(root / "thread_history_1.sqlite")
            con.execute("UPDATE thread_turns SET status='completed', completed_at=?",
                        (int(time.time()),))
            con.commit()
            con.close()
            self.assertEqual(len(claude_zoo._codex_cards(time.time())), 1)
            (root / "thread-writer-locks" / "root-123456.lock").unlink()
            self.assertEqual(claude_zoo._codex_cards(time.time()), [])
        # 历史展示仍属于 Tokenarium，不因 Zoo 的筛选策略改变。
        self.assertEqual(len(collect(root)["sessions"]), 1)

    def test_movie_is_deterministic(self):
        day = zoo.dt.date(2026, 9, 3)
        self.assertEqual(zoo._movie_for(day, ["working"]),
                         zoo._movie_for(day, ["working"]))

    def test_stale_unlocked_turn_is_not_reported_as_working(self):
        state, _ = zoo._classify({"status": "inProgress"},
                                 {"type": "commandExecution"}, False,
                                 zoo.STALE_ACTIVE_SECONDS + 1)
        self.assertEqual(state, "paused")

    def test_failed_item_wins_over_completed_turn(self):
        state, _ = zoo._classify({"status": "completed"},
                                 {"type": "commandExecution", "status": "failed"},
                                 True, 1)
        self.assertEqual(state, "error")

    def test_idle_helpers_expire_but_live_helpers_remain(self):
        self.assertFalse(zoo._helper_is_recent(
            "completed", False, zoo.HELPER_RECENT_SECONDS + 1))
        self.assertFalse(zoo._helper_is_recent(
            "inProgress", False, zoo.STALE_ACTIVE_SECONDS + 1))
        self.assertTrue(zoo._helper_is_recent(
            "inProgress", True, zoo.HELPER_RECENT_SECONDS * 10))
        self.assertTrue(zoo._helper_is_recent("interrupted", True, 30))

    @unittest.skipUnless(os.name == "nt", "Windows Explorer integration")
    def test_unknown_folder_id_is_rejected(self):
        old = zoo.OPENABLE
        try:
            zoo.OPENABLE = {}
            self.assertFalse(zoo.open_folder("not-allowlisted"))
        finally:
            zoo.OPENABLE = old

    @unittest.skipUnless(os.name == "nt", "Windows Explorer integration")
    def test_allowlisted_folder_uses_explicit_explorer(self):
        old = zoo.OPENABLE
        try:
            with tempfile.TemporaryDirectory() as folder:
                zoo.OPENABLE = {"thread": folder}
                with mock.patch.object(zoo.subprocess, "Popen") as popen:
                    self.assertTrue(zoo.open_folder("thread"))
                    command = popen.call_args.args[0]
                    self.assertTrue(command[0].lower().endswith("explorer.exe"))
                    self.assertEqual(command[1], folder)
        finally:
            zoo.OPENABLE = old

    def test_missing_store_is_a_valid_empty_garden(self):
        with tempfile.TemporaryDirectory() as root:
            data = zoo.public_snapshot(root)
            self.assertFalse(data["source"]["available"])
            self.assertEqual(data["sessions"], [])

    def test_cards_are_sorted_by_latest_activity(self):
        cards = [
            {"lastSeen": 10, "state": "working"},
            {"lastSeen": 30, "state": "sleeping"},
            {"lastSeen": 20, "state": "error"},
        ]
        zoo._sort_cards(cards)
        self.assertEqual([card["lastSeen"] for card in cards], [30, 20, 10])


if __name__ == "__main__":
    unittest.main()
