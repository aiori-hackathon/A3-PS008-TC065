// Labs tab: saved labs (deploy / stop / use / delete), and the new-lab builder with a live preview.
// "Draw your own" lets the operator add routers and click pairs to link them.
import {createMap} from "/static/map.js";

const $ = id => document.getElementById(id);
const icon = n => `<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;

export function initBuilder({api, esc, onActive}) {
  const form = $("builder");
  let custom = {routers: ["R1", "R2", "R3"], links: [["R1", "R2"], ["R2", "R3"]]}, picked = null, timer = null;

  const preview = createMap($("preview"), {
    onSelect: name => {
      if (!name || form.template.value !== "custom") return;
      if (picked && picked !== name) {
        const i = custom.links.findIndex(([a, b]) => (a === picked && b === name) || (a === name && b === picked));
        if (i >= 0) custom.links.splice(i, 1); else custom.links.push([picked, name]);
        picked = null; preview.select(null); update();
      } else picked = picked === name ? null : name;
    },
  });

  function body() {
    const f = Object.fromEntries(new FormData(form));
    const name = /^[a-z0-9][a-z0-9-]{0,30}$/.test(f.name) ? f.name : "preview";
    const b = {name, baseline: f.baseline, routers: Number(f.routers), template: f.template};
    if (f.template === "custom") {
      const pos = preview.positions();
      b.spec = {...custom, layout: Object.fromEntries(Object.entries(pos).filter(([r]) => custom.routers.includes(r))
                                                       .map(([r, [x, y]]) => [r, [Math.round(x), Math.round(y)]]))};
      delete b.template;
    }
    return b;
  }

  async function update() {
    const t = form.template.value;
    $("draw-tools").hidden = t !== "custom";
    $("b-count").hidden = t === "custom" || t === "triangle";
    $("b-count-out").textContent = form.routers.value;
    $("b-hint").textContent = t === "custom" ? `${custom.routers.length} routers, ${custom.links.length} links` : (TEMPLATES[t] || "");
    try {
      const lab = await api("/api/labs/preview", body());
      $("b-err").textContent = "";
      preview.render(lab);
      $("b-addresses").innerHTML = `<table><thead><tr><th>Router</th><th>LAN</th><th>Links</th></tr></thead><tbody>${
        Object.entries(lab.routers).map(([r, i]) => `<tr><td>${esc(r)}</td><td class="mono">${esc(i.lans.join(", "))}</td><td class="mono">${
          esc(Object.entries(i.interfaces).filter(([n]) => n !== "lo").map(([n, a]) => `${n} ${a[0]}`).join(", "))}</td></tr>`).join("")}</tbody></table>`;
    } catch (e) { $("b-err").textContent = e.message; }
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(update, 200); };

  let TEMPLATES = {};
  form.addEventListener("input", later);
  $("add-router").onclick = () => {
    let k = custom.routers.length + 1; while (custom.routers.includes(`R${k}`)) k++;
    custom.routers.push(`R${k}`); update();
  };
  const remove = () => {
    const r = preview.selected; if (!r || !custom.routers.includes(r)) return;
    custom.routers = custom.routers.filter(x => x !== r); custom.links = custom.links.filter(l => !l.includes(r)); picked = null; update();
  };
  $("del-router").onclick = remove;
  $("preview").addEventListener("keydown", e => { if ((e.key === "Delete" || e.key === "Backspace") && form.template.value === "custom") remove(); });

  form.addEventListener("submit", async e => {
    e.preventDefault();
    const b = body(); if (b.name === "preview") { $("b-err").textContent = "Give the lab a name (lowercase letters, digits, dashes)."; return; }
    $("create-btn").disabled = true;
    try { await api("/api/labs", b); await loadLabs(); await deployLab(b.name); }
    catch (err) { $("b-err").textContent = err.message; }
    $("create-btn").disabled = false;
  });

  async function deployLab(name) {
    const log = $("job-log"); log.hidden = false; log.textContent = `Deploying ${name}…`;
    try { await api(`/api/labs/${name}/deploy`, {}); }
    catch (e) { log.textContent = e.message; return; }
    for (;;) {
      await new Promise(r => setTimeout(r, 1000));
      const job = await api(`/api/labs/${name}/job`);
      log.textContent = `Deploying ${name}\n` + job.log.map(l => "· " + l).join("\n");
      if (job.state !== "running") {
        if (job.state === "done") { log.textContent += `\n${name} is running and is now the active lab.`; await onActive(); }
        await loadLabs(); return;
      }
    }
  }

  async function act(name, action) {
    const log = $("job-log"); log.hidden = false;
    try {
      if (action === "deploy") return deployLab(name);
      if (action === "use") { await api(`/api/labs/${name}/use`, {}); await onActive(); log.textContent = `${name} is now the active lab.`; }
      if (action === "destroy") { log.textContent = `Stopping ${name}…`; await api(`/api/labs/${name}/destroy`, {}); log.textContent = `${name} stopped.`; await onActive(); }
      if (action === "delete" && confirm(`Delete lab ${name}? Its lab.yaml is removed; the audit history stays.`)) {
        await api(`/api/labs/${name}`, null, "DELETE"); log.textContent = `${name} deleted.`; }
    } catch (e) { log.textContent = e.message; }
    loadLabs();
  }

  async function loadLabs() {
    const data = await api("/api/labs");
    TEMPLATES = data.templates;
    if (!form.template.options.length) {
      form.template.innerHTML = Object.keys(TEMPLATES).map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("")
        + `<option value="custom">draw your own</option>`;
      form.template.value = "ring"; update();
    }
    const button = (lab, act, label, mark, cls, title) =>
      `<button class="btn small ${cls}" data-act="${act}" data-lab="${esc(lab)}" title="${title}">${icon(mark)}${label}</button>`;
    $("lab-list").innerHTML = data.labs.map(l => l.error
      ? `<div class="labrow"><span class="tile bad">${icon("alert")}</span><div><h3>${esc(l.name)}</h3><p class="err">${esc(l.error)}</p></div></div>`
      : `<div class="labrow${l.active ? " active" : ""}"><span class="tile">${icon("network")}</span>
       <div><h3>${esc(l.name)}${l.active ? `<span class="badge">In use</span>` : ""}</h3><p class="hint">${esc(l.template)} · ${l.routers} routers · ${l.links} links</p></div>
       <span class="status s-${l.running ? "running" : "stopped"}">${l.job === "running" ? "Starting" : l.running ? "Running" : "Stopped"}</span>
       <div class="actions">${l.running
          ? (l.active ? "" : button(l.name, "use", "Use this lab", "check", "", "Point the dashboard at this lab"))
            + button(l.name, "destroy", "Stop", "stop", "ghost", "Shut this lab's routers down")
          : button(l.name, "deploy", "Start", "play", "", "Start this lab's routers. The lab that is running is stopped first.")
            + (l.active ? "" : button(l.name, "delete", "Delete", "trash", "ghost danger", "Remove this saved lab"))}</div></div>`).join("");
    $("lab-list").querySelectorAll("button[data-act]").forEach(b => b.onclick = () => act(b.dataset.lab, b.dataset.act));
  }

  return {loadLabs, deployLab};
}
