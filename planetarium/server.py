"""Makara Planetarium loopback API, live collector and dashboard."""

import argparse
import json
import mimetypes
import os
import sqlite3
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .index import DDL, brief, connect, drop, excluded, iso, log_file, opencode

STATIC = Path(__file__).parent / "static"
SUMMARY = """SELECT a.sid,a.parent,a.project,a.title,a.agent,a.model,a.stamp,
 e.turn, min(e.at) first,max(e.at) last,count(e.key) n,sum(e.kind='tool') tools,
 count(e.total) measured,sum(e.total) tokens,sum(e.input) input,sum(e.output) output,
 sum(e.cache) cache,sum(e.reason) reason,sum(e.write_cache) write_cache
 FROM active a LEFT JOIN measured e ON e.src=a.src GROUP BY a.src,e.turn"""


class Observatory:
    def __init__(self, path, config):
        self.path = path
        self.config = config
        self.status = {"running": True, "last": None, "sources": []}
        self.snapshot = {"sessions": [], "tasks": []}
        self.lock = threading.Lock()
        with closing(connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
            if db.execute("PRAGMA user_version").fetchone()[0] < 3:
                with db:
                    db.execute("DELETE FROM events")
                    db.execute("DELETE FROM sessions")
                    db.execute("PRAGMA user_version=3")

    def refresh(self):
        with self.lock, closing(connect(self.path)) as db:
            rows = db.execute(SUMMARY).fetchall()
            assigns = {
                (r["sid"], r["turn"]): r["task"]
                for r in db.execute("SELECT * FROM assignments")
            }
            sessions = {}
            tasks = {}
            parents = {r["sid"]: r["parent"] for r in rows}
            for r in rows:
                sid = r["sid"]
                parent = sid
                visited = set()
                group = assigns.get((sid, r["turn"]))
                while parent and parent not in visited and not group:
                    visited.add(parent)
                    group = assigns.get((parent, ""))
                    parent = parents.get(parent)
                s = sessions.setdefault(
                    sid,
                    {
                        k: r[k]
                        for k in (
                            "sid",
                            "title",
                            "project",
                            "agent",
                            "parent",
                            "model",
                            "stamp",
                        )
                    },
                )
                s.setdefault("groups", [])
                if group and group not in s["groups"]:
                    s["groups"].append(group)
                s["assigned"] = assigns.get((sid, ""), "")
                if r["first"]:
                    s["first"] = min(s.get("first", r["first"]), r["first"])
                s["last"] = max(s.get("last", ""), r["last"] or "")
                for key in ("n", "tools", "measured", "tokens"):
                    s[key] = s.get(key, 0) + (r[key] or 0)
                for key in ("cache", "input", "output", "reason", "write_cache"):
                    if r[key] is not None:
                        s[key] = (s.get(key) or 0) + r[key]
                    else:
                        s.setdefault(key, None)
                project = r["project"] or ""
                bucket = ("task", group) if group else ("project", project)
                if group or project:
                    g = tasks.setdefault(
                        bucket,
                        {
                            "name": group or project,
                            "kind": bucket[0],
                            "tokens": 0,
                            "measured": 0,
                            "sessions": set(),
                        },
                    )
                    g["tokens"] += r["tokens"] or 0
                    g["measured"] += r["measured"]
                    g["sessions"].add(sid)
            for g in tasks.values():
                g["sessions"] = sorted(g["sessions"])
            self.snapshot = {
                "sessions": sorted(
                    sessions.values(),
                    key=lambda x: x["last"] or x["stamp"],
                    reverse=True,
                ),
                "tasks": sorted(tasks.values(), key=lambda g: -g["tokens"]),
            }

    def scan(self):
        config = json.loads(self.config.read_text())
        counts = []
        seen = set()
        complete = True
        self.status = {**self.status, "running": True}
        with closing(connect(self.path)) as db:
            for loc in config["sources"]:
                root = Path(loc["path"])
                agent = loc["agent"]
                errors = {}
                n = 0
                if not root.exists():
                    complete = False
                    counts.append(
                        {
                            "agent": agent,
                            "location": loc["location"],
                            "files": 0,
                            "errors": {"경로 연결 대기": 1},
                        }
                    )
                    continue
                if agent == "OpenCode":
                    files = [root]
                elif agent == "Codex":
                    files = [
                        p
                        for p in root.rglob("rollout-*.jsonl*")
                        if p.name.endswith((".jsonl", ".jsonl.zst"))
                    ]
                else:
                    files = [
                        p
                        for p in root.rglob("*.jsonl")
                        if p.parent.parent == root or p.parent.name == "subagents"
                    ]
                for p in files:
                    seen.add(str(p))
                    try:
                        with db:
                            if agent == "OpenCode":
                                opencode(db, p, config["excluded_paths"])
                            else:
                                log_file(db, p, agent, config["excluded_paths"])
                        n += 1
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        OSError,
                        sqlite3.Error,
                        EOFError,
                    ) as e:
                        errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
                counts.append(
                    {
                        "agent": agent,
                        "location": loc["location"],
                        "files": n,
                        "errors": errors,
                    }
                )
            if complete:
                with db:
                    for r in db.execute(
                        "SELECT src,agent,project FROM sessions"
                    ).fetchall():
                        origin = (
                            r["src"].rsplit("#", 1)[0]
                            if r["agent"] == "OpenCode"
                            else r["src"]
                        )
                        if origin not in seen or excluded(
                            r["project"], config["excluded_paths"]
                        ):
                            drop(db, r["src"])
            changed = db.total_changes > 0
        if changed or self.status["last"] is None:
            self.refresh()
        self.status = {
            "running": False,
            "last": iso(time.time() * 1000),
            "sources": counts,
        }

    def loop(self):
        while True:
            try:
                self.scan()
            except (
                OSError,
                sqlite3.Error,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                EOFError,
            ) as e:
                self.status = {
                    **self.status,
                    "running": False,
                    "error": type(e).__name__,
                }
            time.sleep(20)

    def timeline(self, q):
        sid = q.get("sid", [""])[0]
        off = max(0, int(q.get("offset", ["0"])[0]))
        with closing(connect(self.path)) as db:
            s = db.execute("SELECT src FROM active WHERE sid=?", (sid,)).fetchone()
            if not s:
                raise ValueError("세션을 찾을 수 없습니다.")
            terms = ["src=?"]
            args = [s[0]]
            for name in ("turn", "kind"):
                v = q.get(name, [""])[0]
                if v:
                    if name == "kind" and v == "usage":
                        terms.append("kind IN ('usage','legacy')")
                    else:
                        terms.append(name + "=?")
                        args.append(v)
            where = " AND ".join(terms)
            rows = db.execute(
                "SELECT turn,at,kind,label,detail,model,status,input,output,cache,write_cache,reason,total FROM measured WHERE "
                + where
                + " ORDER BY at,key LIMIT 121 OFFSET ?",
                (*args, off),
            )
            items = [dict(r) for r in rows]
            turns = db.execute(
                "SELECT e.turn,min(at) first,max(CASE WHEN kind='request' THEN detail END) title,sum(total) tokens FROM measured e WHERE src=? GROUP BY e.turn ORDER BY first",
                (s[0],),
            )
            return {
                "events": items[:120],
                "more": len(items) > 120,
                "turns": [dict(r) for r in turns],
                "assigned": dict(
                    db.execute("SELECT turn,task FROM assignments WHERE sid=?", (sid,))
                ),
            }

    def assign(self, data):
        sid, turn, name = (data.get(k, "") for k in ("sid", "turn", "task"))
        if not all(isinstance(x, str) for x in (sid, turn, name)) or len(name) > 100:
            raise ValueError("작업 이름을 확인하세요.")
        with closing(connect(self.path)) as db, db:
            s = db.execute("SELECT src FROM active WHERE sid=?", (sid,)).fetchone()
            if (
                not s
                or turn
                and not db.execute(
                    "SELECT 1 FROM events WHERE src=? AND turn=? LIMIT 1", (s[0], turn)
                ).fetchone()
            ):
                raise ValueError("지정한 세션 또는 턴이 없습니다.")
            if name.strip():
                db.execute(
                    "INSERT OR REPLACE INTO assignments VALUES(?,?,?)",
                    (sid, turn, brief(name, 100)),
                )
            else:
                db.execute(
                    "DELETE FROM assignments WHERE sid=? AND turn=?", (sid, turn)
                )
        self.refresh()


