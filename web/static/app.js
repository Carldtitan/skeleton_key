import { html, render, useState, useEffect, useRef, useCallback } from "/static/vendor/preact-htm.js";

/* ---------- plumbing ---------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const publicPage = ["#/r/", "#/login", "#/skills"].some((p) => location.hash.startsWith(p));
  if (res.status === 401 && !publicPage) {
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
    const on = () => { setRoute(read()); window.scrollTo(0, 0); };
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
const plain = (t) => (t || "").replace(/\*\*|__|`/g, "");
const clock = (secs) => `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(Math.floor(secs % 60)).padStart(2, "0")}`;
const ago = (ts) => {
  if (!ts) return "–";
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
};

/* ---------- icons ---------- */

const KeyIcon = () => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 20 3M16 7l3 3M14 9l2 2"/></svg>`;
const HomeIcon = () => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M3 10.5 12 3l9 7.5V21a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/></svg>`;
const SitesIcon = () => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/></svg>`;
const SkillsIcon = () => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 17l.8 2.2L22 20l-2.2.8L19 23l-.8-2.2L16 20l2.2-.8z"/></svg>`;
const RaceIcon = () => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M3 12h4l3-8 4 16 3-8h4"/></svg>`;

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

const STATUS = {
  active: ["done", "connected"], expired: ["blocked", "expired"], none: ["queued", "not connected"],
  busy: ["live", "working"], published: ["done", "published"], failed: ["blocked", "failed"],
  stopped: ["queued", "stopped"], interrupted: ["blocked", "interrupted"], connected: ["done", "connected"],
};
function Status({ state, label }) {
  const [cls, word] = STATUS[state] || ["live", state];
  return html`<span class="status ${cls}"><i></i>${label || word}</span>`;
}

function Copy({ text, shown }) {
  const [done, setDone] = useState(false);
  const copy = async () => { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1400); };
  return html`<div class="copy"><code>${shown || text}</code>
    <button class="button small" onClick=${copy}>${done ? "Copied" : "Copy"}</button></div>`;
}

function Frame({ url, ended, children }) {
  return html`<div class="frame">
    ${url ? html`<iframe src=${url} title="Live browser" allow="clipboard-read; clipboard-write"></iframe>`
          : html`<div class="placeholder">${ended ? "Browser closed" : "Starting the browser…"}</div>`}
    ${children}
  </div>`;
}

function Handoff({ message, onDone }) {
  return html`<div class="handoff" role="alert"><span class="msg">${message}</span>
    <button class="button primary" onClick=${onDone}>Done</button></div>`;
}

function PageHeader({ eyebrow, title, mono, lede, children }) {
  return html`<header class="page-header">
    <div><span class="eyebrow">${eyebrow}</span><h1 class=${mono ? "mono" : ""}>${title}</h1>
      ${lede && html`<p class="lede">${lede}</p>`}</div>
    <div class="actions">${children}</div>
  </header>`;
}

function SectionHeading({ title, count, children }) {
  return html`<div class="section-heading"><h2>${title}</h2>
    <div class="row">${count != null && html`<span class="section-count">${count}</span>`}${children}</div></div>`;
}

/* ---------- landing / login ---------- */

function GateArt() {
  return html`<div class="gate-art" aria-hidden="true">
    <div class="chrome"><i></i><i></i><i></i></div>
    <div class="body">
      <div class="bars"><div class="bar" style="width:34%"></div><div class="bar" style="width:28%"></div></div>
      <div class="bar" style="width:84%;height:22px"></div>
      <div class="bars"><div class="bar" style="width:58%;height:42px"></div><div class="bar" style="width:30%;height:42px;background:#d6cabd"></div></div>
      <div class="picked" style="width:52%"><code>POST /rsvp</code></div>
      <div class="out"><div>rsvp_event(event_id)</div><div><span>verified ·</span> 0.6s</div></div>
    </div>
  </div>`;
}

