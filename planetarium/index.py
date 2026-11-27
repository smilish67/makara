"""Read observable work from local session logs. Never send records to an LLM."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS sessions(src TEXT PRIMARY KEY,sid TEXT,agent TEXT,project TEXT,title TEXT,parent TEXT,model TEXT,stamp TEXT,offset INTEGER,state TEXT);
CREATE INDEX IF NOT EXISTS session_id ON sessions(sid);
CREATE TABLE IF NOT EXISTS events(src TEXT,key TEXT,turn TEXT,at TEXT,kind TEXT,label TEXT,detail TEXT,model TEXT,status TEXT,
 input INTEGER,output INTEGER,cache INTEGER,write_cache INTEGER,reason INTEGER,total INTEGER, PRIMARY KEY(src,key));
CREATE INDEX IF NOT EXISTS timeline ON events(src,at);
CREATE INDEX IF NOT EXISTS turns ON events(src,turn,kind);
CREATE TABLE IF NOT EXISTS assignments(sid TEXT,turn TEXT,task TEXT,PRIMARY KEY(sid,turn));
CREATE VIEW IF NOT EXISTS active AS SELECT * FROM (SELECT sessions.*,row_number() OVER(PARTITION BY sid ORDER BY stamp DESC,offset DESC,src) AS choice FROM sessions WHERE sid!='') WHERE choice=1;
CREATE VIEW IF NOT EXISTS measured AS SELECT e.* FROM events e WHERE kind!='legacy' OR NOT EXISTS
 (SELECT 1 FROM events n WHERE n.src=e.src AND n.turn=e.turn AND n.kind='usage' AND n.total IS NOT NULL);
"""
SECRET = re.compile(
    r"(?i)(?:authorization|bearer\s|private.key|password|비밀번호|api[ _-]?key|access[ _-]?token|secret[ _-]?key|://[^ /]+:[^ /]+@|\b(?:gh[pousr]_|github_pat_|sk-|AKIA|eyJ)[a-z0-9_-]{10,}|[a-z0-9_+/=-]{44,})"
)


def brief(value, limit=280):
    if not isinstance(value, str):
        return ""
    lines = []
    for line in value.splitlines()[:12]:
        if line.lstrip().startswith(("<", "# AGENTS.md", chr(96) * 3)):
            break
        lines.append("[가림]" if SECRET.search(line) else line.strip())
    return " ".join(" ".join(lines).split())[:limit]


