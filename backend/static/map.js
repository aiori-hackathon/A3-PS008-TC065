// Network map for any topology: automatic layout (by template, saved positions, or force-directed),
// drag to rearrange, pinch or ctrl+wheel to zoom, drag background to pan, health colouring and path overlays.
import {tween} from "/static/motion.js";

const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.append(e);
  return e;
};
const W = 1000, H = 700;

export function autoLayout(lab) {
  const names = Object.keys(lab.routers), n = names.length;
  const saved = lab.layout || {};
  if (names.every(r => saved[r])) return Object.fromEntries(names.map(r => [r, [...saved[r]]]));
  const adj = Object.fromEntries(names.map(r => [r, new Set()]));
  lab.links.forEach(l => { adj[l.a].add(l.b); adj[l.b].add(l.a); });
  const circle = (list, cx = W / 2, cy = H / 2, rad = Math.min(300, 70 + list.length * 22)) =>
    Object.fromEntries(list.map((r, i) => [r, [cx + rad * Math.cos(-Math.PI / 2 + 2 * Math.PI * i / list.length),
                                               cy + rad * Math.sin(-Math.PI / 2 + 2 * Math.PI * i / list.length)]]));
  let pos;
  const t = lab.template;
  if (n === 1) pos = {[names[0]]: [W / 2, H / 2]};
  else if (t === "line") {
    const per = Math.min(n, 7);
    pos = Object.fromEntries(names.map((r, i) => { const row = Math.floor(i / per), col = row % 2 ? per - 1 - (i % per) : i % per;
      return [r, [120 + col * (760 / Math.max(per - 1, 1)), 140 + row * 150]]; }));
  } else if (t === "star") {
    const hub = names.reduce((a, b) => adj[b].size > adj[a].size ? b : a);
    pos = {...circle(names.filter(r => r !== hub)), [hub]: [W / 2, H / 2]};
  } else if (t === "tree") {
    const depth = {[names[0]]: 0}, q = [names[0]], levels = [[names[0]]];
    while (q.length) { const u = q.shift(); for (const v of names.filter(x => adj[u].has(x))) if (!(v in depth)) {
      depth[v] = depth[u] + 1; (levels[depth[v]] ||= []).push(v); q.push(v); } }
    names.filter(r => !(r in depth)).forEach(r => (levels[levels.length] ||= []).push(r));
    pos = {};
    levels.forEach((lv, d) => lv.forEach((r, i) => pos[r] = [(i + 1) * W / (lv.length + 1), 90 + d * Math.min(150, 560 / Math.max(levels.length - 1, 1))]));
  } else if (["ring", "triangle", "mesh", "partial-mesh"].includes(t)) pos = circle(names);
  else pos = force(names, lab.links, circle(names));
  for (const r of names) if (saved[r]) pos[r] = [...saved[r]];
  return pos;
}

function force(names, links, start) {
  // Fruchterman-Reingold, enough for a few dozen routers
  const pos = Object.fromEntries(names.map(r => [r, [...start[r]]]));
  const k = Math.sqrt(W * H / names.length) * 0.6;
  for (let it = 0, temp = 120; it < 250; it++, temp *= 0.98) {
    const d = Object.fromEntries(names.map(r => [r, [0, 0]]));
    for (let i = 0; i < names.length; i++) for (let j = i + 1; j < names.length; j++) {
      const a = names[i], b = names[j], dx = pos[a][0] - pos[b][0], dy = pos[a][1] - pos[b][1], dist = Math.hypot(dx, dy) || 0.1;
      const f = k * k / dist; d[a][0] += dx / dist * f; d[a][1] += dy / dist * f; d[b][0] -= dx / dist * f; d[b][1] -= dy / dist * f;
    }
    for (const l of links) {
      const dx = pos[l.a][0] - pos[l.b][0], dy = pos[l.a][1] - pos[l.b][1], dist = Math.hypot(dx, dy) || 0.1, f = dist * dist / k;
      d[l.a][0] -= dx / dist * f; d[l.a][1] -= dy / dist * f; d[l.b][0] += dx / dist * f; d[l.b][1] += dy / dist * f;
    }
    for (const r of names) {
      const len = Math.hypot(...d[r]) || 1;
      pos[r][0] = Math.min(W - 60, Math.max(60, pos[r][0] + d[r][0] / len * Math.min(len, temp)));
      pos[r][1] = Math.min(H - 60, Math.max(60, pos[r][1] + d[r][1] / len * Math.min(len, temp)));
    }
  }
  return pos;
}

