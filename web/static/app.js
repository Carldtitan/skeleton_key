import { html, render, useState, useEffect, useRef, useCallback } from "/static/vendor/preact-htm.js";

/* ---------- plumbing ---------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && !location.hash.startsWith("#/r/") && !location.hash.startsWith("#/login")) {
    location.hash = "#/login?next=" + encodeURIComponent(location.hash.slice(1) || "/");
    throw new Error("login required");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || data.message || res.statusText), { status: res.status });
  return data;
}

function useRoute() {
  const read = () => {
    const [path, query = ""] = (location.hash.slice(1) || "/").split("?");
    return { parts: path.split("/").filter(Boolean), query: new URLSearchParams(query) };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return route;
}

/** Poll `fn` every `ms` while mounted; returns [data, error, refresh]. */
function usePoll(fn, ms, deps) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const alive = useRef(true);
  const tick = useCallback(async () => {
    try { const d = await fn(); if (alive.current) { setData(d); setError(null); } }
    catch (e) { if (alive.current) setError(e); }
  }, deps);
  useEffect(() => {
    alive.current = true;
    tick();
    const id = ms ? setInterval(tick, ms) : null;
    return () => { alive.current = false; id && clearInterval(id); };
  }, [tick, ms]);
  return [data, error, tick];
}

const fmtS = (s) => (s == null ? "–" : s < 10 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`);
const fmtUsd = (c) => (c == null ? "–" : c === 0 ? "$0" : c < 0.01 ? `$${c.toFixed(4)}` : `$${c.toFixed(3)}`);
const fmtN = (n) => (n == null ? "–" : n.toLocaleString());
const mask = (key) => (key ? key.slice(0, 7) + "…" : "");

/* ---------- small components ---------- */

function Help({ children }) {
  const [open, setOpen] = useState(false);
  const ref = useRef();
  useEffect(() => {
    const close = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);
  return html`<span class="help" ref=${ref}>
    <button aria-label="Help" aria-expanded=${open} onClick=${() => setOpen(!open)}>?</button>
    ${open && html`<div class="pop" role="dialog">${children}</div>`}
  </span>`;
}

function Status({ state }) {
  const map = {
    active: ["ok", "connected"], expired: ["bad", "expired"], none: ["idle", "not connected"],
    busy: ["busy", "working"],
  };
  const [cls, word] = map[state] || ["idle", state];
  return html`<span class="status ${cls}">${word}</span>`;
}

function Copy({ text, shown }) {
  const [done, setDone] = useState(false);
  const copy = async () => { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1400); };
  return html`<div class="copy"><code>${shown || text}</code>
    <button class="btn small" onClick=${copy}>${done ? "Copied" : "Copy"}</button></div>`;
}

function Frame({ url, children }) {
  return html`<div class="frame">
    ${url ? html`<iframe src=${url} title="Live browser" allow="clipboard-read; clipboard-write"></iframe>`
          : html`<div class="placeholder">starting browser…</div>`}
    ${children}
  </div>`;
}

function Handoff({ message, onDone }) {
  return html`<div class="handoff" role="alert"><span class="msg">${message}</span>
    <button class="btn accent" onClick=${onDone}>Done</button></div>`;
}

/* ---------- login ---------- */

function Login({ query }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    try { await api("/api/login", { method: "POST", body: { password: pw } }); location.hash = "#" + (query.get("next") || "/"); }
    catch (x) { setErr("Wrong password"); }
  };
  return html`<div class="login"><h1>🗝 Skeleton Key</h1>
    <form onSubmit=${submit}>
      <label class="vh" for="pw">Password</label>
      <input id="pw" class="text" type="password" autofocus value=${pw} onInput=${(e) => setPw(e.target.value)} />
      <button class="btn primary">Enter</button>
    </form>${err && html`<div class="error">${err}</div>`}</div>`;
}

/* ---------- home ---------- */

function Home() {
  const [data] = usePoll(() => api("/api/overview"), 4000, []);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const start = async (e) => {
    e.preventDefault();
    if (!url.trim() || busy) return;
    setBusy(true);
    try { const { id } = await api("/api/jobs", { method: "POST", body: { site_url: url.trim() } }); location.hash = `#/job/${id}`; }
    finally { setBusy(false); }
  };
  return html`
    <div class="head"><span class="grow"></span>
      <${Help}><ol><li>Paste the web app's address.</li><li>Press Generate.</li>
        <li>Log in when the browser asks, then press Done.</li></ol><//></div>
    <form class="generate" onSubmit=${start}>
      <label class="vh" for="url">Web app URL</label>
      <input id="url" class="text" placeholder="https://" value=${url} onInput=${(e) => setUrl(e.target.value)} />
      <button class="btn primary" disabled=${busy}>Generate</button>
    </form>
    <div class="cards">
      ${(data?.sites || []).map((s) => html`
        <a class="card" href=${`#/site/${s.domain}`}>
          <div class="title">${s.title}</div><div class="domain">${s.domain}</div>
          <div class="foot"><span class="n">${s.operations} ops</span><${Status} state=${s.connection || "none"} /></div>
        </a>`)}
      ${(data?.running || []).map((j) => html`
        <a class="card" href=${`#/job/${j.id}`}>
          <div class="title">${j.domain.split(".")[0]}</div><div class="domain">${j.domain}</div>
          <div class="foot"><span class="n">${j.status}</span><${Status} state="busy" /></div>
        </a>`)}
    </div>
    ${data && !data.sites.length && !data.running.length && html`<div class="empty-state">No sites yet.</div>`}`;
}

