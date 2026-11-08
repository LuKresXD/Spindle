/* The motion layer: one small vocabulary (docs/MOTION.md) the page logic calls into.
   Transforms and opacity only; every tween collapses to a cut under
   prefers-reduced-motion. GSAP 3.15 + CustomEase, Flip, SplitText (vendor/). */
window.Motion = (() => {
  "use strict";
  gsap.registerPlugin(CustomEase, Flip, SplitText);
  CustomEase.create("house", "0.625, 0.05, 0, 1");   // fast leap, long soft landing
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const D = (d) => (reduced ? 0 : d);
  const SETTLE = "expo.out", BIG = "expo.inOut", MICRO = "power1.inOut";

  // ── tap feedback ──────────────────────────────────────────────────────────
  // A press squashes the control a little; the release springs it back with an
  // elastic settle. Delegated: anything matching PRESSABLE gets it.
  const PRESSABLE = ".pill, .quiet button, .shelfgrid img, .rrow[data-id], .wheel3d img";
  let pressed = null;
  function press(el) {
    pressed = el;
    gsap.killTweensOf(el);
    gsap.to(el, { scale: 0.96, duration: D(0.1), ease: "power1.out", overwrite: true });
  }
  function release() {
    const el = pressed; pressed = null;
    if (!el) return;
    const w = el.offsetWidth || 1, h = el.offsetHeight || 1;
    const stretch = Math.min(10, 0.04 * w);
    gsap.timeline({ overwrite: true })
      .to(el, { scaleX: (w + stretch) / w, scaleY: (h - stretch * 0.5) / h, duration: D(0.1), ease: "power1.out" })
      .to(el, { scaleX: 1, scaleY: 1, duration: D(0.9), ease: "elastic.out(1, 0.4)", clearProps: "transform" });
  }
  document.addEventListener("pointerdown", (e) => { const t = e.target.closest(PRESSABLE); if (t) press(t); }, { passive: true });
  document.addEventListener("pointerup", release, { passive: true });
  document.addEventListener("pointercancel", release, { passive: true });

  // ── reveals ───────────────────────────────────────────────────────────────
  // Text arrives through a mask: each line slides up out of an overflow-hidden wrapper.
  function revealLines(el, { delay = 0, stagger = 0.075, duration = 1 } = {}) {
    if (!el || !el.textContent.trim()) return null;
    const split = SplitText.create(el, { type: "lines", mask: "lines", linesClass: "sl" });
    return gsap.from(split.lines, { yPercent: 110, duration: D(duration), ease: SETTLE, stagger, delay,
      onComplete: () => split.revert() });
  }
  function revealWords(el, { delay = 0, stagger = 0.06, duration = 1 } = {}) {
    if (!el || !el.textContent.trim()) return null;
    const split = SplitText.create(el, { type: "words", mask: "words", wordsClass: "sw" });
    return gsap.from(split.words, { yPercent: 110, duration: D(duration), ease: SETTLE, stagger, delay,
      onComplete: () => split.revert() });
  }
  // A set of elements arrives: a short rise and a fade, a beat apart.
  function arrive(els, { y = 24, x = 0, scale = 1, stagger = 0.08, delay = 0, duration = 0.9, ease = SETTLE } = {}) {
    const list = gsap.utils.toArray(els).filter(Boolean);
    if (!list.length) return null;
    gsap.killTweensOf(list);
    return gsap.fromTo(list, { y, x, scale, autoAlpha: 0 },
      { y: 0, x: 0, scale: 1, autoAlpha: 1, duration: D(duration), ease, stagger, delay, clearProps: "transform" });
  }
  function leave(els, { duration = 0.25, onComplete } = {}) {
    const list = gsap.utils.toArray(els).filter(Boolean);
    if (!list.length) { if (onComplete) onComplete(); return null; }
    gsap.killTweensOf(list);
    return gsap.to(list, { autoAlpha: 0, duration: D(duration), ease: MICRO, onComplete });
  }
  function fade(el, to, { duration = 0.5, ease = MICRO, delay = 0 } = {}) {
    gsap.killTweensOf(el);
    return gsap.to(el, { autoAlpha: to, duration: D(duration), ease, delay });
  }

  // ── trays: one lighter pill that flies between the buttons ────────────────
  function tray(nav) {
    let ind = nav.querySelector(".tray-ind");
    if (!ind) { ind = document.createElement("i"); ind.className = "tray-ind"; nav.prepend(ind); }
    const rect = (btn) => ({ left: btn.offsetLeft, width: btn.offsetWidth, top: btn.offsetTop, height: btn.offsetHeight });
    let current = null;
    function place(btn) {
      current = btn;
      if (!btn) { gsap.set(ind, { autoAlpha: 0 }); return; }
      const r = rect(btn);
      gsap.killTweensOf(ind);
      gsap.set(ind, { autoAlpha: 1, x: r.left, y: r.top, width: r.width, height: r.height, scaleX: 1, scaleY: 1 });
    }
    function to(btn) {
      if (!btn) { fade(ind, 0, { duration: 0.2 }); current = null; return; }
      if (!current || gsap.getProperty(ind, "opacity") === 0) { place(btn); return; }
      const from = rect(current), r = rect(btn);
      const dir = Math.sign(r.left - from.left) || 1;
      current = btn;
      gsap.killTweensOf(ind);
      gsap.timeline()
        .to(ind, { x: r.left + dir * 10, width: r.width, duration: D(0.35), ease: "house" }, 0)
        .to(ind, { scaleY: 0.86, duration: D(0.15), ease: "power2.out" }, 0)
        .to(ind, { x: r.left, duration: D(0.25), ease: "power2.out" }, ">-0.08")
        .to(ind, { scaleY: 1, duration: D(0.45), ease: "elastic.out(1, 0.5)" }, "<");
    }
    return { place, to };
  }

  // ── the lit row of the tracklist glides to the new track ──────────────────
  function rowGlide(list) {
    let ind = list.querySelector(".row-ind");
    if (!ind) { ind = document.createElement("i"); ind.className = "row-ind"; list.prepend(ind); }
    let shown = false;
    function target(row) { return { y: row.offsetTop, height: row.offsetHeight, left: row.offsetLeft, width: row.offsetWidth }; }
    return {
      place(row) {
        if (!row) { gsap.set(ind, { autoAlpha: 0 }); shown = false; return; }
        const t = target(row);
        gsap.killTweensOf(ind);
        gsap.set(ind, { autoAlpha: 1, y: t.y, height: t.height, x: t.left, width: t.width });
        shown = true;
      },
      to(row) {
        if (!row) { fade(ind, 0, { duration: 0.2 }); shown = false; return; }
        if (!shown) { this.place(row); return; }
        const t = target(row);
        gsap.killTweensOf(ind);
        gsap.to(ind, { y: t.y, height: t.height, x: t.left, width: t.width, duration: D(0.6), ease: "back.out(1.2)" });
      },
    };
  }

  // ── pages: the incoming page slides over the outgoing one, which recedes ─────
  // layout(prog, dir, outgoing, incoming): prog 0..1 of the way, dir +1 = incoming from the right.
  const PARALLAX = 0.35, DIM = 0.6;
  function pageLayout(W, prog, dir, outgoing, incoming, dims) {
    gsap.set(incoming, { x: dir * W * (1 - prog), zIndex: 2, visibility: "visible" });
    gsap.set(outgoing, { x: -dir * W * PARALLAX * prog, zIndex: 1, visibility: "visible" });
    if (dims) { gsap.set(dims.outgoing, { opacity: DIM * prog }); gsap.set(dims.incoming, { opacity: 0 }); }
  }
  function pageCommit(W, prog, dir, outgoing, incoming, dims, speed) {
    const proxy = { p: prog };
    const dur = gsap.utils.clamp(0.3, 0.65, (1 - prog) * 0.75 / Math.max(1, speed * 1.4));
    return gsap.to(proxy, { p: 1, duration: D(dur), ease: "house",
      onUpdate: () => pageLayout(W, proxy.p, dir, outgoing, incoming, dims) }).then();
  }
  function pageCancel(W, prog, dir, outgoing, incoming, dims) {
    const proxy = { p: prog };
    return gsap.to(proxy, { p: 0, duration: D(0.4), ease: "house",
      onUpdate: () => pageLayout(W, proxy.p, dir, outgoing, incoming, dims) }).then();
  }
  function pageRest(active, others, dims) {
    gsap.set(active, { x: 0, zIndex: 1, visibility: "visible" });
    gsap.set(others, { x: 0, zIndex: 0, visibility: "hidden" });
    if (dims) gsap.set(dims, { opacity: 0 });
  }

  // ── the wheel: covers on a curve in depth, one roll that slows into the hit ──
  const CURVE = 26, TILE = 300, GAP = 40;
  function wheel(container, imgs, hit, { onLand } = {}) {
    const chord = TILE + GAP;
    const R = chord / (2 * Math.sin((CURVE / 2) * Math.PI / 180));
    const edge = 95;
    const proxy = { o: hit - 9 };
    const place = () => {
      imgs.forEach((img, i) => {
        const a = gsap.utils.clamp(-edge, edge, (i - proxy.o) * CURVE);
        const rad = a * Math.PI / 180;
        const dist = Math.abs(i - proxy.o);
        gsap.set(img, { x: Math.sin(rad) * R, z: -R * (1 - Math.cos(rad)), rotationY: -a,
          opacity: gsap.utils.clamp(0, 1, 1.15 - dist * 0.26), scale: 1 - Math.min(0.18, dist * 0.04) });
      });
    };
    gsap.set(imgs, { xPercent: -50, yPercent: -50, transformOrigin: "50% 50%" });
    place();
    // fast out of the gate, still visibly turning until it stops: power3, not expo (whose long
    // tail parks the wheel a second before the hand-over)
    const tl = gsap.timeline();
    tl.to(proxy, { o: hit, duration: D(1.9), ease: "power3.out", onUpdate: place });
    tl.to(imgs.filter((_, i) => i !== hit), { opacity: 0, duration: D(0.3), ease: MICRO }, ">-0.3");
    if (onLand) tl.call(onLand, null, ">-0.02");
    return tl;
  }
  // The hit cover flies from its place on the wheel into the card's cover slot, then the
  // real cover takes over underneath it (Flip.fit: same picture, two places, one move).
  function handOver(fromImg, toImg, { onDone } = {}) {
    gsap.set(toImg, { autoAlpha: 0 });
    gsap.set(fromImg, { zIndex: 5 });
    const tl = gsap.timeline({ onComplete: () => { gsap.set(toImg, { autoAlpha: 1 }); gsap.set(fromImg, { autoAlpha: 0 }); if (onDone) onDone(); } });
    tl.add(Flip.fit(fromImg, toImg, { duration: D(0.85), ease: "house", scale: true }), 0);
    tl.to(fromImg, { borderRadius: 32, duration: D(0.85), ease: "house" }, 0);
    return tl;
  }

  // ── the shelf: a sort change is a Flip, not a re-render ───────────────────
  function flipGrid(grid, reorder) {
    const items = Array.from(grid.children);
    const state = Flip.getState(items);
    reorder();
    return Flip.from(state, { duration: D(0.65), ease: "power4.inOut", absolute: true, prune: true, stagger: 0.003 });
  }
  // Covers wave in from the top left.
  function waveIn(items, { cap = 35 } = {}) {
    const list = gsap.utils.toArray(items);
    if (!list.length) return null;
    const first = list.slice(0, cap), rest = list.slice(cap);
    gsap.set(rest, { autoAlpha: 1 });
    return gsap.fromTo(first, { y: 30, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: D(0.8), ease: SETTLE,
      stagger: { each: 0.018, from: "start" }, clearProps: "transform" });
  }

  // ── boot: the wordmark rises out of a mask once, then the screen fades in ─────
  function boot(overlay, word, onDone) {
    const split = SplitText.create(word, { type: "chars", mask: "chars", charsClass: "sc" });
    gsap.timeline({ onComplete: () => { split.revert(); overlay.remove(); if (onDone) onDone(); } })
      .from(split.chars, { yPercent: 110, duration: D(1.25), ease: BIG, stagger: 0.035 })
      .to(word, { autoAlpha: 0, duration: D(0.5), ease: MICRO }, "+=0.45")
      .to(overlay, { autoAlpha: 0, duration: D(0.6), ease: MICRO }, "<0.1");
  }

  return { D, reduced, revealLines, revealWords, arrive, leave, fade, tray, rowGlide, handOver,
           pageLayout, pageCommit, pageCancel, pageRest, wheel, flipGrid, waveIn, boot, gsap };
})();
