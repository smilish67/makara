"use strict";
const el = (id) => document.getElementById(id);
const fmt = (n) => (n == null ? "미제공" : Number(n).toLocaleString("ko-KR"));
const day = (s) =>
  s
    ? new Date(s).toLocaleString("ko-KR", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const make = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const colors = { Codex: "#94dfc7", Claude: "#efad90", OpenCode: "#9cbbf5" };
let data = { sessions: [], tasks: [] },
  sid = "",
  off = 0,
  version = 0,
  assigned = {},
  sessionLimit = 50,
  busy = false;
async function api(path, options) {
  const r = await fetch(path, options);
  if (!r.ok) throw Error("요청 실패 (" + r.status + ")");
  return r.json();
}
function notice(s) {
  el("notice").textContent = s;
}
function chosen() {
  return data.sessions.find((s) => s.sid === sid);
}
function linkSession(s, cls) {
  const b = make("button", null, cls);
  b.onclick = () => select(s.sid);
  return b;
}
function list() {
  const term = el("search").value.toLowerCase(),
    agent = el("agent").value;
  const rows = data.sessions.filter(
    (s) =>
      (!agent || s.agent === agent) &&
      [s.title, s.project, s.sid, ...s.groups]
        .join(" ")
        .toLowerCase()
        .includes(term),
  );
  el("resultcount").textContent = fmt(rows.length) + "개 세션";
  el("moreSessions").hidden = rows.length <= sessionLimit;
  el("list").replaceChildren(
    ...rows.slice(0, sessionLimit).map((s) => {
      const b = linkSession(s, "row" + (sid === s.sid ? " active" : ""));
      b.append(
        make("span", s.agent + " · " + day(s.last), "agent"),
        make("b", s.title || s.project.split("/").pop() || "세션"),
        make(
          "small",
          (s.groups.join(" · ") ||
            s.project.split(/[/\\]/).pop() ||
            "프로젝트 미제공") +
            " · " +
            (s.measured ? fmt(s.tokens) + " 토큰" : "토큰 미제공"),
        ),
      );
      return b;
    }),
  );
}
function metrics() {
  const ss = data.sessions;
  el("sessionCount").textContent = fmt(ss.length);
  el("tools").textContent = fmt(ss.reduce((a, s) => a + s.tools, 0));
  const sum = ss.reduce((a, s) => a + s.tokens, 0);
  el("total").textContent = ss.some((s) => s.measured) ? fmt(sum) : "—";
  el("coverage").textContent =
    fmt(ss.filter((s) => s.measured).length) + "개 세션에서 사용량 제공";
  const cv = el("bar"),
    ctx = cv.getContext("2d");
  cv.width = Math.max(1, cv.clientWidth * 2);
  cv.height = 18;
  let x = 0;
  const legend = [];
  for (const [agent, color] of Object.entries(colors)) {
    const n = ss
        .filter((s) => s.agent === agent)
        .reduce((a, s) => a + s.tokens, 0),
      w = sum ? (n / sum) * cv.width : 0;
    ctx.fillStyle = color;
    ctx.fillRect(x, 0, w, cv.height);
    x += w;
    legend.push(
      make("span", agent + " " + (sum ? Math.round((n / sum) * 100) : 0) + "%"),
    );
  }
  el("legend").replaceChildren(...legend);
}
function tasks() {
  el("tasknames").replaceChildren(
    ...data.tasks
      .filter((g) => g.kind === "task")
      .map((g) => new Option(g.name, g.name)),
  );
  const target = el("taskview"),
    open = new Set(
      Array.from(
        target.querySelectorAll("details[open]"),
        (node) => node.dataset.id,
      ),
    );
  target.replaceChildren(make("h2", "작업별 사용량"));
  if (!data.tasks.length) {
    target.append(
      make(
        "p",
        "세션 또는 턴을 선택하고 같은 작업 이름을 지정하면 여러 에이전트의 사용량을 함께 볼 수 있습니다.",
        "muted",
      ),
    );
    return;
  }
  for (const g of data.tasks) {
    let d = make("details", null, "step");
    d.dataset.id = g.kind + "/" + g.name;
    d.open = open.has(d.dataset.id);
    d.append(
      make(
        "summary",
        (g.kind === "task" ? "작업 · " : "프로젝트 · ") +
          g.name +
          " · " +
          g.sessions.length +
          "개 세션 · " +
          (g.measured ? fmt(g.tokens) + " 토큰" : "미제공"),
      ),
    );
    for (const id of g.sessions) {
      const s = data.sessions.find((s) => s.sid === id);
      if (s) {
        const b = linkSession(s, "row");
        b.textContent = s.agent + " · " + (s.title || s.sid);
        d.append(b);
      }
    }
    target.append(d);
  }
}
function tabs(showTasks) {
  el("sessionview").hidden = showTasks;
  el("taskview").hidden = !showTasks;
  el("sessionsTab").classList.toggle("on", !showTasks);
  el("tasksTab").classList.toggle("on", showTasks);
}
function heading() {
  const s = chosen();
  if (!s) return;
  el("agentname").textContent = s.agent + "  /  " + (s.model || "모델 미제공");
  el("title").textContent = s.title || "세션";
  el("project").textContent = s.project;
  const labels = [
    (s.measured ? fmt(s.tokens) : "미제공") + " 토큰",
    "도구 " + fmt(s.tools) + "건",
    "캐시 읽기 " + (s.measured ? fmt(s.cache) : "미제공"),
    day(s.last),
  ];
  el("facts").replaceChildren(...labels.map((v) => make("span", v)));
  el("family").replaceChildren();
  const parent = data.sessions.find((r) => r.sid === s.parent);
  for (const r of [
    ...(parent ? [parent] : []),
    ...data.sessions.filter((r) => r.parent === sid),
  ]) {
    const b = linkSession(r);
    b.textContent =
      (r === parent ? "상위" : "하위") +
      " 세션 · " +
      (r.title || r.sid).slice(0, 38);
    el("family").append(b);
  }
}
function showAssignment() {
  const t = el("turn").value;
  el("task").value = assigned[t] || "";
  el("assignnote").textContent = t
    ? "선택한 턴에 적용 · 비우면 세션 작업을 따릅니다."
    : "세션 전체와 연결된 하위 세션에 적용합니다.";
}
function eventTitle(e) {
  if (e.kind === "request" && e.detail) return e.detail.slice(0, 70);
  if (e.kind !== "tool") return e.label;
  const actions = [
    [/web__run|WebSearch|WebFetch|web_search/, "웹 조사"],
    [/apply_patch|FileChange|^(Edit|Write)$/, "파일 수정"],
    [/CommandExecution|exec_command|^Bash$/, "터미널 실행"],
    [/SubAgentActivity|spawn_agent/, "에이전트 작업"],
    [/ImageView|view_image/, "이미지 확인"],
  ];
  const match = actions.find(([pattern]) => pattern.test(e.label));
  return match ? match[1] + " · " + e.label : e.label;
}
async function timeline(reset = true) {
  const request = ++version;
  if (reset) off = 0;
  const t = el("turn").value,
    params = new URLSearchParams({
      sid,
      offset: String(off),
      turn: t,
      kind: el("kind").value,
    });
  const result = await api("/api/session?" + params);
  if (request !== version) return;
  if (reset) {
    const opts = [
      new Option("전체 턴", ""),
      ...result.turns
        .filter((r) => r.turn)
        .map(
          (r, i) =>
            new Option(
              i +
                1 +
                " · " +
                (r.title || day(r.first)).slice(0, 44) +
                " · " +
                fmt(r.tokens),
              r.turn,
            ),
        ),
    ];
    el("turn").replaceChildren(...opts);
    el("turn").value = t;
    assigned = result.assigned;
    if (document.activeElement !== el("task")) showAssignment();
    el("events").replaceChildren();
  }
  for (const e of result.events) {
    const d = make("details", null, "step");
    d.dataset.kind = e.kind;
    const h = make("summary");
    const clock = e.at
      ? new Date(e.at).toLocaleTimeString("ko-KR", { hour12: false })
      : "—";
    h.append(
      make("time", clock),
      make("b", eventTitle(e)),
      make("span", e.total != null ? fmt(e.total) + " tok" : e.status, "num"),
    );
    d.append(h);
    const bits = [e.detail, e.model, day(e.at)];
    if (e.total != null)
      bits.push(
        "입력 " +
          fmt(e.input) +
          " · 출력 " +
          fmt(e.output) +
          " · 캐시 읽기 " +
          fmt(e.cache) +
          " · 캐시 쓰기 " +
          fmt(e.write_cache) +
          " · 추론 " +
          fmt(e.reason),
      );
    d.append(make("p", bits.filter(Boolean).join("\n")));
    el("events").append(d);
  }
  if (!el("events").children.length)
    el("events").append(
      make("p", "이 조건에 해당하는 기록이 없습니다.", "muted"),
    );
  off += result.events.length;
  el("more").hidden = !result.more;
}
async function select(id) {
  sid = id;
  tabs(false);
  el("detail").hidden = false;
  el("empty").hidden = true;
  el("turn").replaceChildren(new Option("전체 턴", ""));
  el("kind").value = "";
  list();
  heading();
  await timeline();
}
async function load() {
  if (busy) return;
  busy = true;
  try {
    const old = chosen(),
      r = await api("/api/overview");
    data = r;
    metrics();
    list();
    tasks();
    const st = r.status,
      errors = st.sources.some((s) => Object.keys(s.errors).length);
    el("sync").textContent =
      st.error || errors
        ? "수집 상태 확인"
        : st.running
          ? "기록 색인 중"
          : "자동 수집 중";
    el("sources").replaceChildren(
      make("div", "마지막 동기화 · " + day(st.last)),
      ...st.sources.map((s) =>
        make(
          "div",
          s.location +
            " / " +
            s.agent +
            " · " +
            s.files +
            "개" +
            (Object.keys(s.errors).length
              ? " · " + JSON.stringify(s.errors)
              : ""),
        ),
      ),
    );
    if (st.error) el("sources").append(make("div", st.error));
    if (!chosen() && r.sessions.length) await select(r.sessions[0].sid);
    else if (chosen()) {
      heading();
      if (
        old &&
        old.n !== chosen().n &&
        off <= 120 &&
        !el("events").querySelector("details[open]") &&
        document.activeElement !== el("task")
      )
        await timeline();
    }
    notice("");
  } catch (e) {
    notice("로컬 서비스 연결을 확인하세요. " + e.message);
  } finally {
    busy = false;
  }
}
el("assignment").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sid,
        turn: el("turn").value,
        task: el("task").value.trim(),
      }),
    });
    await load();
    await timeline();
    notice("작업 연결을 저장했습니다.");
  } catch (err) {
    notice(err.message);
  }
};
el("sessionsTab").onclick = () => tabs(false);
el("tasksTab").onclick = () => tabs(true);
el("more").onclick = () => timeline(false).catch((e) => notice(e.message));
el("moreSessions").onclick = () => {
  sessionLimit += 50;
  list();
};
el("turn").onchange = el("kind").onchange = () =>
  timeline().catch((e) => notice(e.message));
el("search").oninput = el("agent").onchange = () => {
  sessionLimit = 50;
  list();
};
load();
setInterval(() => {
  if (!document.hidden) load();
}, 15000);