function Login({ query }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    try { await api("/api/login", { method: "POST", body: { password: pw } }); location.hash = "#" + (query.get("next") || "/"); }
    catch (x) { setErr("Wrong password"); }
  };
  return html`<div class="gate">
    <div class="gate-brand"><span class="brand-mark"><${KeyIcon} /></span>Skeleton Key</div>
    <div class="gate-main">
      <div>
        <h1>Any web app.<br /><em>Now an API.</em></h1>
        <form onSubmit=${submit}>
          <label class="vh" for="pw">Password</label>
          <input id="pw" class="input large" type="password" placeholder="Password" autofocus value=${pw}
            onInput=${(e) => setPw(e.target.value)} />
          <button class="button dark large">Enter</button>
        </form>
        ${err && html`<div class="error">${err}</div>`}
      </div>
      <${GateArt} />
    </div>
  </div>`;
}

/* ---------- home ---------- */

function Home({ overview }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const start = async (e) => {
    e.preventDefault();
    if (!url.trim() || busy) return;
    setBusy(true);
    try { const { id } = await api("/api/jobs", { method: "POST", body: { site_url: url.trim() } }); location.hash = `#/job/${id}`; }
    finally { setBusy(false); }
  };
  const sites = overview?.sites || [], running = overview?.running || [];
  return html`
    <${PageHeader} eyebrow="Home" title="Generator"
        lede="Point it at a web app, log in once, get a tested API your agents can use.">
      <${Help}><ol><li>Paste the web app's address.</li><li>Press Generate.</li>
        <li>Log in when the browser asks, then press Done.</li></ol><//>
    <//>
    <form class="card generate" onSubmit=${start}>
      <div><label class="field-label" for="url">Web app</label>
        <input id="url" class="input large mono" placeholder="https://yourapp.com" value=${url} onInput=${(e) => setUrl(e.target.value)} /></div>
      <button class="button primary large" disabled=${busy}>Generate</button>
    </form>
    <section class="section">
      <${SectionHeading} title="Connected apps" count=${sites.length + running.length} />
      <div class="sites">
        ${sites.map((s) => html`<a class="site-card" href=${`#/site/${s.domain}`}>
          <span class="title">${s.title}</span><span class="domain">${s.domain}</span>
          <span class="foot"><span class="n">${s.operations} operations</span><${Status} state=${s.connection || "none"} /></span></a>`)}
        ${running.map((j) => html`<a class="site-card" href=${`#/job/${j.id}`}>
          <span class="title">${j.domain.split(".")[0]}</span><span class="domain">${j.domain}</span>
          <span class="foot"><span class="n">${j.status}</span><${Status} state="busy" /></span></a>`)}
      </div>
      ${overview && !sites.length && !running.length && html`<div class="empty">No apps yet</div>`}
    </section>`;
}

/* ---------- sites ---------- */

function Sites({ overview }) {
  const sites = overview?.sites || [], running = overview?.running || [];
  return html`
    <${PageHeader} eyebrow="Sites" title="Sites" />
    <div class="site-rows">
      ${sites.map((s) => html`<div class="site-row">
        <a class="tile" href=${`#/site/${s.domain}`} aria-hidden="true" tabindex="-1">${s.title[0]}</a>
        <div class="who"><a class="title" href=${`#/site/${s.domain}`}>${s.title}</a><span class="domain">${s.domain}</span></div>
        <dl class="facts">
          <div><dt>Operations</dt><dd>${s.operations}</dd></div>
          <div><dt>Verified</dt><dd>${s.verified}</dd></div>
          <div><dt>Read / Write</dt><dd>${s.reads} / ${s.writes}</dd></div>
          <div><dt>Session</dt><dd><${Status} state=${s.connection || "none"} /><small>${ago(s.checked)}</small></dd></div>
        </dl>
        <div class="acts"><a class="button secondary" href=${`#/site/${s.domain}`}>Open →</a></div>
      </div>`)}
      ${running.map((j) => html`<div class="site-row">
        <a class="tile" href=${`#/job/${j.id}`} aria-hidden="true" tabindex="-1">${j.domain[0].toUpperCase()}</a>
        <div class="who"><a class="title" href=${`#/job/${j.id}`}>${j.domain.split(".")[0]}</a><span class="domain">${j.domain}</span></div>
        <dl class="facts"><div><dt>Status</dt><dd><${Status} state="busy" label=${j.status} /></dd></div></dl>
        <div class="acts"><a class="button secondary" href=${`#/job/${j.id}`}>Watch →</a></div>
      </div>`)}
      ${overview && !sites.length && !running.length && html`<div class="empty">No sites yet</div>`}
    </div>`;
}