/* ---------- site ---------- */

const TAB_HELP = {
  claude: html`<ol><li>Run the line in a terminal.</li><li>Start a new Claude Code session.</li>
    <li>Status red: press Reconnect and log in.</li></ol>
    <p class="muted">VS Code extension: add the MCP URL to <code>.mcp.json</code> in your project.</p>`,
  codex: html`<ol><li>Run the line in a terminal.</li><li>In Codex, type <code>/mcp</code> to check it loaded.</li><li>Status red: press Reconnect and log in.</li></ol>`,
  agent: html`<ol><li>MCP clients: add the MCP URL.</li><li>Anything else: POST JSON to the REST URL with the header.</li></ol>`,
  code: html`<ol><li>Download and unzip.</li><li>Call <code>run(session, …)</code> with your own cookies in <code>session</code>.</li></ol>`,
};

function Connect({ domain, jobId, onFinished }) {
  const [data] = usePoll(() => api(`/api/jobs/${jobId}`), 1500, [jobId]);
  const status = data?.job?.status;
  useEffect(() => { if (status === "connected" || status === "failed") onFinished(status); }, [status]);
  return html`<${Frame} url=${data?.live_url}>
    ${status === "needs_human" && html`<${Handoff} message="Log in, then press Done."
      onDone=${() => api(`/api/jobs/${jobId}/human-done`, { method: "POST" })} />`}
  <//>`;
}

function OpRow({ op }) {
  const mark = op.status === "verified" ? ["ok", "✓"] : ["idle", "–"];
  return html`<details>
    <summary><span class="mark ${mark[0]}" title=${op.status}>${mark[1]}</span>
      <span class="nm">${op.name}</span><span class="caret">▶</span></summary>
    <div class="opbody">
      ${op.description && html`<p>${op.description}</p>`}
      ${op.params.length > 0 && html`<table><thead><tr><th>param</th><th>type</th><th></th></tr></thead><tbody>
        ${op.params.map((p) => html`<tr><td><code>${p.name}${p.required ? "" : "?"}</code></td><td class="muted">${p.type}</td>
          <td class="muted">${p.description}</td></tr>`)}</tbody></table>`}
      ${op.returns?.length > 0 && html`<table><thead><tr><th>returns</th><th></th></tr></thead><tbody>
        ${op.returns.map((r) => html`<tr><td><code>${r.name}</code></td><td class="muted">${r.description}</td></tr>`)}</tbody></table>`}
      <pre class="code">${op.code}</pre>
    </div></details>`;
}

