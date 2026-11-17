// Dashboard: everything comes from the active lab via the API; nothing about the topology is hard-coded here.
import {createMap} from "/static/map.js";
import {initBuilder} from "/static/builder.js";
import {enter, materialize, slide} from "/static/motion.js";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const icon = n => `<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const when = t => new Date(t * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
const HINT = "Drag a router to move it, drag the background to pan, pinch to zoom. Click a router for its interfaces and routes.";
const KIND = {ADD_ROUTE: "plus-circle", REMOVE_ROUTE: "minus-circle", CONNECT: "link", REVERT_DRIFT: "undo"};
const PAGES = {
  change: ["New change", "Say what the network should do. Nothing reaches a router until it is checked and approved."],
  review: ["Review", "The safety checks, the exact config lines and the decision for one change."],
  history: ["History", "Every change that was requested, and what happened to it."],
  labs: ["Labs", "Saved practice networks. Start one, switch to it, or build a new one."]};
const STATUS = {PENDING_APPROVAL: "Waiting for approval", PENDING_CONFIRM: "Waiting for confirmation", SUCCESS: "Deployed", ROLLED_BACK: "Rolled back",
  ROLLBACK_FAILED: "Rollback failed", BLOCKED: "Blocked", INVALID: "Invalid", REJECTED: "Rejected"};

let NET = null, HEALTH = null, current = null, timer = null, busy = false, labKey = "", ME = null;

export async function api(path, body, method) {
  const r = await fetch(path, {method: method || (body ? "POST" : "GET"),
    headers: body ? {"Content-Type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined});
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("please sign in"); }
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `request failed (${r.status})`);
  return data;
}

/* ---------- map ---------- */
const map = createMap($("map"), {
  onSelect: showRouter,
  onMove: () => { $("save-layout").hidden = false; },
});
$("legend").textContent = HINT;
$("fit").onclick = () => map.fit();
$("zoom-in").onclick = () => map.zoom(1 / 1.3);
$("zoom-out").onclick = () => map.zoom(1.3);
$("full").onclick = () => document.fullscreenElement ? document.exitFullscreen() : $("mapcard").requestFullscreen?.();
$("save-layout").onclick = async () => {
  const pos = Object.fromEntries(Object.entries(map.positions()).map(([r, [x, y]]) => [r, [Math.round(x), Math.round(y)]]));
  try { await api(`/api/labs/${NET.name}/layout`, pos, "PUT"); $("save-layout").hidden = true; } catch (e) { $("legend").textContent = e.message; }
};

function linkOf(r, ifname) {
  const l = NET.links.find(l => (l.a === r && l.a_if === ifname) || (l.b === r && l.b_if === ifname));
  return l ? (l.a === r ? l.b : l.a) : "";
}

async function showRouter(r) {
  const info = NET?.routers[r], box = $("router-detail");
  if (!info) { box.hidden = true; return; }
  const fresh = box.hidden || box.dataset.router !== r;
  box.hidden = false; box.dataset.router = r;
  if (fresh) enter([box]);
  box.innerHTML = `<div class="cardhead"><span class="tile">${icon("router")}</span>
      <div class="grow"><h2>${esc(r)}</h2><p class="hint">${Object.keys(info.interfaces).length} interfaces · ${info.static.length} static route${info.static.length === 1 ? "" : "s"}</p></div>
      <button type="button" class="icon" id="close-router" aria-label="Close router details">${icon("x")}</button></div>
    <div class="blk"><h3>Interfaces</h3>
    <div class="well tablewrap"><table><thead><tr><th>Interface</th><th>Address</th><th>Goes to</th></tr></thead><tbody>
    ${Object.entries(info.interfaces).map(([i, a]) => `<tr><td class="mono">${esc(i)}</td><td class="mono">${esc(a.join(", "))}</td>
      <td>${i === "lo" ? "its LAN" : esc(linkOf(r, i))}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="blk"><h3>Static routes</h3>
    ${info.static.length ? `<pre class="config">${info.static.map(([p, nh]) => `ip route ${esc(p)} ${esc(nh)}`).join("\n")}</pre>` : `<p class="hint">No static routes.</p>`}</div>
    <div class="actions"><button class="btn ghost small" id="show-config">Show running config</button></div>
    <pre class="config" id="router-config" hidden></pre>`;
  $("close-router").onclick = () => map.select(null);
  $("show-config").onclick = async () => {
    try { const c = await api(`/api/routers/${encodeURIComponent(r)}/config`); $("router-config").textContent = c.config; $("router-config").hidden = false; }
    catch (e) { $("router-config").textContent = e.message; $("router-config").hidden = false; }
  };
}

function drawReach() {
  const names = Object.keys(NET.routers);
  const lans = [...new Set(NET.reachability.map(x => x.dst))];
  const by = Object.fromEntries(NET.reachability.map(x => [x.src + "|" + x.dst, x]));
  const ok = NET.reachability.filter(x => x.outcome === "DELIVERED").length;
  const total = NET.reachability.length, pct = total ? Math.round(ok / total * 100) : 100;
  $("reach-summary").textContent = `${ok} of ${total} paths from a router to a LAN work`;
  $("reach-pct").textContent = pct + "%";
  $("reach-bar").style.width = pct + "%"; $("reach-bar").className = pct === 100 ? "full" : "";
  if (names.length <= 12) {
    $("reach").innerHTML = `<thead><tr><th>From</th>${lans.map(l => `<th>${esc(l)}</th>`).join("")}</tr></thead><tbody>` +
      names.map(s => `<tr><td><b>${esc(s)}</b></td>${lans.map(l => { const x = by[s + "|" + l];
        if (!x) return `<td class="own" title="its own LAN">own</td>`;
        return x.outcome === "DELIVERED" ? `<td class="yes" title="${esc(x.path.join(" → "))}">yes</td>`
          : `<td class="no" title="${esc(x.outcome)}">no</td>`; }).join("")}</tr>`).join("") + "</tbody>";
  } else {
    const bad = NET.reachability.filter(x => x.outcome !== "DELIVERED");
    $("reach").innerHTML = `<thead><tr><th>From</th><th>To</th><th>Result</th></tr></thead><tbody>` +
      (bad.length ? bad.map(x => `<tr><td>${esc(x.src)}</td><td class="mono">${esc(x.dst)}</td><td class="no">${esc(x.outcome)}</td></tr>`).join("")
        : `<tr><td colspan="3" class="yes">Every router reaches every LAN.</td></tr>`) + "</tbody>";
  }
}

function setDrift() {
  const d = HEALTH?.drift, box = $("drift"), arriving = box.hidden && d?.drift;
  box.hidden = !d?.drift;
  if (arriving) materialize(box);
  if (!d?.drift) return;
  box.innerHTML = `${icon("alert")}<div><b>The routers were changed outside the agent.</b><p class="mono">${esc(d.summary)}</p>
    <p class="hint">New changes are blocked until this is resolved. Last approved state: ${esc(d.approved_by)}.</p>
    <div class="actions"><button class="btn small" id="drift-revert">${icon("undo")}Revert through approval</button>
    ${ME.role === "admin" ? `<button class="btn ghost small" id="drift-accept">Accept as the approved state</button>` : ""}</div></div>`;
  $("drift-revert").onclick = async () => { try { openChange(await api("/api/drift/revert", {})); } catch (e) { $("legend").textContent = e.message; } };
  if ($("drift-accept")) $("drift-accept").onclick = async () => {
    if (!confirm("Accept the current routes as approved? This is recorded in the audit log.")) return;
    try { await api("/api/drift/accept", {}); await refresh(); } catch (e) { $("legend").textContent = e.message; } };
}

function setHealth() {
  const n = Object.keys(NET.routers).length, links = NET.links.length, down = HEALTH?.bad_links?.length || 0, problems = HEALTH?.problems?.length || 0;
  $("map-sub").textContent = `${NET.name} · ${n} routers · ${links} links`;
  $("st-routers").textContent = n;
  $("st-links").textContent = HEALTH ? `${links - down} of ${links}` : "–";
  $("st-links-tile").className = "tile" + (HEALTH && !HEALTH.ok ? " bad" : "");
  $("st-audit").textContent = !HEALTH ? "–" : HEALTH.audit_intact ? "Intact" : "Tampered";
  $("st-audit-tile").className = "tile " + (HEALTH && !HEALTH.audit_intact ? "bad" : "ok");
  $("health-dot").className = "dot" + (!HEALTH ? "" : HEALTH.ok ? " ok" : " bad");
  $("health-text").textContent = !HEALTH ? "Checking…" : HEALTH.ok ? "All links up" : `${problems} problem${problems === 1 ? "" : "s"}`;
  $("health").title = HEALTH && !HEALTH.ok ? HEALTH.problems.join("\n") : "";
}

async function refresh() {
  if (busy) return;
  busy = true;
  try {
    NET = await api("/api/network");
    const key = NET.name + JSON.stringify(NET.links);
    map.render(NET);
    drawReach(); setHealth();
    if (key !== labKey) { labKey = key; fillForm(); loadExamples(); $("router-detail").hidden = true; }
    if (map.selected) showRouter(map.selected);
    HEALTH = await api("/api/health");
    map.setHealth(HEALTH); setHealth(); setDrift();
  } catch (e) {
    $("health-dot").className = "dot bad"; $("health-text").textContent = e.message;
  } finally { busy = false; }
}

/* ---------- tabs ---------- */
function moveThumb(instant) {
  const b = document.querySelector('.tabs [aria-selected="true"]');
  slide($("tab-thumb"), {x: b.offsetLeft, y: b.offsetTop, width: b.offsetWidth, height: b.offsetHeight}, instant);
}
addEventListener("resize", () => moveThumb(true));

function show(view, quiet) {
  const was = document.querySelector('.tabs [aria-selected="true"]').dataset.view;
  document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  ["change", "review", "history", "labs"].forEach(v => $("view-" + v).hidden = v !== view);
  $("layout").dataset.view = view;  // history and labs use the full width, without the map
  [$("page-title").textContent, $("page-sub").textContent] = PAGES[view];
  moveThumb();
  if (was !== view && !quiet) enter($("view-" + view).children);
  if (view === "history") loadHistory();
  if (view === "labs") builder.loadLabs();
}
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => show(b.dataset.view)));