/* ---------- skills ---------- */

function LessonCard({ l, n, admin, onChange }) {
  const set = async (status) => { await api(`/api/skills/${l.id}`, { method: "POST", body: { status } }); onChange(); };
  return html`<li class="lesson">
    <span class="lesson-n">${String(n).padStart(2, "0")}</span>
    <div class="lesson-body">
      <div class="row"><span class="mono muted">${l.site} · ${l.date}</span><span class="spacer"></span>
        <span class="tag ${l.source}">${l.source}</span>
        <${Status} state=${l.status === "approved" ? "published" : l.status === "rejected" ? "failed" : "stopped"} label=${l.status} /></div>
      <p class="lesson-text">${l.lesson}</p>
      ${(l.failure || l.fix) && html`<details><summary>The mistake</summary>
        ${l.failure && html`<div class="lesson-part"><span class="eyebrow">What went wrong</span><p>${l.failure}</p></div>`}
        ${l.fix && html`<div class="lesson-part"><span class="eyebrow">Fix</span><pre class="code">${l.fix}</pre></div>`}
      </details>`}
      ${admin && l.status === "proposed" && html`<div class="row"><button class="button primary small" onClick=${() => set("approved")}>Approve</button>
        <button class="button secondary small" onClick=${() => set("rejected")}>Reject</button></div>`}
    </div>
  </li>`;
}

function Skills({ admin }) {
  const [data, , refresh] = usePoll(() => api("/api/skills"), 15000, []);
  const items = data?.lessons || [];
  return html`
    <${PageHeader} eyebrow="Skills" title="What the generator has learned">
      <${Help}><ol><li>Each lesson came from a real mistake, shown under “The mistake”.</li>
        <li>Approved lessons are read before every generation.</li><li>Auto lessons wait for approval.</li></ol><//>
    <//>
    <dl class="summary">
      <div><dt>Lessons</dt><dd>${items.length}</dd></div>
      <div><dt>Approved</dt><dd>${data?.approved ?? "–"}</dd></div>
      <div><dt>From sites</dt><dd>${data?.sites ?? "–"}</dd></div>
      <div><dt>Read before every run</dt><dd>✓</dd></div>
    </dl>
    <ol class="lessons section">${items.map((l, i) => html`<${LessonCard} l=${l} n=${i + 1} admin=${admin} onChange=${refresh} />`)}</ol>`;
}

/* ---------- site ---------- */

const TAB_HELP = {
  claude: html`<ol><li>Run the line in a terminal.</li><li>Start a new Claude Code session.</li></ol>
    <p>VS Code extension: add the MCP URL to <code>.mcp.json</code> in your project.</p>`,
  codex: html`<ol><li>Run the line in a terminal.</li><li>In Codex, type <code>/mcp</code> to check it loaded.</li></ol>`,
  agent: html`<ol><li>MCP clients: add the MCP URL.</li><li>Anything else: POST JSON to the REST URL with the header.</li></ol>`,
  code: html`<ol><li>Download and unzip.</li><li>Call <code>run(session, …)</code> with your own cookies in <code>session</code>.</li></ol>`,
};

function Connect({ jobId, onFinished }) {
  const [data] = usePoll(() => api(`/api/jobs/${jobId}`), 1500, [jobId]);
  const status = data?.job?.status;
  useEffect(() => { if (status === "connected" || status === "failed") onFinished(status); }, [status]);
  return html`<section class="section"><${Frame} url=${data?.live_url}>
    ${status === "needs_human" && html`<${Handoff} message="Log in, then press Done."
      onDone=${() => api(`/api/jobs/${jobId}/human-done`, { method: "POST" })} />`}
  <//></section>`;
}