function Site({ domain }) {
  const [site, , refresh] = usePoll(() => api(`/api/sites/${domain}`), 10000, [domain]);
  const [tab, setTab] = useState("claude");
  const [connectJob, setConnectJob] = useState(null);
  if (!site) return html`<div class="muted">…</div>`;
  const conn = site.connection;
  const name = domain.split(".")[0];
  const mcp = conn?.mcp_url;
  const shownMcp = mcp && mcp.replace(conn.api_key, mask(conn.api_key));
  const probe = site.operations.find((o) => o.side_effect === "read" && !o.params.some((p) => p.required));
  const reads = site.operations.filter((o) => o.side_effect === "read");
  const writes = site.operations.filter((o) => o.side_effect !== "read");
  const startConnect = async () => setConnectJob((await api(`/api/sites/${domain}/connect`, { method: "POST" })).job_id);
  const lines = {
    claude: [`claude mcp add --transport http ${name} ${mcp}`, `claude mcp add --transport http ${name} ${shownMcp}`],
    codex: [`codex mcp add ${name} --url ${mcp}`, `codex mcp add ${name} --url ${shownMcp}`],
  };
  return html`
    <div class="head">
      <a class="back" href="#/" aria-label="Back">←</a><h1>${site.title}</h1>
      <span class="mono muted">${domain}</span>
      <span class="spacer"></span>
      <${Status} state=${connectJob ? "busy" : conn?.status || "none"} />
      ${!connectJob && html`<button class="btn" onClick=${startConnect}>${conn ? "Reconnect" : "Connect"}</button>`}
    </div>
    ${connectJob && html`<${Connect} domain=${domain} jobId=${connectJob}
        onFinished=${() => { setConnectJob(null); refresh(); }} />`}
    ${!connectJob && conn && html`
      <div class="row" style="margin-bottom:10px">
        <div class="tabs" role="tablist">
          ${[["claude", "Claude Code"], ["codex", "Codex"], ["agent", "Any agent"], ["code", "Code"]].map(([k, label]) => html`
            <button class="tab" role="tab" aria-selected=${tab === k} onClick=${() => setTab(k)}>${label}</button>`)}
        </div><span class="spacer"></span><${Help}>${TAB_HELP[tab]}<//>
      </div>
      ${lines[tab] && html`<${Copy} text=${lines[tab][0]} shown=${lines[tab][1]} />`}
      ${tab === "agent" && html`<div class="card kv">
        <span class="k">MCP</span><${Copy} text=${mcp} shown=${shownMcp} />
        <span class="k">REST</span><${Copy} text=${`${site.rest_base}/<operation>`} />
        <span class="k">Header</span><${Copy} text=${`Authorization: Bearer ${conn.api_key}`} shown=${`Authorization: Bearer ${mask(conn.api_key)}`} />
        ${probe && html`<span class="k">Try</span><${Copy}
          text=${`curl -X POST ${site.rest_base}/${probe.name} -H "Authorization: Bearer ${conn.api_key}" -d '{}'`}
          shown=${`curl -X POST ${site.rest_base}/${probe.name} -H "Authorization: Bearer ${mask(conn.api_key)}" -d '{}'`} />`}
      </div>`}
      ${tab === "code" && html`<div class="row">
        <a class="btn primary" href=${site.download_url}>Download</a>
        <a class="btn" href=${site.openapi_url} target="_blank" rel="noopener">OpenAPI</a></div>`}`}
    <div class="ops">
      <div><h2>Read · ${reads.length}</h2><div class="oplist">${reads.map((o) => html`<${OpRow} op=${o} />`)}</div></div>
      <div><h2>Write · ${writes.length}</h2><div class="oplist">${writes.map((o) => html`<${OpRow} op=${o} />`)}</div></div>
    </div>`;
}

/* ---------- generate ---------- */

const PHASES = [["login", "Login"], ["explore", "Explore"], ["generate", "Generate"], ["verify", "Verify"], ["publish", "Publish"]];
const OP_MARK = {
  verified: ["ok", "✓"], failed: ["bad", "✗"], excluded: ["idle", "–"],
  unverified_irreversible: ["idle", "–"], unverified_no_undo: ["idle", "–"],
};

function Generate({ id }) {
  const [data] = usePoll(() => api(`/api/jobs/${id}`), 1500, [id]);
  if (!data) return html`<div class="muted">…</div>`;
  const { job, phase } = data;
  const idx = phase === "done" ? PHASES.length : PHASES.findIndex(([k]) => k === phase);
  const running = ["starting", "needs_human", "exploring", "explored", "generating", "generated", "publishing"].includes(job.status);
  const ops = data.operations.filter((o) => o.name);
  const verified = ops.filter((o) => o.status === "verified").length;
  return html`
    <div class="head">
      <a class="back" href="#/" aria-label="Back">←</a><h1 class="mono">${data.domain}</h1>
      <div class="rail">${PHASES.map(([k, label], i) => html`
        ${i > 0 && html`<span class="sep">›</span>`}
        <span class="ph ${i < idx ? "done" : i === idx ? "now" : ""}">${label}</span>`)}</div>
      <span class="spacer"></span>
      ${!running && !["published"].includes(job.status) && html`<span class="status bad">${job.status}</span>`}
      <${Help}><ol><li>Log in inside the browser when asked, then press Done.</li><li>Everything after that runs on its own.</li></ol><//>
    </div>
    <div class="gen">
      <div>
        <${Frame} url=${data.live_url}>
          ${job.status === "needs_human" && html`<${Handoff} message=${job.status_detail || "Log in, then press Done."}
            onDone=${() => api(`/api/jobs/${id}/human-done`, { method: "POST" })} />`}
        <//>
        <div class="footer-bar">
          <span>step <b>${data.steps}</b></span><span><b>${fmtUsd(data.cost_usd)}</b></span>
          <span class="spacer"></span>
          ${job.status === "published" && html`<a class="btn primary" href=${`#/site/${data.domain}`}>Open →</a>`}
          ${running && html`<button class="btn small" onClick=${() => api(`/api/jobs/${id}/stop`, { method: "POST" })}>Stop</button>`}
        </div>
      </div>
      <div class="side">
        <div class="panel"><h2>Endpoints <span class="n">${data.endpoints.length}</span></h2>
          <ul>${[...data.endpoints].reverse().map((e) => html`<li><span></span>
            <span class="ep" title=${e.endpoint}>${e.endpoint.replace(/^(\w+) [^/]+/, "$1 ")}</span><span></span>
            <span class="why">${e.label}</span></li>`)}</ul></div>
        <div class="panel"><h2>Operations <span class="n">${verified} / ${ops.length}</span></h2>
          <ul>${ops.map((o) => {
            const [cls, sym] = OP_MARK[o.status] || ["busy", "↻"];
            return html`<li><span class="mark ${cls}">${sym}</span><span class="ep">${o.name}</span>
              <span class="att">${cls === "busy" && o.attempts ? `${o.attempts}/4` : ""}</span></li>`;
          })}</ul></div>
      </div>
    </div>`;
}