/* ---------- change form ---------- */
const form = $("intent-form");
const opts = (list, sel) => list.map(([v, label]) => `<option value="${esc(v)}"${v === sel ? " selected" : ""}>${esc(label)}</option>`).join("");

function fillForm() {
  const names = Object.keys(NET.routers);
  for (const f of [form.router, form.source]) f.innerHTML = opts(names.map(n => [n, n]), names.includes(f.value) ? f.value : names[0]);
  $("lan-list").innerHTML = Object.values(NET.routers).flatMap(i => i.lans).map(l => `<option value="${esc(l.replace(/\.1\//, ".0/"))}">`).join("");
  $("target-list").innerHTML = names.map(n => `<option value="${esc(n)}">`).join("");
  $("ask-text").placeholder = `e.g. connect ${names[0]} to ${names[names.length - 1]}`;
  form.target.placeholder = names[names.length - 1];
  fillNextHop(); syncKind();
}
function fillNextHop() {
  const nbrs = NET.routers[form.router.value]?.neighbours || {};
  form.next_hop.innerHTML = opts(Object.entries(nbrs).map(([ip, n]) => [n, `${n} (${ip})`])) || `<option value="">no neighbours</option>`;
}
function syncKind() {
  const k = form.intent.value;
  form.querySelectorAll("[data-for]").forEach(e => e.hidden = !({route: k !== "CONNECT", add: k === "ADD_ROUTE", connect: k === "CONNECT"}[e.dataset.for]));
}
form.router.addEventListener("change", fillNextHop);
form.addEventListener("change", e => { if (e.target.name === "intent") syncKind(); });
function fromForm() {
  const f = Object.fromEntries(new FormData(form));
  const base = {intent: f.intent, reason: f.reason.trim()};
  if (f.intent === "CONNECT") return {...base, source: f.source, destination: f.target.trim(), both_ways: !!f.both_ways};
  const raw = {...base, router: f.router, destination: f.destination.trim()};
  if (f.intent === "ADD_ROUTE") raw.next_hop = f.next_hop;
  return raw;
}
function toForm(i) {
  form.intent.value = i.intent; syncKind();
  if (i.intent === "CONNECT") { form.source.value = i.source; form.target.value = i.destination; form.both_ways.checked = i.both_ways !== false; }
  else { form.router.value = i.router; fillNextHop(); form.destination.value = i.destination || "";
         if (i.next_hop) form.next_hop.value = Object.values(NET.routers[i.router].neighbours).includes(i.next_hop) ? i.next_hop
           : (NET.routers[i.router].neighbours[i.next_hop] || i.next_hop); }
  form.reason.value = i.reason || "";
}