def iso(value):
    if isinstance(value, (int, float)) and value:
        return (
            datetime.fromtimestamp(value / 1000, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return str(value or "")


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA temp_store=MEMORY")
    return db


def text_parts(parts):
    if isinstance(parts, str):
        return brief(parts)
    return brief(
        " ".join(
            str(x.get("text", ""))[:800]
            for x in (parts or [])
            if isinstance(x, dict)
            and x.get("type") in ("text", "input_text", "output_text")
        )
    )


def event(db, src, key, st, at, kind, label, detail="", status="", counts=None):
    db.execute(
        "INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            src,
            str(key),
            st.get("turn", ""),
            iso(at),
            kind,
            brief(label, 130),
            brief(detail),
            brief(st.get("model", ""), 100),
            brief(status, 50),
            *(counts or (None,) * 6),
        ),
    )


def usage(raw, agent):
    if not isinstance(raw, dict) or not raw:
        return None

    def integer(value):
        return value if isinstance(value, int) and value >= 0 else None

    if agent == "OpenCode":
        cache = raw.get("cache") or {}
        values = [
            integer(v)
            for v in (
                raw.get("input"),
                raw.get("output"),
                cache.get("read"),
                cache.get("write"),
                raw.get("reasoning"),
            )
        ]
        total = integer(raw.get("total"))
    else:
        keys = (
            (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "reasoning_output_tokens",
            )
            if agent == "Claude"
            else (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "reasoning_output_tokens",
            )
        )
        values = [integer(raw.get(k)) for k in keys]
        total = integer(raw.get("total_tokens"))
    if total is None and values[0] is not None and values[1] is not None:
        components = (
            values
            if agent == "OpenCode"
            else values[:4]
            if agent == "Claude"
            else values[:2]
        )
        total = sum(v for v in components if v is not None)
    return (*values, total)


def tool_label(payload):
    name = payload.get("name") or "web_search"
    if name not in ("exec", "functions.exec"):
        return name
    # Code mode wrappers can call several registered tools in a single event.
    # Keep only identifiers. Arguments, queries and commands never enter the index.
    body = payload.get("input") or payload.get("arguments") or ""
    if not isinstance(body, str):
        return name
    names = dict.fromkeys(
        re.findall(r"\btools\.([a-zA-Z_][a-zA-Z0-9_]{0,90})\s*\(", body[:32768])
    )
    return " · ".join(list(names)[:4]) or name


def message(db, src, key, st, at, user, msg):
    if not msg:
        return
    kind = "request" if user else "answer"
    dup = db.execute(
        "SELECT 1 FROM events WHERE src=? AND turn=? AND kind=? AND detail=? LIMIT 1",
        (src, st.get("turn", ""), kind, msg),
    ).fetchone()
    if not dup:
        event(db, src, key, st, at, kind, "사용자 요청" if user else "응답", msg)
    if user and not st.get("title"):
        st["title"] = msg[:95]


def elapsed(start, end):
    if (
        isinstance(start, (int, float))
        and isinstance(end, (int, float))
        and end >= start
    ):
        return f"소요 {(end - start) / 1000:.2f}초"
    return ""


def codex(db, src, k, j, st):
    ty, p, at = j.get("type"), j.get("payload") or {}, j.get("timestamp")
    t = p.get("type")
    if st.get("skip", 0):
        st["skip"] -= 1
        return
    if ty == "session_meta" and st.get("id") and p.get("id") != st["id"]:
        st["inherited"] = True
        return
    if st.get("inherited"):
        if p.get("thread_id") != st.get("id"):
            return
        st.pop("inherited")
    if p.get("thread_id") and st.get("id") and p["thread_id"] != st["id"]:
        return
    if ty == "session_meta":
        boundary = p.get("subagent_history_start_ordinal")
        if isinstance(boundary, int) and boundary > 1:
            st["skip"] = boundary - 1
        st["parent"] = p.get("parent_thread_id") or p.get("forked_from_id") or ""
        st.update(id=p.get("id") or p.get("session_id"), cwd=p.get("cwd", ""))
        source = p.get("source")
        if isinstance(source, dict):
            sub = source.get("subagent")
            if isinstance(sub, dict):
                st["parent"] = (sub.get("thread_spawn") or {}).get(
                    "parent_thread_id"
                ) or st["parent"]
    elif ty == "turn_context":
        st.update(model=p.get("model", ""), turn=p.get("turn_id") or st.get("turn", ""))
    elif ty == "event_msg" and t in ("task_started", "task_complete", "turn_aborted"):
        st["turn"] = p.get("turn_id") or st.get("turn", "")
        event(
            db,
            src,
            k,
            st,
            at,
            "status",
            {
                "task_started": "턴 시작",
                "task_complete": "턴 완료",
                "turn_aborted": "턴 중단",
            }[t],
        )
    elif ty == "token_usage_record":
        st["turn"] = p.get("turn_id") or st.get("turn", "")
        event(
            db,
            src,
            "usage:" + str(p.get("response_id") or k),
            st,
            at,
            "usage",
            "모델 호출",
            counts=usage(p.get("usage"), "Codex"),
        )
    elif ty == "event_msg" and t == "token_count":
        raw = (p.get("info") or {}).get("total_token_usage") or {}
        if not raw:
            return
        prev = st.get("previous", {})
        delta = {
            n: max(0, v - prev.get(n, 0)) for n, v in raw.items() if isinstance(v, int)
        }
        if (
            not prev
            and st.get("parent")
            or raw.get("total_tokens", 0) < prev.get("total_tokens", 0)
        ):
            delta = (p.get("info") or {}).get("last_token_usage") or {}
        st["previous"] = {
            key: value for key, value in raw.items() if isinstance(value, int)
        }
        if delta.get("total_tokens"):
            event(
                db, src, k, st, at, "legacy", "모델 호출", counts=usage(delta, "Codex")
            )
    elif ty == "event_msg" and t in ("user_message", "agent_message"):
        message(db, src, k, st, at, t == "user_message", brief(p.get("message", "")))
    elif (
        ty == "response_item"
        and t == "message"
        and p.get("role") in ("user", "assistant")
        and p.get("phase") != "commentary"
    ):
        message(db, src, k, st, at, p["role"] == "user", text_parts(p.get("content")))
    elif ty == "response_item" and t in (
        "function_call",
        "custom_tool_call",
        "web_search_call",
    ):
        event(
            db,
            src,
            "tool:" + str(p.get("call_id") or p.get("id") or k),
            st,
            at,
            "tool",
            tool_label(p),
        )
    elif ty == "response_item" and t in (
        "function_call_output",
        "custom_tool_call_output",
    ):
        db.execute(
            "UPDATE events SET status='반환' WHERE src=? AND key=?",
            (src, "tool:" + str(p.get("call_id"))),
        )
    elif ty == "event_msg" and t == "item_completed":
        item = p.get("item", {})
        if item.get("type") in (
            "McpToolCall",
            "CommandExecution",
            "FileChange",
            "Extension",
            "SubAgentActivity",
            "ImageView",
            "WebSearch",
        ):
            event(
                db,
                src,
                "tool:" + str(item.get("id") or k),
                st,
                at,
                "tool",
                str(item.get("tool") or item.get("kind") or item["type"]),
                detail=elapsed(p.get("started_at_ms"), p.get("completed_at_ms")),
                status=str(item.get("status", "완료")),
            )
        elif (
            item.get("type") in ("UserMessage", "AgentMessage")
            and item.get("phase") != "commentary"
        ):
            message(
                db,
                src,
                k,
                st,
                at,
                item["type"] == "UserMessage",
                text_parts(item.get("content")),
            )


def claude(db, src, k, j, st):
    sid = j.get("sessionId") or j.get("session_id")
    if sid:
        st["id"] = sid
        if "/subagents/" in src:
            st.update(id=sid + "/" + Path(src).stem, parent=sid)
    if j.get("cwd"):
        st["cwd"] = j["cwd"]
    if j.get("type") == "ai-title":
        st["title"] = brief(j.get("aiTitle"), 95)
    msg = j.get("message") or {}
    if not isinstance(msg, dict):
        return
    parts = msg.get("content", [])
    at = j.get("timestamp")
    if j.get("type") == "user":
        for b in parts if isinstance(parts, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                db.execute(
                    "UPDATE events SET status=? WHERE src=? AND key=?",
                    (
                        "오류" if b.get("is_error") else "반환",
                        src,
                        "tool:" + str(b.get("tool_use_id")),
                    ),
                )
        caption = text_parts(parts)
        if caption and not j.get("isMeta"):
            st["turn"] = j.get("uuid", str(k))
            message(db, src, k, st, at, True, caption)
    if j.get("type") != "assistant":
        return
    st["model"] = msg.get("model", "")
    ident = msg.get("id") or j.get("uuid") or k
    if msg.get("usage") and st["model"] != "<synthetic>":
        event(
            db,
            src,
            "usage:" + str(ident),
            st,
            at,
            "usage",
            "모델 호출",
            counts=usage(msg["usage"], "Claude"),
        )
    for b in parts if isinstance(parts, list) else []:
        if b.get("type") == "tool_use":
            event(
                db,
                src,
                "tool:" + str(b.get("id") or k),
                st,
                at,
                "tool",
                b.get("name", "도구"),
            )
    message(db, src, k, st, at, False, text_parts(parts))


def excluded(cwd, paths):
    def normalize(path):
        value = str(path or "").replace(chr(92), "/").rstrip("/")
        if re.match(r"^[a-zA-Z]:/", value):
            value = "/mnt/" + value[0].lower() + value[2:]
        return value.casefold() if value.startswith("/mnt/") else value

    cwd = normalize(cwd)
    return any(cwd == p or cwd.startswith(p + "/") for p in map(normalize, paths) if p)


def save(db, src, agent, st, stamp, offset):
    sid = lambda key: agent + "/" + str(st[key]) if st.get(key) else ""
    db.execute(
        "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            src,
            sid("id"),
            agent,
            brief(st.get("cwd", ""), 500),
            brief(st.get("title", ""), 95),
            sid("parent"),
            brief(st.get("model", ""), 100),
            iso(stamp),
            offset,
            json.dumps(st, ensure_ascii=False),
        ),
    )


def log_file(db, path, agent, exclusions):
    src = str(path)
    stat = path.stat()
    stamp = iso(stat.st_mtime * 1000)
    row = db.execute("SELECT * FROM sessions WHERE src=?", (src,)).fetchone()
    previous = json.loads(row["state"]) if row else {}
    policy_hash = hashlib.sha256(json.dumps(exclusions).encode()).hexdigest()
    if (
        row
        and (
            "excluded_policy" not in previous
            or previous["excluded_policy"] == policy_hash
        )
        and row["stamp"] == stamp
        and row["offset"] == stat.st_size
        and not excluded(row["project"], exclusions)
    ):
        return
    zipped = src.endswith(".zst")
    append = (
        row
        and not zipped
        and 0 < row["offset"] < stat.st_size
        and previous.get("inode") == stat.st_ino
    )

    if zipped:
        from compression import zstd

        stream = zstd.open(path, "rb")
    else:
        stream = path.open("rb")
    with stream as f:
        if append:
            offset = row["offset"]
            f.seek(max(0, offset - 256))
            append = hashlib.sha256(
                f.read(min(256, offset))
            ).hexdigest() == previous.get("tail")
        if not append:
            db.execute("DELETE FROM events WHERE src=?", (src,))
            if not zipped:
                f.seek(0)
        st = previous if append else {"inode": stat.st_ino}
        pos = f.tell()
        for line in f:
            if not line.endswith(b"\n") and not zipped:
                break
            k = str(pos)
            pos += len(line)
            if excluded(st.get("cwd"), exclusions):
                break
            try:
                j = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(j, dict):
                (codex if agent == "Codex" else claude)(db, src, k, j, st)
        if not zipped and pos:
            f.seek(max(0, pos - 256))
            st["tail"] = hashlib.sha256(f.read(min(256, pos))).hexdigest()
    if excluded(st.get("cwd"), exclusions):
        db.execute("DELETE FROM events WHERE src=?", (src,))
        st = {"excluded_policy": policy_hash}
        pos = stat.st_size
    save(db, src, agent, st, stamp, stat.st_size if zipped else pos)


def opencode(db, path, exclusions):
    prefix = str(path) + "#"
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=4)
    ) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute("BEGIN")
        rows = reader.execute(
            "SELECT id,parent_id,title,directory,time_updated FROM session_v2"
        )
        live = set()
        for s in rows:
            src = prefix + s["id"]
            if excluded(s["directory"], exclusions):
                continue
            live.add(src)
            marker = reader.execute(
                "SELECT count(*),max(time_updated) FROM session_message WHERE session_id=?",
                (s["id"],),
            ).fetchone()
            fingerprint = tuple(marker) + (s["time_updated"],)
            old = db.execute(
                "SELECT state FROM sessions WHERE src=?", (src,)
            ).fetchone()
            if old and json.loads(old[0]).get("sig") == list(fingerprint):
                continue
            st = {
                "id": s["id"],
                "parent": s["parent_id"],
                "cwd": s["directory"],
                "title": brief(s["title"], 95),
                "sig": fingerprint,
            }
            db.execute("DELETE FROM events WHERE src=?", (src,))
            for m in reader.execute(
                "SELECT id,type,time_created,data FROM session_message WHERE session_id=? ORDER BY seq",
                (s["id"],),
            ):
                j = json.loads(m["data"])
                key = m["id"]
                at = m["time_created"]
                if m["type"] == "user":
                    st["turn"] = key
                    event(
                        db,
                        src,
                        key,
                        st,
                        at,
                        "request",
                        "사용자 요청",
                        brief(j.get("text")),
                    )
                elif m["type"] in ("assistant", "compaction"):
                    mod = j.get("model") or {}
                    st["model"] = (
                        mod.get("id", "") if isinstance(mod, dict) else str(mod)
                    )
                    event(
                        db,
                        src,
                        key,
                        st,
                        at,
                        "usage",
                        "모델 호출",
                        counts=usage(j.get("tokens"), "OpenCode"),
                    )
                    for i, b in enumerate(j.get("content") or []):
                        if isinstance(b, dict) and b.get("type") == "tool":
                            event(
                                db,
                                src,
                                key + ":" + str(i),
                                st,
                                at,
                                "tool",
                                b.get("name", "도구"),
                                detail=elapsed(
                                    (b.get("time") or {}).get("start"),
                                    (b.get("time") or {}).get("end"),
                                ),
                                status=str((b.get("state") or {}).get("status", "")),
                            )
                    message(
                        db,
                        src,
                        key + ":text",
                        st,
                        at,
                        False,
                        text_parts(j.get("content")),
                    )
                else:
                    event(db, src, key, st, at, "status", m["type"])
            save(db, src, "OpenCode", st, s["time_updated"], marker[0])
        for r in db.execute(
            "SELECT src FROM sessions WHERE substr(src,1,?)=?", (len(prefix), prefix)
        ).fetchall():
            if r[0] not in live:
                drop(db, r[0])


def drop(db, src):
    db.execute("DELETE FROM events WHERE src=?", (src,))
    db.execute("DELETE FROM sessions WHERE src=?", (src,))
