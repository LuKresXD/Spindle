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
  const PRESSABLE = ".pill, .quiet button, .shelfitem, .rrow[data-id], .wheel3d img";
  // The feedback owns scale and nothing else: a shelf tile carries its sort offset in x/y and a
  // wave may still be fading it in, so the press kills only scale tweens and the release never
  // clears the transform (clearProps on any transform part drops the whole inline transform and
  // sent a sorted tile back to its DOM cell: the black square in the Price sort, 2026-11-08).
  const SCALE = "scale,scaleX,scaleY";
  let pressed = null;
  function press(el) {
    pressed = el;
    gsap.killTweensOf(el, SCALE);
    gsap.to(el, { scale: 0.96, duration: D(0.1), ease: "power1.out" });
  }
  function release() {
    const el = pressed; pressed = null;
    if (!el) return;
    const w = el.offsetWidth || 1, h = el.offsetHeight || 1;
    const stretch = Math.min(10, 0.04 * w);
    gsap.killTweensOf(el, SCALE);
    gsap.timeline()
      .to(el, { scaleX: (w + stretch) / w, scaleY: (h - stretch * 0.5) / h, duration: D(0.1), ease: "power1.out" })
      .to(el, { scaleX: 1, scaleY: 1, duration: D(0.9), ease: "elastic.out(1, 0.4)" });
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
  // keep: opacity only, so the element stays painted (the shelf fades to nothing but is kept
  // laid out and rastered for its next opening)
  function fade(el, to, { duration = 0.5, ease = MICRO, delay = 0, keep = false } = {}) {
    gsap.killTweensOf(el);
    return gsap.to(el, { [keep ? "opacity" : "autoAlpha"]: to, duration: D(duration), ease, delay });
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
    const proxy = { o: hit - 9, f: 0 };          // o: the ring's position; f: the fade-in
    let landed = false;
    // The tiles are written straight to style, one string a tile a frame: a gsap.set per tile
    // per frame built fifteen tween objects a frame, a third of the Pi's budget and a major GC
    // mid-roll. The hit tile is handed back to GSAP (one set) before the hand-over.
    const place = () => {
      imgs.forEach((img, i) => {
        if (landed && i === hit) return;
        const a = gsap.utils.clamp(-edge, edge, (i - proxy.o) * CURVE);
        const rad = a * Math.PI / 180;
        const dist = Math.abs(i - proxy.o);
        img.style.transform = `translate(-50%, -50%) translate3d(${(Math.sin(rad) * R).toFixed(2)}px, 0px, ${(-R * (1 - Math.cos(rad))).toFixed(2)}px) rotateY(${(-a).toFixed(3)}deg) scale(${(1 - Math.min(0.18, dist * 0.04)).toFixed(4)})`;
        img.style.opacity = (proxy.f * gsap.utils.clamp(0, 1, 1.1 - dist * 0.33)).toFixed(3);
      });
    };
    place();
    // The tiles are placed first and fade in one by one over the first frames (never the
    // group: an opacity on the 3D parent costs an extra render pass every frame), so the
    // GPU uploads the textures before anything has to move. Then fast out of the gate,
    // still visibly turning until it stops: power3, not expo (whose long tail parks the
    // wheel a second before the hand-over).
    const tl = gsap.timeline({ paused: true });
    tl.to(proxy, { f: 1, duration: D(0.3), ease: MICRO, onUpdate: place,
      onStart: () => gsap.set(imgs, { visibility: "inherit" }) }, 0);   // the stylesheet hides a tile until it is placed
    tl.to(proxy, { o: hit, duration: D(1.9), ease: "power3.out", onUpdate: place }, 0.12);
    requestAnimationFrame(() => requestAnimationFrame(() => tl.play()));
    tl.to(imgs.filter((_, i) => i !== hit), { autoAlpha: 0, duration: D(0.3), ease: MICRO }, ">-0.3");
    tl.call(() => {
      landed = true;
      if (imgs[hit]) gsap.set(imgs[hit], { xPercent: -50, yPercent: -50, x: 0, y: 0, z: 0, rotationY: 0, scale: 1, opacity: 1 });
      if (onLand) onLand();
    }, null, ">-0.02");
    return tl;
  }
  // The hit cover flies from its place on the wheel into the card's cover slot, then the
  // real cover takes over underneath it (Flip.fit: same picture, two places, one move).
  function handOver(fromImg, toImg, { onDone } = {}) {
    gsap.set(toImg, { autoAlpha: 0 });
    gsap.set(fromImg, { zIndex: 5 });
    // The tile scales into the slot (its corners grow with it and land on the card's radius),
    // then the card's own cover fades in OVER the still-solid tile and the tile is dropped
    // only once the cover is fully there. Never a crossfade of the two: two half-transparent
    // copies of the same picture add up to three quarters, and the cover visibly dips.
    const tl = gsap.timeline({ onComplete: () => { if (onDone) onDone(); } });
    tl.add(Flip.fit(fromImg, toImg, { duration: D(0.85), ease: "house", scale: true }), 0);
    tl.to(toImg, { autoAlpha: 1, duration: D(0.3), ease: MICRO }, ">-0.1");
    tl.set(fromImg, { autoAlpha: 0 });
    return tl;
  }

  // ── the shelf: a sort change is a Flip, not a re-render ───────────────────
  // A sort of equal cells: each tile glides from where it was to where it is. The caller knows
  // both (the grid's geometry is arithmetic), so nothing is measured and nothing is laid out;
  // the transforms stay, they are the order.
  function glide(items, from, to) {
    if (!items.length) return null;
    return gsap.fromTo(items, { x: (i) => from[i].x, y: (i) => from[i].y },
      { x: (i) => to[i].x, y: (i) => to[i].y, duration: D(0.65), ease: "power4.inOut", stagger: 0.003 });
  }
  // Covers wave in from the top left: yPercent, so a sort's x/y offsets are left alone, and
  // plain opacity, so no tile changes visibility mid-wave (each such change re-layerizes).
  function waveIn(items) {
    const list = gsap.utils.toArray(items);
    if (!list.length) return null;
    return gsap.fromTo(list, { yPercent: 14, opacity: 0 }, { yPercent: 0, opacity: 1, duration: D(0.8), ease: SETTLE,
      stagger: { each: 0.018, from: "start" } });
  }

  // ── numbers roll like an odometer: each digit a column of 0-9 behind a mask ────────
  function odometer(el, value) {
    const text = String(value);
    const cur = el.dataset.odo;
    if (cur === text) return;
    const was = cur == null;
    el.dataset.odo = text;
    const cols = [...text].map((ch, i) => {
      let col = el.children[i];
      if (!col || col.dataset.ch === undefined) {
        col = document.createElement("span"); col.className = "odo";
        col.innerHTML = /\d/.test(ch) ? `<span class="odo-col">${"0123456789".split("").map((d) => `<b>${d}</b>`).join("")}</span>` : `<b class="odo-sym">${ch}</b>`;
        el.insertBefore(col, el.children[i] || null);
      }
      col.dataset.ch = ch;
      return col;
    });
    while (el.children.length > cols.length) el.lastChild.remove();
    cols.forEach((col, i) => {
      const ch = text[i];
      const inner = col.firstElementChild;
      if (!/\d/.test(ch)) { inner.textContent = ch; return; }
      const to = -Number(ch) * 10;           // the column is ten digits tall: one digit = 10 %
      if (was) gsap.set(inner, { yPercent: to });
      else gsap.to(inner, { yPercent: to, duration: D(0.9), ease: "house", overwrite: true });
    });
  }
  // a count that rolls up to its value when it first appears
  function countUp(el, value, { duration = 1.1, delay = 0 } = {}) {
    const n = Number(value) || 0;
    const proxy = { v: 0 };
    el.textContent = "0";
    return gsap.to(proxy, { v: n, duration: D(duration), ease: "power3.out", delay,
      onUpdate: () => { el.textContent = String(Math.round(proxy.v)); } });
  }
  // the current line of lyrics gets heavier as well as bigger (Inter is variable)
  function weight(el, w, duration = 0.5) {
    gsap.to(el, { fontVariationSettings: `"wght" ${w}`, duration: D(duration), ease: "house", overwrite: "auto" });
  }
  // a scroller's head recedes as it scrolls: a little slower than the content, and fading.
  // One listener per scroller; `head()` returns whatever element is the head right now.
  function parallaxHead(scroller, head, { rate = 0.35, fadeBy = 420 } = {}) {
    const apply = () => {
      const el = head();
      if (!el) return;
      const y = scroller.scrollTop;
      gsap.set(el, { y: y * rate, opacity: gsap.utils.clamp(0, 1, 1 - y / fadeBy) });
    };
    scroller.addEventListener("scroll", apply, { passive: true });
    return apply;
  }

  // ── boot: the record mark draws itself, the wordmark rises beside it, then the screen ──
  function boot(overlay, word, onDone) {
    const mark = overlay.querySelector(".bootmark");
    const split = SplitText.create(word, { type: "chars", mask: "chars", charsClass: "sc" });
    const tl = gsap.timeline({ onComplete: () => { split.revert(); overlay.remove(); if (onDone) onDone(); } });
    if (mark) {
      const disc = mark.querySelector(".bm-disc"), label = mark.querySelector(".bm-label"), hole = mark.querySelector(".bm-hole");
      const grooves = [...mark.querySelectorAll(".bm-groove")];
      grooves.forEach((g) => { const c = 2 * Math.PI * Number(g.getAttribute("r")); gsap.set(g, { strokeDasharray: c, strokeDashoffset: c, opacity: 1 }); });
      tl.fromTo(disc, { scale: 0.6, opacity: 0 }, { scale: 1, opacity: 1, duration: D(0.9), ease: BIG }, 0)
        .to(grooves, { strokeDashoffset: 0, duration: D(1.1), ease: "power2.inOut", stagger: { each: 0.06, from: "end" } }, 0.25)
        .fromTo(label, { scale: 0.4, opacity: 0 }, { scale: 1, opacity: 1, duration: D(0.7), ease: "back.out(1.6)" }, 0.9)
        .fromTo(hole, { scale: 0, opacity: 0 }, { scale: 1, opacity: 1, duration: D(0.4), ease: SETTLE }, 1.25);
    }
    tl.from(split.chars, { yPercent: 110, duration: D(1.1), ease: BIG, stagger: 0.035 }, 0.75)
      .to([word, mark].filter(Boolean), { autoAlpha: 0, duration: D(0.5), ease: MICRO }, "+=0.5")
      .to(overlay, { autoAlpha: 0, duration: D(0.6), ease: MICRO }, "<0.1");
    return tl;
  }

  return { D, reduced, revealLines, revealWords, arrive, leave, fade, tray, rowGlide, handOver, odometer, countUp, weight, parallaxHead,
           pageLayout, pageCommit, pageCancel, pageRest, wheel, glide, waveIn, boot, gsap };
})();