async function submitIntent(raw, btn) {
  $("form-err").textContent = "";
  const label = btn.innerHTML; btn.disabled = true; btn.textContent = "Checking against the network model…";
  try { openChange(await api("/api/intent", raw)); }
  catch (err) { $("form-err").textContent = err.message; }
  btn.disabled = false; btn.innerHTML = label;
}
form.addEventListener("submit", e => { e.preventDefault(); submitIntent(fromForm(), $("validate-btn")); });

async function loadExamples() {
  const box = $("examples"); box.replaceChildren();
  try {
    for (const ex of await api("/api/examples")) {
      const b = document.createElement("button"); b.type = "button"; b.className = "chip"; b.textContent = ex.label;
      b.dataset.kind = ["add", "remove", "connect"].includes(ex.key) ? "ok" : "bad";
      b.title = `Expected: ${ex.expect}`; b.onclick = () => toForm(ex.intent); box.append(b);
    }
  } catch (e) { box.textContent = e.message; }
}

/* ---------- plain-language input ---------- */
const describe = i => !i ? "" : i.intent === "REVERT_DRIFT" ? "Revert changes made outside the agent" : i.intent === "CONNECT" ? `Connect ${i.source} ${i.both_ways === false ? "→" : "⇄"} ${i.destination}`
  : i.intent === "REMOVE_ROUTE" ? `Remove route to ${i.destination} on ${i.router}` : `On ${i.router}, send ${i.destination} via ${i.next_hop}`;