def serve(obs, bind, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_data(self, body, kind="application/json", code=200):
            if kind == "application/json":
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; frame-ancestors 'none'; object-src 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            host = self.headers.get("Host", "")
            return (
                host in {f"localhost:{port}", f"127.0.0.1:{port}"}
                and self.headers.get("Origin", f"http://{host}") == f"http://{host}"
            )

        def do_GET(self):
            if not self.allowed():
                return self.send_error(403)
            u = urlsplit(self.path)
            if u.path == "/api/overview":
                return self.send_data({**obs.snapshot, "status": obs.status})
            if u.path == "/api/session":
                try:
                    return self.send_data(obs.timeline(parse_qs(u.query)))
                except ValueError as e:
                    return self.send_data({"error": str(e)}, code=400)
            files = {"/": "index.html", "/style.css": "style.css", "/app.js": "app.js"}
            if u.path not in files:
                return self.send_error(404)
            p = STATIC / files[u.path]
            self.send_data(
                p.read_bytes(), mimetypes.guess_type(str(p))[0] + "; charset=utf-8"
            )

        def do_POST(self):
            if (
                not self.allowed()
                or self.headers.get_content_type() != "application/json"
            ):
                return self.send_error(403)
            if self.path != "/api/task":
                return self.send_error(404)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n < 8192:
                    raise ValueError("요청 크기 오류")
                data = json.loads(self.rfile.read(n))
                if not isinstance(data, dict):
                    raise TypeError("요청 형식 오류")
                obs.assign(data)
                self.send_data({"saved": True})
            except (ValueError, TypeError) as e:
                self.send_data({"error": str(e)}, code=400)

    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=obs.loop, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18768)
    a = p.parse_args()
    os.umask(0o077)
    serve(Observatory(a.db, a.config), a.bind, a.port)