function OpRow({ op }) {
  const mark = op.status === "verified" ? ["ok", "✓"] : ["idle", "–"];
  return html`<details>
    <summary><span class="mark ${mark[0]}" title=${op.status}>${mark[1]}</span>
      <span class="nm">${op.name}</span><span class="caret">▶</span></summary>
    <div class="opbody">
      ${op.description && html`<p>${op.description}</p>`}
      ${op.params.length > 0 && html`<table class="grid"><thead><tr><th>Param</th><th>Type</th><th></th></tr></thead><tbody>
        ${op.params.map((p) => html`<tr><td><code>${p.name}${p.required ? "" : "?"}</code></td><td class="muted">${p.type}</td>
          <td class="muted">${p.description}</td></tr>`)}</tbody></table>`}
      ${op.returns?.length > 0 && html`<table class="grid"><thead><tr><th>Returns</th><th></th></tr></thead><tbody>
        ${op.returns.map((r) => html`<tr><td><code>${r.name}</code></td><td class="muted">${r.description}</td></tr>`)}</tbody></table>`}
      <pre class="code">${op.code}</pre>
    </div></details>`;
}

function Site({ domain }) {
  const [site, , refresh] = usePoll(() => api(`/api/sites/${domain}`), 10000, [domain]);
  const [tab, setTab] = useState("claude");
  const [connectJob, setConnectJob] = useState(null);
  if (!site) return null;
  const conn = site.connection;
  const name = domain.split(".")[0];
  const mcp = conn?.mcp_url;
  const shownMcp = mcp && mcp.replace(conn.api_key, mask(conn.api_key));
  const probe = site.operations.find((o) => o.side_effect === "read" && !o.params.some((p) => p.required));
  const reads = site.operations.filter((o) => o.side_effect === "read");
  const writes = site.operations.filter((o) => o.side_effect !== "read");
  const verified = site.operations.filter((o) => o.status === "verified").length;
  const startConnect = async () => setConnectJob((await api(`/api/sites/${domain}/connect`, { method: "POST" })).job_id);
  const lines = {
    claude: [`claude mcp add --transport http ${name} ${mcp}`, `claude mcp add --transport http ${name} ${shownMcp}`],
    codex: [`codex mcp add ${name} --url ${mcp}`, `codex mcp add ${name} --url ${shownMcp}`],
  };
  return html`
    <a class="back" href="#/sites">← All sites</a>
    <${PageHeader} eyebrow=${domain} title=${site.title}>
      <${Status} state=${connectJob ? "busy" : conn?.status || "none"} />
      ${!connectJob && html`<button class="button secondary" onClick=${startConnect}>${conn ? "Reconnect" : "Connect"}</button>`}
    <//>
    <dl class="summary">
      <div><dt>Operations</dt><dd>${site.operations.length}</dd></div>
      <div><dt>Verified</dt><dd>${verified}</dd></div>
      <div><dt>Read</dt><dd>${reads.length}</dd></div>
      <div><dt>Write</dt><dd>${writes.length}</dd></div>
      <div><dt>Session checked</dt><dd>${ago(conn?.checked)}</dd></div>
    </dl>
    ${connectJob && html`<${Connect} jobId=${connectJob} onFinished=${() => { setConnectJob(null); refresh(); }} />`}
    ${!connectJob && conn && html`<section class="section">
      <${SectionHeading} title="Connect your agent"><${Help}>${TAB_HELP[tab]}<//><//>
      <div class="tablist" role="tablist">
        ${[["claude", "Claude Code"], ["codex", "Codex"], ["agent", "Any agent"], ["code", "Code"]].map(([k, label]) => html`
          <button class="tab" role="tab" aria-selected=${tab === k} onClick=${() => setTab(k)}>${label}</button>`)}
      </div>
      ${lines[tab] && html`<${Copy} text=${lines[tab][0]} shown=${lines[tab][1]} />`}
      ${tab === "agent" && html`<div class="kv">
        <span class="k">MCP</span><${Copy} text=${mcp} shown=${shownMcp} />
        <span class="k">REST</span><${Copy} text=${`${site.rest_base}/<operation>`} />
        <span class="k">Header</span><${Copy} text=${`Authorization: Bearer ${conn.api_key}`} shown=${`Authorization: Bearer ${mask(conn.api_key)}`} />
        ${probe && html`<span class="k">Try</span><${Copy}
          text=${`curl -X POST ${site.rest_base}/${probe.name} -H "Authorization: Bearer ${conn.api_key}" -d '{}'`}
          shown=${`curl -X POST ${site.rest_base}/${probe.name} -H "Authorization: Bearer ${mask(conn.api_key)}" -d '{}'`} />`}
        ${probe && html`<span class="k">PowerShell</span><${Copy}
          text=${`Invoke-RestMethod -Method Post -Uri ${site.rest_base}/${probe.name} -Headers @{Authorization="Bearer ${conn.api_key}"} -ContentType "application/json" -Body '{}'`}
          shown=${`Invoke-RestMethod -Method Post -Uri ${site.rest_base}/${probe.name} -Headers @{Authorization="Bearer ${mask(conn.api_key)}"} -ContentType "application/json" -Body '{}'`} />`}
      </div>`}
      ${tab === "code" && html`<div class="row" style="margin-top:14px">
        <a class="button primary" href=${site.download_url}>Download</a>
        <a class="button secondary" href=${site.openapi_url} target="_blank" rel="noopener">OpenAPI</a></div>`}
    </section>`}
    <section class="section">
      <${SectionHeading} title="Operations" count=${site.operations.length} />
      <div class="ops">
        <div class="oplist"><div class="oplist-head"><span class="eyebrow">Read</span><span class="section-count">${reads.length}</span></div>
          ${reads.map((o) => html`<${OpRow} op=${o} />`)}</div>
        <div class="oplist"><div class="oplist-head"><span class="eyebrow">Write</span><span class="section-count">${writes.length}</span></div>
          ${writes.map((o) => html`<${OpRow} op=${o} />`)}</div>
      </div>
    </section>`;
}