$("ask-form").addEventListener("submit", async e => {
  e.preventDefault();
  const text = $("ask-text").value.trim(); if (!text) return;
  const btn = $("ask-btn"); btn.disabled = true; $("ask-hint").textContent = "Reading your request…"; $("understood").replaceChildren();
  try {
    const res = await api("/api/ask", {text});
    $("ask-hint").textContent = res.intents.length ? `Understood by ${res.how}. Check it, then validate.${res.note ? " " + res.note : ""}` : res.note;
    res.intents.forEach(i => {
      const card = document.createElement("div"); card.className = "card";
      card.innerHTML = `<span class="tile">${icon(KIND[i.intent] || "edit")}</span><div><b>${esc(describe(i))}</b><span class="mono">${esc(JSON.stringify(i))}</span></div>
        <div class="actions"><button type="button" class="btn small">${icon("check")}Validate this</button>
        <button type="button" class="btn ghost small">Edit in form</button></div>`;
      const [go, edit] = card.querySelectorAll("button");
      go.onclick = () => submitIntent({...i, reason: i.reason || text.slice(0, 200)}, go);
      edit.onclick = () => toForm(i);
      $("understood").append(card);
    });
    enter($("understood").children);
  } catch (err) { $("ask-hint").textContent = err.message; }
  btn.disabled = false;
});

/* ---------- review ---------- */
function verdict(c) {
  const kind = c.status === "SUCCESS" ? "ok" : ["PENDING_APPROVAL", "PENDING_CONFIRM", "REJECTED", "ROLLED_BACK"].includes(c.status) ? "wait" : "bad";
  const title = {PENDING_APPROVAL: "All checks passed. Approve or reject this change.", SUCCESS: "Deployed and verified on the routers.",
    PENDING_CONFIRM: "Deployed and verified. Confirm it, or every router rolls back by itself.",
    ROLLED_BACK: "Deployment failed, so every router was restored from its snapshot.",
    ROLLBACK_FAILED: "Deployment failed and the rollback did not fully succeed. Check the routers now.",
    BLOCKED: "Blocked before it reached any router.", INVALID: "The request is not valid.",
    REJECTED: "Rejected. Nothing was changed on the routers."}[c.status] || c.status;
  const mark = kind === "ok" ? "check-circle" : ["PENDING_APPROVAL", "PENDING_CONFIRM"].includes(c.status) ? "hourglass" : c.status === "ROLLED_BACK" ? "undo" : kind === "wait" ? "x-circle" : "alert";
  return `<div class="verdict ${kind}">${icon(mark)}<div><h2>${esc(title)}</h2><p>${esc(c.reason)}</p></div></div>`;
}