/* ---------- race ---------- */

const LANES = [
  ["frontier_browser", "browser", false],
  ["open_browser", "browser", false],
  ["skeleton_key", "Skeleton Key", true],
];
const shortModel = (m) => {
  if (!m) return "";
  const c = m.match(/^claude-([a-z]+)-(\d+)(?:-(\d+))?$/);
  if (c) return `Claude ${c[1][0].toUpperCase()}${c[1].slice(1)} ${c[2]}${c[3] ? "." + c[3] : ""}`;
  return m.replace(/^qwen/, "Qwen ").replace(/^glm/, "GLM ");
};
const plain = (t) => (t || "").replace(/\*\*|__|`/g, "");
const ratio = (a, b) => (a >= b ? `${(a / b).toFixed(1)}× cheaper` : `${(b / a).toFixed(1)}× pricier`);

function Race({ raceId }) {
  const [overview] = usePoll(() => api("/api/overview"), 0, []);
  const sites = (overview?.sites || []).filter((s) => s.connection === "active");
  const [domain, setDomain] = useState(null);
  const [presets, setPresets] = useState(null);
  const [task, setTask] = useState("");
  const [custom, setCustom] = useState(false);
  const d = domain || sites[0]?.domain;
  useEffect(() => {
    if (d) api(`/api/race/presets/${d}`).then((p) => { setPresets(p); setTask((t) => (raceId && t ? t : p.tasks[0])); });
  }, [d]);
  // A raced task that isn't a preset shows in the free-text box.
  useEffect(() => { if (presets && task && !presets.tasks.includes(task)) setCustom(true); }, [presets, task]);
  const run = async (e) => {
    e.preventDefault();
    const { race_id } = await api("/api/race", { method: "POST", body: { domain: d, task } });
    location.hash = `#/race/${race_id}`;
  };
  return html`
    <form class="row" onSubmit=${run}>
      <label class="vh" for="rsite">Site</label>
      <select id="rsite" class="text" value=${d} onChange=${(e) => setDomain(e.target.value)}>
        ${sites.map((s) => html`<option value=${s.domain}>${s.title}</option>`)}</select>
      <label class="vh" for="rtask">Task</label>
      ${custom ? html`<input id="rtask" class="text grow" value=${task} onInput=${(e) => setTask(e.target.value)} />`
        : html`<select id="rtask" class="text grow" value=${task}
            onChange=${(e) => (e.target.value === "__custom" ? (setCustom(true), setTask("")) : setTask(e.target.value))}>
            ${(presets?.tasks || []).map((t) => html`<option value=${t}>${t}</option>`)}
            <option value="__custom">Custom…</option></select>`}
      <button class="btn primary" disabled=${!d || !task}>Run</button>
      <${Help}><ol><li>Pick a task and press Run.</li><li>All lanes act as the same logged-in user.</li>
        <li>✓ / ✗ is checked against the site's real data.</li></ol><//>
    </form>
    ${raceId && html`<${RaceView} id=${raceId} frontier=${presets?.frontier} open=${presets?.open}
        onTask=${(t) => t && t !== task && setTask(t)} />`}
    ${!sites.length && overview && html`<div class="empty-state">Connect a site first.</div>`}`;
}