/* ---------- generate ---------- */

const PHASES = [["login", "Log in"], ["explore", "Explore"], ["generate", "Generate"], ["verify", "Verify"], ["publish", "Publish"]];
const OP_MARK = {
  verified: ["ok", "✓"], failed: ["bad", "✗"], excluded: ["idle", "–"],
  unverified_irreversible: ["idle", "–"], unverified_no_undo: ["idle", "–"],
};

function logRows(events, start) {
  const rows = [];
  for (const e of events) {
    const d = e.data || {};
    const at = clock(Math.max(0, e.ts - start));
    if (e.kind === "status") { if (typeof d.detail === "string" && d.detail) rows.push([at, d.status, d.detail]); }
    else if (e.kind === "step") rows.push([at, "explore", `${d.label || d.action}`]);
    else if (e.kind === "operation") rows.push([at, "verify", `${d.name}: ${d.status}${d.attempt ? ` (${d.attempt}/4)` : ""}`]);
    else if (e.kind === "published") rows.push([at, "publish", `${d.operations} operations published`]);
    else if (e.kind === "lesson") rows.push([at, "lesson", d.lesson]);
    else if (e.kind === "error") rows.push([at, "error", JSON.stringify(d).slice(0, 160), true]);
  }
  return rows;
}

function Generate({ id }) {
  const [data] = usePoll(() => api(`/api/jobs/${id}`), 1500, [id]);
  const logRef = useRef();
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = 1e9; }, [data?.events?.length]);
  if (!data) return null;
  const { job, phase } = data;
  const idx = phase === "done" ? PHASES.length : PHASES.findIndex(([k]) => k === phase);
  const running = ["starting", "needs_human", "exploring", "explored", "generating", "generated", "publishing"].includes(job.status);
  const ops = data.operations.filter((o) => o.name);
  const verified = ops.filter((o) => o.status === "verified").length;
  const rows = logRows(data.events, job.created);
  return html`
    <a class="back" href="#/">← Home</a>
    <${PageHeader} eyebrow="Generate" title=${data.domain}>
      <${Status} state=${running ? "busy" : job.status} />
      ${job.status === "published" && html`<a class="button primary" href=${`#/site/${data.domain}`}>Open</a>`}
      ${running && html`<button class="button secondary" onClick=${() => api(`/api/jobs/${id}/stop`, { method: "POST" })}>Stop</button>`}
      <${Help}><ol><li>Log in inside the browser when asked, then press Done.</li><li>Everything after that runs on its own.</li></ol><//>
    <//>
    <dl class="summary">
      <div><dt>Step</dt><dd>${data.steps}</dd></div>
      <div><dt>Endpoints</dt><dd>${data.endpoints.length}</dd></div>
      <div><dt>Operations</dt><dd>${verified} / ${ops.length}</dd></div>
      <div><dt>Cost</dt><dd>${fmtUsd(data.cost_usd)}</dd></div>
    </dl>
    <nav class="steps" aria-label="Progress">${PHASES.map(([k, label], i) => html`
      <div class="step ${i < idx ? "done" : i === idx ? "now" : ""}" aria-current=${i === idx ? "step" : undefined}>
        <small>0${i + 1}</small><span>${label}</span></div>`)}</nav>
    <section class="section gen">
      <${Frame} url=${running ? data.live_url : null} ended=${!running}>
        ${job.status === "needs_human" && html`<${Handoff} message=${job.status_detail || "Log in, then press Done."}
          onDone=${() => api(`/api/jobs/${id}/human-done`, { method: "POST" })} />`}
      <//>
      <div class="side">
        <div class="panel"><div class="panel-head"><span class="eyebrow">Endpoints</span><span class="section-count">${data.endpoints.length}</span></div>
          <ul>${[...data.endpoints].reverse().map((e) => html`<li><span></span>
            <span class="ep" title=${e.endpoint}>${e.endpoint.replace(/^(\w+) [^/]+/, "$1 ")}</span><span></span>
            <span class="why">${e.label}</span></li>`)}</ul></div>
        <div class="panel"><div class="panel-head"><span class="eyebrow">Operations</span><span class="section-count">${verified} / ${ops.length}</span></div>
          <ul>${ops.map((o) => {
            const [cls, sym] = OP_MARK[o.status] || ["busy", "↻"];
            return html`<li><span class="mark ${cls}">${sym}</span><span class="ep">${o.name}</span>
              <span class="att">${cls === "busy" && o.attempts ? `${o.attempts}/4` : ""}</span></li>`;
          })}</ul></div>
      </div>
    </section>
    <section class="section">
      <${SectionHeading} title="Log" />
      <div class="log" ref=${logRef}>${rows.map(([at, ph, msg, err]) => html`
        <div class="log-row ${err ? "error" : ""}"><span class="at">${at}</span><span class="ph">${ph}</span><span class="msg">${msg}</span></div>`)}</div>
    </section>`;
}