// The five stages every change goes through: `done` stages are finished, the next one is current, `fail` is where it stopped.
const STEPS = ["Request", "Safety checks", "Approval", "Deploy", "Verify"];
function steps(done = 0, fail = -1) {
  return `<ol class="steps">${STEPS.map((s, i) => { const k = i === fail ? "fail" : i < done ? "done" : i === done ? "now" : "todo";
    return `<li class="${k}"><span class="pip">${k === "done" ? icon("check") : k === "fail" ? icon("x") : i + 1}</span>${s}</li>`; }).join("")}</ol>`;
}
function progress(c) {
  const ev = c.events || [], approved = ev.some(e => e.stage === "approval" && e.result === "APPROVED");
  const afterDeploy = ev.some(e => e.stage === "health") ? 4 : 3;
  const fail = {INVALID: 0, BLOCKED: approved ? 3 : 1, REJECTED: 2, ROLLED_BACK: afterDeploy, ROLLBACK_FAILED: afterDeploy}[c.status];
  return c.status === "SUCCESS" ? steps(5) : c.status === "PENDING_APPROVAL" ? steps(2) : c.status === "PENDING_CONFIRM" ? steps(4)
    : steps(fail ?? 0, fail ?? -1);
}

function traceChange(c) {
  map.clearTraces(); $("legend").textContent = HINT;
  const failed = (c.report || []).find(r => !r.ok && (r.check === "loop" || r.check === "blackhole"));
  if (failed) {
    const m = failed.check === "loop" ? failed.detail.match(/: ([^;()]+→[^;()]+)/) : failed.detail.match(/\(([^()]+→[^()]+)\)/);
    if (m) {
      const path = m[1].split("→").map(s => s.trim());
      map.path(path, failed.check === "loop" ? "loop" : "hole");
      $("legend").textContent = failed.check === "loop" ? `Traffic would circle forever: ${path.join(" → ")}` : `Traffic would be dropped at ${path[path.length - 1]}: ${path.join(" → ")}`;
      return;
    }
  }
  const owner = ip => Object.entries(NET.routers).find(([, r]) => Object.values(r.interfaces).flat().some(a => a.startsWith(ip + "/")))?.[0];
  let i = 0;
  for (const [r, lines] of Object.entries(c.candidate || {})) for (const line of lines) {
    const parts = line.split(" "), nh = parts[parts.length - 1], to = owner(nh);
    if (to) map.hop(r, to, line.startsWith("no ") ? "gone" : "new", i++);
  }
  if (i) $("legend").textContent = `Green arrows: routes this change adds${i > 1 ? ` on ${Object.keys(c.candidate).length} routers` : ""}. Grey dashed: routes it removes.`;
}