function RaceView({ id, frontier, open, onTask }) {
  const [data] = usePoll(() => api(`/api/race/${id}`), 1200, [id]);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 250); return () => clearInterval(t); }, []);
  useEffect(() => { if (data?.task) onTask(data.task); }, [data?.task, id]);
  if (!data) return null;
  const results = data.result?.results || {};
  const lanes = LANES.filter(([k]) => k !== "frontier_browser" || frontier || results[k] || data.contestants[k]);
  const elapsed = (now / 1000 - data.started);
  const best = results.skeleton_key;
  return html`
    <div class="lanes" style=${`--lanes:${lanes.length}`}>
      ${lanes.map(([k, kind, ours]) => {
        const c = data.contestants[k] || { steps: [] };
        const r = results[k];
        const model = r?.model || (k === "frontier_browser" ? frontier : open);
        const live = !r && c.live_url && data.status === "racing";
        return html`<div class="lane ${ours ? "ours" : ""}">
          <h3>${shortModel(model) || (k === "frontier_browser" ? "Frontier" : "Open model")} · ${kind}</h3>
          ${live ? html`<${Frame} url=${c.live_url} />`
            : html`<div class="log">${c.steps.map((s) => html`<div>${s.step}. ${s.action}${s.thought
                ? html` <span class="res">${s.thought}</span>` : ""}</div>`)}</div>`}
          <div class="metrics">
            <div class="metric"><div class="v">${r ? fmtS(r.seconds) : data.status === "racing" ? fmtS(elapsed) : "–"}</div><div class="l">time</div></div>
            <div class="metric"><div class="v">${fmtN(r?.model_tokens ?? c.model_tokens)}</div><div class="l">tokens</div></div>
            <div class="metric"><div class="v">${fmtUsd(r?.cost_usd ?? c.cost_usd)}</div><div class="l">cost</div></div>
            <div class="metric"><div class="v">${r ? (r.correct === true ? html`<span class="verdict ok">✓</span>`
              : r.correct === false ? html`<span class="verdict bad">✗</span>` : "?") : "…"}</div><div class="l">correct</div></div>
          </div>
          ${r && html`<div class="answer">${plain(r.answer) || html`<span class="muted">${r.error || "no answer"}</span>`}</div>`}
        </div>`;
      })}
    </div>
    ${best && html`<div class="footer-bar">${lanes.filter(([k]) => k !== "skeleton_key" && results[k]).map(([k]) => html`
      <span>vs ${shortModel(results[k].model)} · <b>${(results[k].seconds / Math.max(best.seconds, 0.1)).toFixed(1)}×</b> faster ·
        <b>${best.cost_usd && results[k].cost_usd ? ratio(results[k].cost_usd, best.cost_usd) : "–"}</b></span>`)}</div>`}
    ${data.status === "failed" && html`<div class="error">${data.detail}</div>`}`;
}

/* ---------- reconnect link (no site password) ---------- */

function Reconnect({ token }) {
  const [data, error] = usePoll(() => api(`/api/r/${token}`), 1500, [token]);
  if (error?.status === 410) return html`<div class="login"><h1>Link expired</h1></div>`;
  if (!data) return html`<div class="muted">…</div>`;
  if (data.status === "connected") return html`<div class="login"><h1>✓ ${data.domain} reconnected</h1></div>`;
  return html`<div class="head"><h1 class="mono">${data.domain}</h1><span class="spacer"></span>
      <${Help}><ol><li>Log in inside the browser.</li><li>Press Done.</li></ol><//></div>
    <${Frame} url=${data.live_url}>
      ${data.status === "needs_human" && html`<${Handoff} message="Log in, then press Done."
        onDone=${() => api(`/api/r/${token}/done`, { method: "POST" })} />`}
    <//>`;
}

/* ---------- app ---------- */

function App() {
  const { parts, query } = useRoute();
  const [page, arg] = parts;
  const bare = page === "login" || page === "r";
  let view;
  if (page === "login") view = html`<${Login} query=${query} />`;
  else if (page === "r") view = html`<${Reconnect} token=${arg} />`;
  else if (page === "site") view = html`<${Site} domain=${arg} />`;
  else if (page === "job") view = html`<${Generate} id=${arg} />`;
  else if (page === "race") view = html`<${Race} raceId=${arg} />`;
  else view = html`<${Home} />`;
  return html`
    ${!bare && html`<header class="bar"><a class="brand" href="#/">🗝 Skeleton Key</a>
      <nav><a href="#/" aria-current=${!page ? "page" : undefined}>Sites</a>
        <a href="#/race" aria-current=${page === "race" ? "page" : undefined}>Race</a></nav></header>`}
    <main>${view}</main>`;
}

render(html`<${App} />`, document.getElementById("app"));
