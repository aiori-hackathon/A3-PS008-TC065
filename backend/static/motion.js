// Spring motion on top of the vendored Motion library (window.Motion, the vanilla build of Framer Motion).
// Without the library nothing animates; with "reduce motion" on, movement becomes a short fade.
const M = window.Motion;
const calm = matchMedia("(prefers-reduced-motion: reduce)");
const SPRING = {type: "spring", bounce: 0, duration: 0.45};  // critically damped: no overshoot

// Content arriving: fade up, one after another.
export function enter(els, {y = 12, gap = 0.04} = {}) {
  els = [...els].filter(e => !e.hidden);
  if (!M || !els.length) return;
  if (calm.matches) M.animate(els, {opacity: [0, 1]}, {duration: 0.2});
  else M.animate(els, {opacity: [0, 1], y: [y, 0]}, {...SPRING, delay: M.stagger(gap)});
}

// A glass surface arriving: blur and scale settle together, so it reads as a material, not a fade.
export function materialize(el) {
  if (!M || !el) return;
  if (calm.matches) return void M.animate(el, {opacity: [0, 1]}, {duration: 0.2});
  M.animate(el, {opacity: [0, 1], scale: [0.96, 1], filter: ["blur(10px)", "blur(0px)"]}, {...SPRING, duration: 0.55})
    .then(() => { el.style.filter = ""; });
}

// Segmented-control thumb. A tap carries a little momentum, so a little bounce.
export function slide(el, to, instant) {
  if (!M) return Object.assign(el.style, {transform: `translate(${to.x}px,${to.y}px)`, width: to.width + "px", height: to.height + "px"});
  M.animate(el, to, instant || calm.matches ? {duration: 0} : {type: "spring", bounce: 0.18, duration: 0.4});
}

// A 0 -> 1 spring for values CSS cannot animate (the map's viewBox). Returns something with stop().
export function tween(onUpdate) {
  if (!M || calm.matches) { onUpdate(1); return {stop() {}}; }
  return M.animate(0, 1, {...SPRING, duration: 0.6, onUpdate});
}