async function openChange(c) {
  clearInterval(timer);
  const full = await api(`/api/changes/${c.id}`).catch(() => c);
  const arriving = current?.id !== full.id || current?.status !== full.status;
  current = full; show("review", true);
  const top = $("view-review").getBoundingClientRect().top;  // one-column layouts: bring the review into view
  if (arriving && (top < 60 || top > innerHeight * 0.6)) $("view-review").scrollIntoView({behavior: "smooth"});
  if (NET && full.lab === NET.name) traceChange(full);
  const pending = full.status === "PENDING_APPROVAL", confirming = full.status === "PENDING_CONFIRM";
  const mine = (full.intent || {}).requested_by === ME.name, canApprove = ME.role !== "operator";
  const names = NET ? Object.keys(NET.routers) : [];
  $("review-empty").hidden = true; const box = $("review"); box.hidden = false;
  const intent = full.intent || {}, shown = full.normalized || intent, report = full.report || [], diffs = Object.entries(full.diff || {});
  box.innerHTML = `
    <div class="blk"><div class="cardhead"><span class="tile">${icon(KIND[shown.intent] || "edit")}</span>
        <div class="grow"><h2>${esc(describe(shown))}</h2><p class="hint">${esc(intent.reason)}</p></div>
        <span class="status s-${esc(full.status)}">${esc(STATUS[full.status] || full.status)}</span></div>
      <div class="tags"><span class="tagline mono">#${esc(full.id)}</span><span class="tagline">${icon("layers")}${esc(full.lab)}</span>
        <span class="tagline">${icon("user")}${esc(intent.requested_by)}</span><span class="tagline">${icon("clock")}${esc(when(full.created_at))}</span></div></div>
    ${progress(full)}
    ${verdict(full)}
    ${report.length ? `<div class="blk"><div class="sub"><h3>Safety checks in the network model</h3><span class="hint">${report.filter(r => r.ok).length} of ${report.length} passed</span></div>
      <ul class="checks">${report.map(r => `<li class="${r.ok ? "" : "fail"}">${icon(r.ok ? "check-circle" : "x-circle")}<b>${esc(r.check)}</b><span>${esc(r.detail)}</span></li>`).join("")}</ul></div>` : ""}
    ${diffs.length ? `<div class="blk"><h3>Config change</h3>${diffs.map(([r, lines]) => `<div class="diffbox"><div class="diffhead">${icon("router")}${esc(r)}</div>
      <pre class="diff">${lines.map(l => `<div class="${l[0] === "+" ? "add" : l[0] === "-" ? "del" : ""}">${esc(l)}</div>`).join("")}</pre></div>`).join("")}</div>` : ""}
    ${pending && (mine || !canApprove) ? `<p class="note">${icon("hourglass")}<span>Waiting for an approver. <span id="countdown"></span>.
      ${mine ? "You requested this change, so someone else has to approve it." : "Your role cannot approve changes."}</span></p>` : ""}
    ${pending && !mine && canApprove ? `<form class="decide" id="decide">
      <div class="sub"><h3>Your decision as ${esc(ME.name)}</h3><span class="hint" id="countdown"></span></div>
      <div class="row"><label><span class="lab">Reason</span><input name="reason" placeholder="Optional"></label>
        <label><span class="lab">Deploy as <small>confirmed commit, RFC 6241</small></span><select name="confirm_timeout">
          <option value="">Permanent change</option><option value="120">Confirmed commit, undo in 2 min</option>
          <option value="300">Confirmed commit, undo in 5 min</option><option value="600">Confirmed commit, undo in 10 min</option></select></label></div>
      <div class="row"><label><span class="lab">Failure test <small>forces a failure to show the rollback</small></span>
        <select name="simulate_failure"><option value="">Off, deploy normally</option>
        <option value="deploy">Router rejects its config mid-deploy</option><option value="health">A link goes down after deploy</option></select></label>
        <label><span class="lab">On router</span><select name="failure_router">${opts(names.map(n => [n, n]), names[names.length - 1])}</select></label></div>
      <div class="actions"><button class="btn go lg" type="submit" value="approve">${icon("check")}Approve and deploy</button>
        <button class="btn ghost lg danger" type="submit" value="reject">${icon("x")}Reject</button></div>
      <p class="err" id="decide-err" role="alert"></p></form>` : ""}
    ${confirming && (mine || !canApprove) ? `<p class="note">${icon("hourglass")}<span>Live on the routers, waiting for an approver to confirm it.
      <span id="countdown"></span>. ${mine ? "You requested this change, so someone else has to confirm it." : "Your role cannot confirm changes."}</span></p>` : ""}
    ${confirming && !mine && canApprove ? `<form class="decide" id="confirm-form">
      <div class="sub"><h3>Keep this change?</h3><span class="hint" id="countdown"></span></div>
      <p class="hint">It is live and the pings passed. If nobody confirms it in time, every router is restored from the snapshot, like a NETCONF confirmed commit.</p>
      <div class="actions"><button class="btn go lg" type="submit" value="confirm">${icon("check")}Confirm and keep</button>
        <button class="btn ghost lg danger" type="submit" value="cancel">${icon("undo")}Roll back now</button></div>
      <p class="err" id="decide-err" role="alert"></p></form>` : ""}
    <div class="blk"><h3>Audit trail</h3><ol class="trail">${(full.events || []).map(e => `<li><span class="pt s-${esc(e.result)}"></span>
      <div><b>${esc(e.stage)}</b><span class="status mini s-${esc(e.result)}">${esc(e.result)}</span><p>${esc(e.detail)}</p></div>
      <time class="mono">${esc(new Date(e.ts).toLocaleTimeString())}</time></li>`).join("")}</ol></div>`;
  if (arriving) enter(box.children);
  if (pending || confirming) {
    const deadline = pending ? full.expires_at : full.confirm_by, what = pending ? "auto-rejects" : "rolls back";
    const tick = () => { const s = Math.max(0, Math.round(deadline - Date.now() / 1000));
      if ($("countdown")) $("countdown").textContent = `${what} in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      if (s === 0) { clearInterval(timer); openChange(full); } };
    tick(); timer = setInterval(tick, 1000);
    $("decide")?.addEventListener("submit", decide);
    $("confirm-form")?.addEventListener("submit", decide);
  }
  loadHistory();
}

async function decide(e) {
  e.preventDefault();
  const action = e.submitter.value, f = Object.fromEntries(new FormData(e.target));
  const buttons = e.target.querySelectorAll("button"); buttons.forEach(b => b.disabled = true);
  const label = e.submitter.innerHTML;
  e.submitter.textContent = {approve: "Pre-flight, backup, deploy, pings…", reject: "Rejecting…", confirm: "Confirming…", cancel: "Restoring every router…"}[action];
  try {
    const c = await api(`/api/changes/${current.id}/${action}`, ["confirm", "cancel"].includes(action) ? {} : {reason: f.reason,
      simulate_failure: f.simulate_failure || null, failure_router: f.simulate_failure ? f.failure_router : null,
      confirm_timeout: f.confirm_timeout ? Number(f.confirm_timeout) : null});
    await refresh(); loadExamples(); openChange(c);
  } catch (err) {
    $("decide-err").textContent = err.message; buttons.forEach(b => b.disabled = false); e.submitter.innerHTML = label;
  }
}

/* ---------- history ---------- */
let HIST = [], hFilter = "all";
const GROUPS = {  // label, dot colour, which statuses
  all: ["All", "none", () => true], waiting: ["Waiting", "wait", s => s.startsWith("PENDING")], deployed: ["Deployed", "ok", s => s === "SUCCESS"],
  stopped: ["Stopped", "bad", s => ["BLOCKED", "INVALID", "REJECTED"].includes(s)], rolled: ["Rolled back", "wait", s => s.startsWith("ROLL")]};

function drawHistory() {
  const q = $("h-search").value.trim().toLowerCase();
  $("h-filters").innerHTML = Object.entries(GROUPS).map(([k, [label, kind, test]]) => `<button type="button" class="chip" data-kind="${kind}" data-f="${k}"
    aria-pressed="${k === hFilter}">${label}<b>${HIST.filter(c => test(c.status)).length}</b></button>`).join("");
  const rows = HIST.filter(c => GROUPS[hFilter][2](c.status)
    && (!q || [c.id, c.lab, describe(c.normalized || c.intent), c.intent?.requested_by].join(" ").toLowerCase().includes(q)));
  $("history").innerHTML = rows.length ? rows.map(c => `<tr data-id="${esc(c.id)}"><td><span class="idtag">#${esc(c.id)}</span></td>
    <td class="what">${esc(describe(c.normalized || c.intent))}<small>${esc(c.lab)} · requested by ${esc(c.intent?.requested_by)}</small></td>
    <td class="when">${esc(when(c.created_at))}</td><td><span class="status s-${esc(c.status)}">${esc(STATUS[c.status] || c.status)}</span></td></tr>`).join("")
    : `<tr><td colspan="4"><div class="blank"><span class="tile">${icon("clock")}</span><b>${HIST.length ? "Nothing matches" : "No changes yet"}</b>
       <p>${HIST.length ? "Try another filter or search." : "Start with New change."}</p></div></td></tr>`;
  $("history").querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => openChange({id: tr.dataset.id}));
}
$("h-filters").addEventListener("click", e => { const b = e.target.closest("[data-f]"); if (b) { hFilter = b.dataset.f; drawHistory(); } });
$("h-search").addEventListener("input", drawHistory);
$("st-pending-btn").onclick = () => { hFilter = "waiting"; show("history"); };

async function loadHistory() {
  HIST = await api("/api/changes").catch(() => []);
  const pending = HIST.filter(c => c.status.startsWith("PENDING")).length;
  $("pending-count").hidden = !pending; $("pending-count").textContent = pending; $("st-pending").textContent = pending;
  drawHistory();
}

/* ---------- labs ---------- */
const builder = initBuilder({api, esc, onActive: async () => { labKey = ""; await refresh(); await loadLabSelect(); }});

async function loadLabSelect() {
  const {labs} = await api("/api/labs");
  $("lab-select").innerHTML = labs.filter(l => !l.error).map(l => `<option value="${esc(l.name)}"${l.active ? " selected" : ""}>${esc(l.name)}${l.running ? "" : " (stopped)"}</option>`).join("");
}
$("lab-select").addEventListener("change", async e => {
  const name = e.target.value;
  const {labs} = await api("/api/labs");
  const lab = labs.find(l => l.name === name);
  if (lab?.running) { await api(`/api/labs/${name}/use`, {}); labKey = ""; await refresh(); await loadLabSelect(); return; }
  if (confirm(`Lab ${name} is not running. Deploy it now? This stops the lab that is running.`)) { show("labs"); builder.deployLab(name); }
  else loadLabSelect();
});

/* ---------- people (admin) ---------- */
async function loadPeople() {
  $("people").hidden = ME.role !== "admin";
  if (ME.role !== "admin") return;
  const list = await api("/api/users").catch(() => []);
  $("user-list").innerHTML = list.map(u => `<tr><td><b>${esc(u.name)}</b></td><td><span class="badge">${esc(u.role)}</span></td></tr>`).join("");
}
$("user-form").addEventListener("submit", async e => {
  e.preventDefault(); $("user-err").textContent = "";
  try { await api("/api/users", Object.fromEntries(new FormData(e.target))); e.target.reset(); loadPeople(); }
  catch (err) { $("user-err").textContent = err.message; }
});

/* ---------- theme ---------- */
$("theme").onclick = () => {
  const root = document.documentElement, t = root.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("theme", t);
  const apply = () => { root.dataset.theme = t; };
  document.startViewTransition ? document.startViewTransition(apply) : apply();  // cross-fade, no brightness jump
};

/* ---------- sign in ---------- */
let started = false;
function showLogin() {
  const arriving = $("login").hidden;
  $("app").hidden = true; $("login").hidden = false; $("login-form").name.focus();
  if (arriving) materialize($("login-form"));
}
$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); $("login-err").textContent = "";
  try { await api("/api/login", Object.fromEntries(new FormData(e.target))); e.target.password.value = ""; start(); }
  catch (err) { $("login-err").textContent = err.message; }
});
$("signout").onclick = async () => { await api("/api/logout", {}).catch(() => {}); location.reload(); };

async function start() {
  try { ME = await api("/api/me"); } catch (e) { return; }
  $("login").hidden = true; $("app").hidden = false;
  $("whoami").textContent = ME.name; $("role").textContent = ME.role; $("avatar").textContent = ME.name[0].toUpperCase();
  [$("page-title").textContent, $("page-sub").textContent] = PAGES[$("layout").dataset.view];
  $("guide").innerHTML = steps(0);
  moveThumb(true);
  enter(document.querySelectorAll(".pagehead > *, .mapcol > *, main > section > *"), {gap: 0.05});
  labKey = "";
  await loadLabSelect();
  await refresh();
  loadHistory(); loadPeople();
  if (!started) { started = true; setInterval(() => { if (!document.hidden && !$("app").hidden) refresh(); }, 5000); }
}
start();
