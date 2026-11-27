import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from planetarium.index import brief, excluded, usage
from planetarium.server import Observatory


class PlanetariumTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sources = self.root / "sessions"
        self.sources.mkdir()
        self.config = self.root / "sources.json"
        self.config.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "agent": "Codex",
                            "location": "test",
                            "path": str(self.sources),
                        }
                    ],
                    "excluded_paths": ["/private"],
                }
            )
        )
        self.obs = Observatory(self.root / "index.sqlite", self.config)

    def log(self, filename, *records):
        path = self.sources / ("rollout-" + filename + ".jsonl")
        with path.open("a") as out:
            for kind, payload in records:
                print(
                    json.dumps(
                        {
                            "type": kind,
                            "timestamp": "2026-01-01T00:00:00Z",
                            "payload": payload,
                        }
                    ),
                    file=out,
                )
        return path

    def native(self, value):
        return (
            "token_usage_record",
            {"response_id": "r", "usage": {"total_tokens": value}},
        )

    def test_incremental_dedup_and_partial_record(self):
        records = [
            ("session_meta", {"id": "s", "cwd": "/project"}),
            ("turn_context", {"turn_id": "t"}),
            (
                "event_msg",
                {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 20}},
                },
            ),
            self.native(20),
            self.native(20),
        ]
        path = self.log("one", *records)
        self.log("copy", *records)
        with path.open("ab") as f:
            f.write(
                b'{"type":"event_msg","payload":{"type":"user_message","message":"question"}}'
            )
        self.obs.scan()
        self.assertEqual(len(self.obs.snapshot["sessions"]), 1)
        s = self.obs.snapshot["sessions"][0]
        self.assertEqual((s["measured"], s["tokens"], s["cache"]), (1, 20, None))
        with path.open("ab") as f:
            f.write(b"\n")
        self.obs.scan()
        self.obs.scan()
        self.assertEqual(self.obs.snapshot["sessions"][0]["title"], "question")
        self.assertEqual(self.obs.snapshot["tasks"][0]["tokens"], 20)

    def test_fork_preserves_identity_and_inherits_assignment(self):
        self.log(
            "parent", ("session_meta", {"id": "a", "cwd": "/work"}), self.native(10)
        )
        self.log(
            "child",
            (
                "session_meta",
                {
                    "id": "b",
                    "cwd": "/work",
                    "parent_thread_id": "a",
                    "subagent_history_start_ordinal": 3,
                },
            ),
            ("session_meta", {"id": "a", "cwd": "/work"}),
            self.native(10),
            ("turn_context", {"turn_id": "real"}),
            self.native(7),
        )
        self.obs.scan()
        self.obs.assign({"sid": "Codex/a", "task": "shared"})
        self.assertEqual(self.obs.snapshot["tasks"][0]["tokens"], 17)
        self.obs.assign({"sid": "Codex/b", "turn": "real", "task": "other"})
        self.assertEqual(
            {g["name"]: g["tokens"] for g in self.obs.snapshot["tasks"]},
            {"shared": 10, "other": 7},
        )
        self.obs.assign({"sid": "Codex/b", "turn": "real", "task": ""})
        self.assertEqual(self.obs.snapshot["tasks"][0]["tokens"], 17)

    def test_privacy_exclusion_and_tool_names(self):
        self.log(
            "excluded",
            ("session_meta", {"id": "off", "cwd": "/private/project"}),
            self.native(40),
        )
        self.log(
            "ok",
            ("session_meta", {"id": "ok", "cwd": "/public"}),
            (
                "response_item",
                {
                    "type": "custom_tool_call",
                    "call_id": "call",
                    "name": "exec",
                    "input": 'await tools.web__run("private query")',
                },
            ),
            (
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call",
                    "output": "private result",
                },
            ),
            ("response_item", {"type": "reasoning", "text": "private reasoning"}),
        )
        self.obs.scan()
        self.assertEqual(len(self.obs.snapshot["sessions"]), 1)
        events = self.obs.timeline({"sid": ["Codex/ok"]})["events"]
        self.assertEqual(
            [(e["label"], e["status"]) for e in events], [("web__run", "반환")]
        )
        self.assertNotIn("private", json.dumps(events))
        self.assertTrue(excluded("C:/Users/Test/repo", ["/mnt/c/users/test"]))
        self.assertEqual(brief("token\npassword=example"), "token [가림]")

    def test_claude_cache_and_repeated_message(self):
        root = self.sources / "project"
        root.mkdir()
        msg = {
            "id": "response",
            "model": "model",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        }
        path = root / "test.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "abc",
                        "cwd": "/code",
                        "message": msg,
                    }
                )
                for _ in range(2)
            )
            + "\n"
        )
        cfg = json.loads(self.config.read_text())
        cfg["sources"][0]["agent"] = "Claude"
        self.config.write_text(json.dumps(cfg))
        self.obs.scan()
        s = self.obs.snapshot["sessions"][0]
        self.assertEqual(
            (s["measured"], s["tokens"], s["cache"], s["reason"]), (1, 17, 3, None)
        )

    def test_opencode_accounting_and_deletion(self):
        path = self.root / "opencode.db"
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.executescript(
            "CREATE TABLE session_v2(id,parent_id,title,directory,time_updated); CREATE TABLE session_message(id,type,time_created,time_updated,seq,data,session_id);"
        )
        with db:
            db.execute("INSERT INTO session_v2 VALUES('s',NULL,'task','/app',1000)")
            db.execute(
                "INSERT INTO session_message VALUES('m','assistant',1000,1000,1,?,'s')",
                (
                    json.dumps(
                        {
                            "tokens": {
                                "input": 2,
                                "output": 3,
                                "reasoning": 1,
                                "cache": {"read": 4},
                                "total": None,
                            }
                        }
                    ),
                ),
            )
        cfg = json.loads(self.config.read_text())
        cfg["sources"][0].update(agent="OpenCode", path=str(path))
        self.config.write_text(json.dumps(cfg))
        self.obs.scan()
        self.assertEqual(self.obs.snapshot["sessions"][0]["tokens"], 10)
        with db:
            db.execute("DELETE FROM session_v2")
        self.obs.scan()
        self.assertFalse(self.obs.snapshot["sessions"])
        self.assertIsNone(usage({}, "OpenCode"))