/* ---------- race ---------- */

const LANES = [["frontier_browser", "browser"], ["frontier_skeleton_key", "Skeleton Key"], ["open_browser", "browser"],
  ["skeleton_key", "Skeleton Key"]];
const shortModel = (m) => {
  if (!m) return "";
  const c = m.match(/^claude-([a-z]+)-(\d+)(?:-(\d+))?$/);
  if (c) return `Claude ${c[1][0].toUpperCase()}${c[1].slice(1)} ${c[2]}${c[3] ? "." + c[3] : ""}`;
  return m.replace(/^qwen/, "Qwen ").replace(/^glm/, "GLM ");
};

function Race({ raceId, overview }) {
  const sites = (overview?.sites || []).filter((s) => s.connection === "active");
  const [domain, setDomain] = useState(null);
  const [presets, setPresets] = useState(null);
  const [task, setTask] = useState("");
  const [custom, setCustom] = useState(false);
  const d = domain || sites[0]?.domain;
  useEffect(() => {
    if (d) api(`/api/race/presets/${d}`).then((p) => { setPresets(p); setTask((t) => (raceId && t ? t : p.tasks[0])); });
  }, [d]);
  useEffect(() => { if (presets && task && !presets.tasks.includes(task)) setCustom(true); }, [presets, task]);
  const run = async (e) => {
    e.preventDefault();
    const { race_id } = await api("/api/race", { method: "POST", body: { domain: d, task } });
    location.hash = `#/race/${race_id}`;
  };
  return html`
    <${PageHeader} eyebrow="Race" title="Same task, four ways">
      <${Help}><ol><li>Pick a task and press Run.</li><li>All lanes act as the same logged-in user.</li>
        <li>✓ / ✗ is checked against the site's real data.</li></ol><//>
    <//>
    <form class="card race-form" onSubmit=${run}>
      <div><label class="field-label" for="rsite">Site</label>
        <select id="rsite" class="input" value=${d} onChange=${(e) => setDomain(e.target.value)}>
          ${sites.map((s) => html`<option value=${s.domain}>${s.title}</option>`)}</select></div>
      <div><label class="field-label" for="rtask">Task</label>
        ${custom ? html`<input id="rtask" class="input" value=${task} onInput=${(e) => setTask(e.target.value)} />`
          : html`<select id="rtask" class="input" value=${task}
              onChange=${(e) => (e.target.value === "__custom" ? (setCustom(true), setTask("")) : setTask(e.target.value))}>
              ${(presets?.tasks || []).map((t) => html`<option value=${t}>${t}</option>`)}
              <option value="__custom">Custom…</option></select>`}</div>
      <button class="button primary large" disabled=${!d || !task}>Run</button>
    </form>
    ${raceId && html`<section class="section"><${RaceView} id=${raceId} frontier=${presets?.frontier} open=${presets?.open}
        onTask=${(t) => t && t !== task && setTask(t)} /></section>`}
    ${overview && !sites.length && html`<div class="empty section">Connect a site first</div>`}`;
}