export function createMap(svg, {onSelect = () => {}, onMove = () => {}, editable = false} = {}) {
  let lab = null, pos = {}, selected = null, health = null, view = null, dragging = null, anim = null;
  const gLinks = el("g", {}, svg), gTrace = el("g", {}, svg), gNodes = el("g", {}, svg);
  const defs = el("defs", {}, svg);
  for (const kind of ["loop", "hole", "new", "gone"]) {
    const m = el("marker", {id: `arrow-${kind}-${svg.id}`, viewBox: "0 0 10 10", refX: 7, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse"}, defs);
    el("path", {d: "M0 0L10 5L0 10z", class: `head ${kind}`}, m);
  }
  const size = () => { const n = lab ? Object.keys(lab.routers).length : 1; return n <= 10 ? 38 : n <= 20 ? 30 : 24; };

  function setView(v) { view = v; svg.setAttribute("viewBox", v.join(" ")); }
  // Spring the view to its target from wherever it is now; any pointer input stops it at once.
  function glide(to, arrive) {
    anim?.stop();
    const from = arrive ? [to[0] - to[2] * .07, to[1] - to[3] * .07, to[2] * 1.14, to[3] * 1.14] : view;
    if (!from) return setView(to);
    anim = tween(t => { setView(from.map((v, i) => v + (to[i] - v) * t)); if (arrive) gLinks.style.opacity = gNodes.style.opacity = t; });
  }
  function fit(arrive) {
    const pts = Object.values(pos); if (!pts.length) return setView([0, 0, W, H]);
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]), pad = 90;
    const x0 = Math.min(...xs) - pad, y0 = Math.min(...ys) - pad - 40, w = Math.max(...xs) - x0 + pad, h = Math.max(...ys) - y0 + pad + 40;  // 40: room for LAN tags above and below
    const scale = Math.max(w / W, h / H, 0.55);
    glide([x0 + w / 2 - W * scale / 2, y0 + h / 2 - H * scale / 2, W * scale, H * scale], arrive === true);
  }
  const toSvg = e => { const p = svg.createSVGPoint(); p.x = e.clientX; p.y = e.clientY; return p.matrixTransform(svg.getScreenCTM().inverse()); };

  // An address in a rounded tag, centred on (x, y). Monospace, so the width follows from the length.
  function tag(parent, x, y, text, cls = "") {
    const w = text.length * 7.9 + 18;
    el("rect", {class: "tagbox", x: x - w / 2, y: y - 11, width: w, height: 22, rx: 11}, parent);
    el("text", {class: `tag ${cls}`, x, y}, parent).textContent = text;
  }

  function draw() {
    gLinks.replaceChildren(); gNodes.replaceChildren();
    if (!lab) return;
    const n = Object.keys(lab.routers).length, R = size(), showLans = n <= 12;
    const ys = Object.values(pos).map(p => p[1]), midY = (Math.min(...ys) + Math.max(...ys)) / 2;
    lab.links.forEach((l, i) => {
      const [x1, y1] = pos[l.a], [x2, y2] = pos[l.b];
      const bad = health?.bad_links?.includes(i);
      const line = el("line", {class: `link${bad ? " bad" : ""}`, x1, y1, x2, y2}, gLinks);
      el("title", {}, line).textContent = `${l.a} ${l.a_if} ${l.a_ip} ─ ${l.b} ${l.b_if} ${l.b_ip} (${l.subnet})${bad ? ", not passing traffic" : ""}`;
    });
    if (n <= 8) lab.links.forEach(l => { if (l.subnet) tag(gLinks, (pos[l.a][0] + pos[l.b][0]) / 2, (pos[l.a][1] + pos[l.b][1]) / 2, l.subnet, "net"); });
    for (const [name, info] of Object.entries(lab.routers)) {
      const [x, y] = pos[name];
      const bad = health?.bad_routers?.includes(name);
      const g = el("g", {class: `node${name === selected ? " selected" : ""}${bad ? " bad" : ""}${dragging?.moved && dragging.name === name ? " lifted" : ""}`, tabindex: 0, role: "button",
                         "aria-label": `Router ${name}${bad ? ", has a problem" : ""}`}, gNodes);
      el("circle", {class: "halo", cx: x, cy: y, r: R + 8}, g);
      el("circle", {class: "disc", cx: x, cy: y, r: R}, g);
      const big = R >= 38;  // room for the router glyph above the name
      if (big) el("use", {class: "glyph", href: "#i-router", x: x - 11, y: y - 25, width: 22, height: 22}, g);
      el("text", {class: "name", x, y: big ? y + 12 : y, "font-size": big ? 17 : R * 0.55}, g).textContent = name;
      const side = y < midY - 1 ? -1 : 1;  // LAN tags go on the outside of the drawing, away from the links
      if (showLans) info.lans.forEach((lan, i) => tag(g, x, y + side * (R + 28 + i * 26), lan.replace(/\.1\//, ".0/")));
      g.addEventListener("pointerdown", e => { e.stopPropagation(); anim?.stop(); dragging = {name, start: toSvg(e), moved: false}; svg.setPointerCapture(e.pointerId); });
      g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(name); } });
    }
  }

  function select(name) { selected = name; draw(); gNodes.querySelector(".selected")?.focus(); onSelect(name); }

  svg.addEventListener("pointerdown", e => { anim?.stop(); if (!dragging) dragging = {pan: true, start: toSvg(e), view: [...view]}; svg.setPointerCapture(e.pointerId); });
  svg.addEventListener("pointermove", e => {
    if (!dragging) return;
    const p = toSvg(e);
    if (dragging.pan) {
      const dx = p.x - dragging.start.x, dy = p.y - dragging.start.y;
      dragging.view[0] -= dx; dragging.view[1] -= dy; setView(dragging.view); return;
    }
    if (Math.hypot(p.x - dragging.start.x, p.y - dragging.start.y) > 4) dragging.moved = true;
    if (dragging.moved) { pos[dragging.name] = [p.x, p.y]; draw(); gTrace.replaceChildren(); }
  });
  svg.addEventListener("pointerup", () => {
    const d = dragging; dragging = null;
    if (d?.name) { if (d.moved) { draw(); onMove(pos); } else select(d.name); }
  });
  svg.addEventListener("wheel", e => {
    if (!e.ctrlKey && !e.metaKey) return;  // plain scrolling moves the page; a pinch (which arrives as ctrl+wheel) zooms
    e.preventDefault(); anim?.stop();
    const p = toSvg(e), f = Math.exp(Math.max(-50, Math.min(50, e.deltaY)) * 0.006);
    setView([p.x - (p.x - view[0]) * f, p.y - (p.y - view[1]) * f, view[2] * f, view[3] * f]);
  }, {passive: false});

  function arrow(a, b, kind, i = 0, dashed = false) {
    const [x1, y1] = pos[a], [x2, y2] = pos[b], R = size() + 4;
    const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1, nx = -dy / len * 8, ny = dx / len * 8;
    el("line", {class: `trace ${kind}${dashed ? " dashed" : ""}`, x1: x1 + dx / len * R + nx, y1: y1 + dy / len * R + ny,
                x2: x2 - dx / len * R + nx, y2: y2 - dy / len * R + ny, "marker-end": `url(#arrow-${kind}-${svg.id})`,
                style: `animation-delay:${i * .2}s`}, gTrace);
  }

  return {
    render(newLab, newHealth) {
      const newNames = JSON.stringify(Object.keys(newLab.routers));
      const refit = !lab || lab.name !== newLab.name || JSON.stringify(Object.keys(lab.routers)) !== newNames;
      const changed = refit || JSON.stringify(lab.links) !== JSON.stringify(newLab.links);
      const keep = lab && lab.name === newLab.name ? pos : {};
      lab = newLab; health = newHealth || health;
      if (changed) {
        pos = autoLayout(lab);
        for (const r of Object.keys(lab.routers)) if (keep[r]) pos[r] = keep[r];  // editing: routers stay where they are
        selected = Object.keys(lab.routers).includes(selected) ? selected : null;
        if (refit) fit(true);
      }
      if (!dragging) draw();
    },
    setHealth(h) { health = h; if (!dragging) draw(); },
    clearTraces() { gTrace.replaceChildren(); },
    path(names, kind) {  // loop / hole along a router path
      for (let i = 0; i < names.length - 1; i++) if (pos[names[i]] && pos[names[i + 1]]) arrow(names[i], names[i + 1], kind, i);
      const last = pos[names[names.length - 1]];
      if (kind === "hole" && last) el("path", {class: "x", d: `M${last[0] + size()} ${last[1] - size() - 14}l14 14m0 -14l-14 14`}, gTrace);
    },
    hop(a, b, kind, i = 0) { if (pos[a] && pos[b]) arrow(a, b, kind, i, kind === "gone"); },
    positions: () => pos,
    zoom(f) { const [x, y, w, h] = view; glide([x + w * (1 - f) / 2, y + h * (1 - f) / 2, w * f, h * f]); },
    fit, select,
    get selected() { return selected; },
  };
}