function RaceView({ id, frontier, open, onTask }) {
  const [data] = usePoll(() => api(`/api/race/${id}`), 1200, [id]);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 250); return () => clearInterval(t); }, []);
  useEffect(() => { if (data?.task) onTask(data.task); }, [data?.task, id]);
  if (!data) return null;
  const results = data.result?.results || {};
  const lanes = LANES.filter(([k]) => !k.startsWith("frontier") || frontier || results[k] || data.contestants[k]);
  const elapsed = now / 1000 - data.started;
  return html`
    <div class="lanes" style=${`--lanes:${lanes.length}`}>
      ${lanes.map(([k, kind]) => {
        const c = data.contestants[k] || { steps: [] };
        const r = results[k] || (c.done && { ...c.done, correct: undefined, pending: true });
        const model = r?.model || (k.startsWith("frontier") ? frontier : open);
        const live = !r && c.live_url && data.status === "racing";
        const verdict = r && !r.pending ? (r.correct === true ? ["good", "✓"] : r.correct === false ? ["bad", "✗"] : ["", "?"]) : ["", "…"];
        return html`<div class="lane ${kind === "Skeleton Key" ? "ours" : ""}">
          <h3>${shortModel(model)} · ${kind}</h3>
          ${live ? html`<${Frame} url=${c.live_url} />`
            : html`<div class="log">${c.steps.map((s) => html`<div>${s.step}. ${s.action}${s.thought
                ? html` <span class="res">${s.thought}</span>` : ""}</div>`)}</div>`}
          <dl class="metrics">
            <div class="metric"><dt>Time</dt><dd>${r?.seconds != null ? fmtS(r.seconds) : data.status === "racing" ? fmtS(elapsed) : "–"}</dd></div>
            <div class="metric"><dt>Tokens</dt><dd>${fmtN(r?.model_tokens ?? c.model_tokens)}</dd></div>
            <div class="metric"><dt>Cost</dt><dd>${fmtUsd(r?.cost_usd ?? c.cost_usd)}</dd></div>
            <div class="metric"><dt>Correct</dt><dd class=${verdict[0]}>${verdict[1]}</dd></div>
          </dl>
          ${r && html`<div class="answer">${plain(r.answer) || html`<span class="muted">${r.error || "No answer"}</span>`}</div>`}
        </div>`;
      })}
    </div>
    ${data.status === "failed" && html`<div class="empty section">${data.detail}</div>`}`;
}

/* ---------- reconnect link (no site password) ---------- */

function Reconnect({ token }) {
  const [data, error] = usePoll(() => api(`/api/r/${token}`), 1500, [token]);
  if (error?.status === 410) return html`<div class="gate"><div class="gate-center"><h1>Link expired</h1></div></div>`;
  if (!data) return null;
  if (data.status === "connected")
    return html`<div class="gate"><div class="gate-center"><h1>✓ ${data.domain}</h1><${Status} state="connected" /></div></div>`;
  return html`<div class="gate">
    <div class="gate-brand"><span class="brand-mark"><${KeyIcon} /></span>Skeleton Key</div>
    <div class="page" style="width:100%;margin-top:28px">
      <${PageHeader} eyebrow="Reconnect" title=${data.domain} mono=${true}>
        <${Help}><ol><li>Log in inside the browser.</li><li>Press Done.</li></ol><//>
      <//>
      <${Frame} url=${data.live_url}>
        ${data.status === "needs_human" && html`<${Handoff} message="Log in, then press Done."
          onDone=${() => api(`/api/r/${token}/done`, { method: "POST" })} />`}
      <//>
    </div>
  </div>`;
}

/* ---------- app ---------- */

function Shell({ page, arg, children, overview, signedIn, skillsCount }) {
  const [jobDomain, setJobDomain] = useState(null);
  useEffect(() => { setJobDomain(null); if (page === "job") api(`/api/jobs/${arg}`).then((j) => setJobDomain(j.domain)).catch(() => {}); }, [page, arg]);
  const site = page === "site" ? overview?.sites?.find((s) => s.domain === arg)?.title || arg : page === "job" ? jobDomain : null;
  const signOut = async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); location.hash = "#/login"; };
  const cur = (p) => (p ? "page" : undefined);
  const count = (n) => (n ? html`<span class="nav-count">${n}</span>` : "");
  return html`<div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#/"><span class="brand-mark"><${KeyIcon} /></span>Skeleton Key</a>
      <div class="sidebar-context"><small>Site</small><strong>${site || "None selected"}</strong></div>
      <nav class="side-nav">
        <a class="nav-item" href="#/" aria-current=${cur(!page || page === "job")}><${HomeIcon} />Home</a>
        <a class="nav-item" href="#/sites" aria-current=${cur(page === "sites" || page === "site")}><${SitesIcon} />Sites${count(overview?.sites?.length)}</a>
        <a class="nav-item" href="#/skills" aria-current=${cur(page === "skills")}><${SkillsIcon} />Skills${count(skillsCount)}</a>
        <a class="nav-item" href="#/race" aria-current=${cur(page === "race")}><${RaceIcon} />Race</a>
      </nav>
      <div class="sidebar-bottom">${signedIn
        ? html`<button class="signout" onClick=${signOut}>Sign out</button>`
        : html`<a class="signout" href="#/login?next=/skills">Sign in</a>`}</div>
    </aside>
    <main class="app-main"><div class="page">${children}</div></main>
  </div>`;
}

function App() {
  const { parts, query } = useRoute();
  const [page, arg] = parts;
  const bare = page === "login" || page === "r";
  const [overview, overviewErr] = usePoll(() => (bare ? Promise.resolve(null) : api("/api/overview")), bare ? 0 : 5000, [bare]);
  const [skills] = usePoll(() => (bare ? Promise.resolve(null) : api("/api/skills")), bare ? 0 : 30000, [bare]);
  if (page === "login") return html`<${Login} query=${query} />`;
  if (page === "r") return html`<${Reconnect} token=${arg} />`;
  const signedIn = !!overview && !overviewErr;
  let view;
  if (page === "site") view = html`<${Site} domain=${arg} />`;
  else if (page === "sites") view = html`<${Sites} overview=${overview} />`;
  else if (page === "skills") view = html`<${Skills} admin=${signedIn} />`;
  else if (page === "job") view = html`<${Generate} id=${arg} />`;
  else if (page === "race") view = html`<${Race} raceId=${arg} overview=${overview} />`;
  else view = html`<${Home} overview=${overview} />`;
  return html`<${Shell} page=${page} arg=${arg} overview=${overview} signedIn=${signedIn}
    skillsCount=${skills?.lessons?.length}>${view}<//>`;
}

render(html`<${App} />`, document.getElementById("app"));
