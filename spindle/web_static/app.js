/* Spindle touch display client.
   One EventSource carries the engine's ScreenState; the page interpolates the
   progress between ticks, fetches lyrics and the info sheet on demand, and turns
   swipes and taps into the few things the screen can do.

   The screen is three pages in a ring, swiped left and right: Off (black on
   purpose), Spindle (the record: playing, or black with the shelf controls a tap
   reveals) and Claw (a placeholder for now). A needle drop always brings Spindle.
   Everything that moves goes through the motion layer (motion.js, docs/MOTION.md). */
(() => {
  "use strict";
  const M = window.Motion;
  const $ = (id) => document.getElementById(id);
  const el = {
    body: document.body, bgs: [$("bg-a"), $("bg-b")], home: $("home"), art: $("art"), title: $("title"),
    artist: $("artist"), albumline: $("albumline"), sidetext: $("sidetext"), fill: $("fill"),
    feat: $("feat"), elapsed: $("elapsed"), remaining: $("remaining"), list: $("list"), lyrics: $("lyrics"),
    lyriclines: $("lyriclines"), btnLyrics: $("btn-lyrics"), btnInfo: $("btn-info"), corner: document.querySelector(".corner"),
    listening: $("listening"), lvl: $("lvl"), sheet: $("sheet"), tiles: $("tiles"),
    ended: $("ended"), endedKicker: $("ended-kicker"), endedTitle: $("ended-title"), endedSub: $("ended-sub"),
    pages: [$("page-off"), $("page-spindle"), $("page-claw")], pageOff: $("page-off"), offword: $("offword"), dots: $("dots"),
    idle: $("idle"), btnPick: $("btn-pick"), btnShelf: $("btn-shelf"), btnRecent: $("btn-recent"),
    pick: $("pick"), wheel: $("wheel"), wheelKicker: $("wheel-kicker"), wheelstrip: $("wheelstrip"),
    landed: $("landed"), pickArt: $("pick-art"), pickKicker: $("pick-kicker"), pickTitle: $("pick-title"),
    pickArtist: $("pick-artist"), pickMeta: $("pick-meta"), pickPlays: $("pick-plays"),
    btnAnother: $("btn-another"), btnPickDone: $("btn-pick-done"), pickHint: $("pick-hint"),
    shelf: $("shelf"), shelfCount: $("shelf-count"), shelfSort: $("shelf-sort"), btnShelfClose: $("btn-shelf-close"),
    shelfgrid: $("shelfgrid"), recent: $("recent"), btnRecentClose: $("btn-recent-close"), recentlist: $("recentlist"),
    boot: $("boot"), bootword: $("bootword"),
  };
  const dims = el.pages.map((p) => p.querySelector(".page-dim"));

  const BULK_IMPORT_DAY = "2025-04-17";   // the day the first 46 records were entered at once
  const OFF = 0, SPINDLE = 1, CLAW = 2;
  const PAGE_NAMES = ["off", "spindle", "claw"];
  let state = null;
  let clockOffset = 0;                    // client seconds minus server seconds
  let artKey = "";
  let listKey = "";
  let sideKey = "";
  let trackKey = "";
  let lyricsOn = false;
  let lyrics = null;                      // {synced: [[t, line]], plain}
  let lyricsFor = "";
  let lyricIndex = -1;
  // A new track's start is a late guess until the engine's fingerprint offsets pin it
  // (~15 s in). Lean the lyric clock forward over that window, decaying to a small
  // steady-state cushion, so a fresh track is not read late while settled tracks stay put.
  const LYRIC_BASE_LEAD = 0.3;        // karaoke convention: a line lands a touch early
  const FRESH_LEAD = 2.6, FRESH_DECAY = 16;
  let trackChangedAt = 0;             // performance.now() of the last track change
  let infoFor = "";
  let info = null;                        // album info (pressing, story, plays)
  let trackFor = "";
  let trackInfo = null;                   // Genius: story, samples, producers
  let sheetOpen = false;
  let endedDismissed = null;              // `at` of an end-of-side card Luka tapped away
  let endedShown = false;
  let lastPhase = "";
  // The engine confirms a track change a few seconds after it happens (a tick, then a
  // fingerprint). When the start is pinned and the deck speed measured, the clock knows
  // the end to a second: flip the page to the next track on the clock and let the engine
  // catch up. If it disagrees for 10 s, fall back to what it says.
  let serverState = null, flip = null;    // flip = { index, at (server seconds) }
  const nextRowOf = (st) => {
    const rows = st.tracklist || [], i = st.current_index;
    if (i < 0 || i + 1 >= rows.length) return null;
    return sideOf(rows[i].position) === sideOf(rows[i + 1].position) ? rows[i + 1] : null;   // never across a side
  };
  function viewOf(st) {
    if (!flip) return st;
    if (st.current_index >= flip.index || serverNow() - flip.at > 10) { flip = null; return st; }
    const nxt = st.tracklist[flip.index];
    return { ...st, title: nxt.title, position: nxt.position, current_index: flip.index, track_elapsed: 0,
             track_duration: nxt.duration, updated_at: flip.at, scrobbled: !!nxt.scrobbled, start_exact: false };
  }

  // ── helpers ──────────────────────────────────────────────────────────────
  const mmss = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const serverNow = () => Date.now() / 1000 - clockOffset;
  const fmtCount = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n);
  // "A1" and "1A" (the Madvillainy pressing) both mean side A: the first letter in the position
  const sideOf = (pos) => { const m = /[A-Za-z]/.exec(pos || ""); return m ? m[0].toUpperCase() : ""; };
  // "Maria I'm Drunk (feat. Justin Bieber & Young Thug)" → bare title + the feature credit,
  // shown on its own smaller line instead of stacking the title seven lines high.
  const FEAT_RE = /\s*[\(\[]\s*(?:feat|ft|featuring|with|w\/)\.?\s*([^\)\]]*)[\)\]]\s*$/i;
  function splitFeat(title) {
    const m = FEAT_RE.exec(title || "");
    return m ? { main: (title || "").slice(0, m.index).trim() || title, feat: m[1].trim() } : { main: title || "", feat: "" };
  }
  const fmtDate = (iso) => {
    if (!iso) return "";
    if (iso.slice(0, 10) === BULK_IMPORT_DAY) return iso.slice(0, 4);
    const d = new Date(iso.slice(0, 10) + "T12:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  };
  const fmtDay = (ts) => {
    const d = new Date(ts * 1000), now = new Date();
    const sameDay = (a, b) => a.toDateString() === b.toDateString();
    if (sameDay(d, now)) return "Today";
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (sameDay(d, y)) return "Yesterday";
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
      (d.getFullYear() !== now.getFullYear() ? ` ${d.getFullYear()}` : "");
  };
  const W = () => window.innerWidth;
  const sample = (arr, n) => { const a = arr.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a.slice(0, n); };
  const show = (node) => { node.classList.remove("hidden"); M.gsap.set(node, { clearProps: "opacity,visibility" }); };
  const hide = (node) => node.classList.add("hidden");

  function rawPosition() {
    if (!state || state.track_elapsed == null) return null;
    return state.track_elapsed + Math.max(0, serverNow() - state.updated_at) * (state.deck_speed || 1);
  }
  function position() {
    const p = rawPosition();
    return p == null ? null : (state.track_duration ? Math.min(p, state.track_duration) : p);
  }

  // ── pages ────────────────────────────────────────────────────────────────
  // The incoming page slides over the outgoing one, which recedes at a third of
  // the speed and darkens. The ring: left of Off is Claw, right of Claw is Off.
  let pageIndex = SPINDLE;
  let moving = false;
  let dotsTimer = 0, offTimer = 0;
  const neighbour = (dir) => (pageIndex + (dir > 0 ? 1 : 2)) % 3;
  function rest() {
    M.pageRest(el.pages[pageIndex], el.pages.filter((_, i) => i !== pageIndex), dims);
  }
  function dragTo(dx) {
    const dir = dx < 0 ? 1 : -1;
    const incoming = neighbour(dir), third = neighbour(-dir);
    M.gsap.set(el.pages[third], { visibility: "hidden" });
    M.pageLayout(W(), Math.min(1, Math.abs(dx) / W()), dir, el.pages[pageIndex], el.pages[incoming],
                 { outgoing: dims[pageIndex], incoming: dims[incoming] });
  }
  async function showPage(next, dir, prog = 0, speed = 0) {
    if (next === pageIndex || moving) { if (!moving) { rest(); showDots(1200); } return; }
    moving = true;
    const prev = pageIndex;
    pageIndex = next;
    onPageChanged();
    await M.pageCommit(W(), prog, dir, el.pages[prev], el.pages[next], { outgoing: dims[prev], incoming: dims[next] }, speed);
    rest();
    moving = false;
  }
  async function snapBack(dx) {
    const dir = dx < 0 ? 1 : -1, incoming = neighbour(dir);
    moving = true;
    await M.pageCancel(W(), Math.min(1, Math.abs(dx) / W()), dir, el.pages[pageIndex], el.pages[incoming],
                       { outgoing: dims[pageIndex], incoming: dims[incoming] });
    rest();
    moving = false;
    showDots(1200);
    if (pageIndex !== SPINDLE) hideIdle();
  }
  function onPageChanged() {
    el.body.dataset.page = PAGE_NAMES[pageIndex];
    showDots(1800);
    if (pageIndex === OFF) flashOff(); else M.fade(el.offword, 0, { duration: 0.3 });
    if (pageIndex === SPINDLE) showIdle(); else hideIdle();
  }
  function showDots(ms) {
    clearTimeout(dotsTimer);
    [...el.dots.children].forEach((d, i) => d.classList.toggle("on", i === pageIndex));
    el.dots.classList.add("show");
    if (ms) dotsTimer = setTimeout(() => el.dots.classList.remove("show"), ms);
  }
  // the Off page names itself for a moment: on arrival, and on a tap
  function flashOff() {
    clearTimeout(offTimer);
    M.fade(el.offword, 1, { duration: 0.7, ease: "expo.out" });
    offTimer = setTimeout(() => M.fade(el.offword, 0, { duration: 0.9 }), 2200);
  }
  rest();

  // ── phases ───────────────────────────────────────────────────────────────
  function apply(st) {
    serverState = st;
    clockOffset = Date.now() / 1000 - st.server_time;
    st = viewOf(st);
    state = st;
    // a tapped-away end card stays away; a record coming on brings the Spindle page
    const ended = st.ended && st.ended.at !== endedDismissed ? st.ended : null;
    const playing = st.phase === "playing";
    if (playing && lastPhase !== "playing") {
      closeOverlays();
      if (pageIndex !== SPINDLE) showPage(SPINDLE, pageIndex === OFF ? 1 : -1);
    }
    lastPhase = st.phase;
    if (st.phase !== "idle") unprepareShelf();
    el.body.className = `phase-${st.phase}` + (sheetOpen ? " sheet-open" : "") + (ended ? " ended-open" : "");
    el.home.classList.toggle("hidden", !playing && !ended);
    requestAnimationFrame(() => el.home.classList.toggle("shown", playing || !!ended));
    const listen = st.phase === "listening" && !pickOpen && !shelfOpen && !recentOpen;
    if (listen && el.listening.classList.contains("hidden")) { show(el.listening); M.arrive(el.listening.firstElementChild, { y: 14, duration: 0.8 }); }
    else if (!listen) hide(el.listening);
    if (listen) renderLevel(st.level_db);
    if (playing) renderHome(st);
    renderEnded(ended);
    if (!playing && !ended) {
      closeSheet();
      if (st.phase !== "idle") hideIdle();
      // idle and listening are black: the last record's blurred cover must not linger
      // (the end-of-side card keeps it; once that goes, so does the backdrop; a pick owns it)
      if (artKey && !pickOpen) { artKey = ""; el.art.removeAttribute("src"); setBackdrop(""); }
    }
  }

  // five bars that follow what the Pi hears: -60 dB is silence, -15 dB a loud record
  const LVL_SHAPE = [0.55, 0.8, 1, 0.8, 0.55];
  function renderLevel(db) {
    const norm = M.gsap.utils.clamp(0, 1, ((db ?? -120) + 60) / 45);
    [...el.lvl.children].forEach((b, i) => {
      const target = 0.15 + norm * LVL_SHAPE[i] * (0.85 + (i % 2) * 0.1);
      M.gsap.to(b, { scaleY: target, duration: M.D(1.4), ease: "house", overwrite: true });
    });
  }

  function renderHome(st) {
    const key = `${st.artist}|${st.title}|${st.album}`;
    if (key !== trackKey) {
      trackKey = key;
      trackChangedAt = performance.now();
      const { main, feat } = splitFeat(st.title);
      el.title.textContent = main;
      fitTitle(el.title);
      el.artist.textContent = st.artist || "";
      el.feat.textContent = feat ? `feat. ${feat}` : "";
      M.revealLines(el.title);
      M.revealLines(el.artist, { delay: 0.08 });
      if (feat) M.arrive(el.feat, { delay: 0.18, y: 14 });
      lyricIndex = -1;
      loadLyrics(st);      // into the hidden panels too, so Lyrics and Info open instantly
      loadInfo();
      warmNextLyrics(st);
    }
    el.albumline.textContent = [st.album, st.year].filter(Boolean).join(" · ");

    if (st.art && st.art !== artKey) {
      artKey = st.art;
      const url = `/art/${st.art}.jpg`;
      const img = new Image();
      img.onload = () => {
        el.art.src = url;
        setBackdrop(url);
        M.arrive(el.art, { y: 22, scale: 0.96, duration: 1.1 });
      };
      img.src = url;
    } else if (!st.art && artKey) {
      artKey = ""; el.art.removeAttribute("src"); setBackdrop("");
    }

    const rows = st.tracklist || [];
    const lk = rows.map((r, i) => `${r.position}|${r.title}|${r.scrobbled ? 1 : 0}|${i === st.current_index ? 1 : 0}`).join("\n");
    if (lk !== listKey) {
      listKey = lk;
      renderList(st, rows);
    }
    renderSide(st, rows);
  }

  // Cover URLs come in three sizes: the cover, a tile copy and the pre-blurred backdrop seed.
  const tileUrl = (u) => (u ? u.replace(/\.jpg$/, ".s.jpg") : "");
  const blurUrl = (u) => (u ? u.replace(/\.jpg$/, ".blur.jpg") : "");
  let bgFront = 0, bgUrl = "";
  function setBackdrop(url) {
    const seed = blurUrl(url);
    if (seed === bgUrl) return;
    bgUrl = seed;
    const front = el.bgs[bgFront], back = el.bgs[1 - bgFront];
    if (!seed) { M.fade(front, 0, { duration: 0.9, ease: "house" }); M.fade(back, 0, { duration: 0.9, ease: "house" }); return; }
    const img = new Image();
    img.onload = () => {
      if (bgUrl !== seed) return;
      back.style.backgroundImage = `url("${seed}")`;
      M.fade(back, 1, { duration: 1.1, ease: "house" });
      M.fade(front, 0, { duration: 1.1, ease: "house" });
      bgFront = 1 - bgFront;
    };
    img.src = seed;
  }

  // Shrink a title until no word has to break (Plasticworld came out as "Plasticworl / d").
  function fitTitle(node, max = 108, min = 44) {
    let size = max;
    node.style.fontSize = size + "px";
    const fits = () => { const w = node.clientWidth; return [...node.childNodes].length === 0 ||
      Array.from(node.textContent.split(/\s+/)).every((word) => measure(word, size) <= w); };
    while (size > min && !fits()) { size -= 4; node.style.fontSize = size + "px"; }
    while (size > min && node.scrollHeight > size * 1.02 * 3.2) { size -= 4; node.style.fontSize = size + "px"; }
  }
  const canvas = document.createElement("canvas").getContext("2d");
  function measure(text, size) {
    canvas.font = `700 ${size}px Inter, sans-serif`;
    return canvas.measureText(text).width * 0.975;             // letter-spacing -0.025em
  }

  const rowGlide = M.rowGlide(el.list);
  function renderList(st, rows) {
    const cur = st.current_index;
    const side = cur >= 0 && rows[cur] ? sideOf(rows[cur].position) : "";
    // the current side's tracks when the pressing is known, else the window around the current track
    let shown = rows.map((r, i) => ({ r, i }));
    if (side) shown = shown.filter(({ r }) => sideOf(r.position) === side);
    if (shown.length > 9) {
      const at = Math.max(0, shown.findIndex(({ i }) => i === cur));
      const start = Math.max(0, Math.min(at - 3, shown.length - 9));
      shown = shown.slice(start, start + 9);
    }
    el.list.classList.toggle("compact", shown.length > 6);
    el.list.classList.toggle("tiny", shown.length > 8);
    const sk = shown.map(({ r }) => r.position + "|" + r.title).join("\n");
    const sameSet = sk === sideKey;
    sideKey = sk;
    const ind = el.list.querySelector(".row-ind");
    el.list.innerHTML = shown.map(({ r, i }) => {
      const cls = i === cur ? "cur" : (r.scrobbled ? "done" : "");
      const pos = r.position || String(i + 1).padStart(2, "0");
      return `<div class="row ${cls}"><span class="pos">${esc(pos)}</span><span class="name">${esc(splitFeat(r.title).main)}</span><span class="dur">${r.duration ? mmss(r.duration) : ""}</span></div>`;
    }).join("");
    if (ind) el.list.prepend(ind);
    const curRow = el.list.querySelector(".row.cur");
    if (sameSet) rowGlide.to(curRow); else { rowGlide.place(curRow); M.arrive(el.list.querySelectorAll(".row"), { y: 16, stagger: 0.04, duration: 0.7 }); }
  }

  function renderSide(st, rows) {
    const cur = st.current_index;
    const side = cur >= 0 && rows[cur] ? sideOf(rows[cur].position) : "";
    let left = null;
    const p = position();
    if (cur >= 0 && st.track_duration && p != null) {
      left = Math.max(0, st.track_duration - p);
      for (let i = cur + 1; i < rows.length; i++) {
        if (side && sideOf(rows[i].position) !== side) break;
        if (!rows[i].duration) { left = null; break; }
        left += rows[i].duration;
      }
    }
    const head = side ? `Side ${side}` : "";
    if (left == null) { el.sidetext.textContent = head || "Now playing"; delete el.sidetext.dataset.mode; return; }
    const mins = left >= 90, n = mins ? Math.round(left / 60) : Math.round(left);
    const mode = `${head}|${mins ? "m" : "s"}`;
    if (el.sidetext.dataset.mode !== mode) {
      el.sidetext.dataset.mode = mode;
      el.sidetext.innerHTML = `${head ? esc(head) + " · " : ""}<span id="sideodo"></span> ${mins ? "min" : "s"} left`;
    }
    M.odometer(document.getElementById("sideodo"), n);
  }

  function renderEnded(e) {
    if (!e) { if (endedShown) { endedShown = false; hide(el.ended); } return; }
    const n = `${e.scrobbled} track${e.scrobbled === 1 ? "" : "s"} scrobbled`;
    el.endedKicker.textContent = e.side ? `Side ${e.side} · finished` : "Side finished";
    el.endedTitle.textContent = e.next_side ? `Flip for Side ${e.next_side}` : "That's the record";
    el.endedSub.textContent = e.next_side
      ? `${n}${e.next_title ? ` · Side ${e.next_side} opens with ${splitFeat(e.next_title).main}` : ""}`
      : `${n} · ${e.artist} · ${e.album}`;
    if (!endedShown) {
      endedShown = true;
      show(el.ended);
      M.arrive(el.endedKicker, { y: 14, duration: 0.8 });
      M.revealWords(el.endedTitle, { delay: 0.1 });
      M.arrive(el.endedSub, { y: 14, delay: 0.35, duration: 0.8 });
    }
  }

  // ── progress + lyrics clock ──────────────────────────────────────────────
  // The bar moves by transform (composited) ten times a second; the clock text
  // changes once a second; lyrics are checked every frame (cheap: an index compare).
  function lyricLead() {
    if (!trackChangedAt || (state && state.start_exact)) return LYRIC_BASE_LEAD;   // pinned: no guess to cover
    const age = (performance.now() - trackChangedAt) / 1000;
    const fresh = age < FRESH_DECAY ? FRESH_LEAD * (1 - age / FRESH_DECAY) : 0;
    return LYRIC_BASE_LEAD + fresh;
  }
  let lastSide = 0, lastBar = 0, lastSec = -1;
  function frame() {
    if (state && state.phase === "playing") {
      const p = position();
      const now = performance.now();
      if (p != null && now - lastBar > 100) {
        lastBar = now;
        const frac = state.track_duration ? Math.min(1, p / state.track_duration) : 0;
        el.fill.style.transform = `scaleX(${frac.toFixed(4)})`;
        const sec = Math.floor(p);
        if (sec !== lastSec) {
          lastSec = sec;
          el.elapsed.textContent = mmss(p);
          el.remaining.textContent = state.track_duration ? "-" + mmss(state.track_duration - p) : "";
        }
      }
      if (now - lastSide > 5000) { lastSide = now; renderSide(state, state.tracklist || []); }
      if (lyricsOn && lyrics && p != null) highlightLyric(p + lyricLead());
      if (!flip && state.start_exact && state.deck_speed && state.track_duration && serverState) {
        const raw = rawPosition();
        if (raw != null && raw >= state.track_duration + 0.4 && nextRowOf(state)) {
          flip = { index: state.current_index + 1, at: serverNow() };
          apply(serverState);
        }
      }
    }
    requestAnimationFrame(frame);
  }

  // ── lyrics ───────────────────────────────────────────────────────────────
  function warmNextLyrics(st) {
    const nxt = nextRowOf(st);
    if (!nxt) return;
    const q = new URLSearchParams({ artist: st.artist, title: nxt.title, album: st.album || "" });
    if (nxt.duration) q.set("duration", String(Math.round(nxt.duration)));
    fetch(`/api/lyrics?${q}`).catch(() => {});       // server-side cache only; rendered when it plays
  }

  async function loadLyrics(st) {
    const key = `${st.artist}|${st.title}|${st.track_duration || ""}`;
    if (key === lyricsFor && lyrics) { renderLyrics(); return; }
    lyricsFor = key; lyrics = null; lyricIndex = -1;
    el.lyriclines.innerHTML = `<div class="lyric empty">Finding lyrics…</div>`;
    M.gsap.set(el.lyriclines, { y: 0 });
    try {
      const q = new URLSearchParams({ artist: st.artist, title: st.title, album: st.album || "" });
      if (st.track_duration) q.set("duration", String(Math.round(st.track_duration)));
      const r = await fetch(`/api/lyrics?${q}`);
      const data = await r.json();
      if (key !== lyricsFor) return;
      lyrics = data;
      renderLyrics();
    } catch (e) {
      el.lyriclines.innerHTML = `<div class="lyric empty">No lyrics for this one</div>`;
    }
  }

  function renderLyrics() {
    if (!lyrics || !lyrics.synced || !lyrics.synced.length) {
      el.lyriclines.innerHTML = `<div class="lyric empty">No synced lyrics for this one</div>`;
      M.gsap.set(el.lyriclines, { y: 0 });
      return;
    }
    el.lyriclines.innerHTML = lyrics.synced.map(([, line]) => `<div class="lyric">${esc(line) || "♪"}</div>`).join("");
    lyricIndex = -1;
    if (lyricsOn) M.arrive([...el.lyriclines.children].slice(0, 12), { y: 30, stagger: 0.035, duration: 0.8 });
  }

  function highlightLyric(p) {
    const lines = lyrics.synced;
    let idx = -1;
    for (let i = 0; i < lines.length; i++) { if (lines[i][0] <= p) idx = i; else break; }
    if (idx === lyricIndex) return;
    const prev = lyricIndex;
    lyricIndex = idx;
    const nodes = el.lyriclines.children;
    for (let i = 0; i < nodes.length; i++) nodes[i].classList.toggle("cur", i === idx);
    const target = idx >= 0 ? nodes[idx] : null;
    const y = target ? target.offsetTop - 300 : 0;
    M.gsap.to(el.lyriclines, { y: -y, duration: M.D(0.55), ease: "house", overwrite: true });
    if (prev >= 0 && nodes[prev]) { M.gsap.to(nodes[prev], { scale: 1, duration: M.D(0.4), ease: "house", overwrite: true }); M.weight(nodes[prev], 540, 0.4); }
    if (target) { M.gsap.to(target, { scale: 1.22, duration: M.D(0.5), ease: "back.out(1.4)", overwrite: true }); M.weight(target, 760, 0.5); }
  }

  const cornerTray = M.tray(el.corner);
  function trayState() { cornerTray.to(sheetOpen ? el.btnInfo : (lyricsOn ? el.btnLyrics : null)); }
  function toggleLyrics(on) {
    lyricsOn = on == null ? !lyricsOn : on;
    el.btnLyrics.classList.toggle("on", lyricsOn);
    el.lyrics.classList.toggle("hidden", !lyricsOn);
    el.list.classList.toggle("hidden", lyricsOn);
    trayState();
    if (lyricsOn && state) { loadLyrics(state); if (lyrics) M.arrive([...el.lyriclines.children].slice(0, 12), { y: 30, stagger: 0.035, duration: 0.8 }); }
    else if (!lyricsOn) M.arrive(el.list, { y: 16, duration: 0.7 });
  }

  // ── info sheet ───────────────────────────────────────────────────────────
  async function openSheet() {
    if (!state || state.phase !== "playing") return;
    sheetOpen = true;
    el.btnInfo.classList.add("on");
    trayState();
    el.body.classList.add("sheet-open");
    el.sheet.classList.remove("hidden");
    requestAnimationFrame(() => el.sheet.classList.add("open"));
    await loadInfo(true);
  }

  async function loadInfo(opening = false) {
    const albumKey = `${state.artist}|${state.album}`;
    const trackKey2 = `${state.artist}|${state.title}`;
    const needAlbum = albumKey !== infoFor, needTrack = trackKey2 !== trackFor;
    if (!needAlbum && !needTrack) { renderInfo(opening); return; }
    if (needAlbum) { infoFor = albumKey; info = null; }
    if (needTrack) { trackFor = trackKey2; trackInfo = null; }
    renderInfo(opening);
    const jobs = [];
    if (needAlbum) jobs.push(fetch(`/api/info?${new URLSearchParams({ artist: state.artist, album: state.album || "" })}`)
      .then((r) => r.json()).then((d) => { if (albumKey === infoFor) info = d; }).catch(() => {}));
    if (needTrack) jobs.push(fetch(`/api/track?${new URLSearchParams({ artist: state.artist, title: state.title })}`)
      .then((r) => r.json()).then((d) => { if (trackKey2 === trackFor) trackInfo = d; }).catch(() => {}));
    await Promise.all(jobs);
    renderInfo(false);
  }

  function closeSheet() {
    if (!sheetOpen) return;
    sheetOpen = false;
    countedFor.clear();
    el.btnInfo.classList.remove("on");
    trayState();
    el.body.classList.remove("sheet-open");
    el.sheet.classList.remove("open");
    setTimeout(() => { if (!sheetOpen) el.sheet.classList.add("hidden"); }, 620);
  }

  const sec = (h, body) => `<div class="sec"><h2>${esc(h)}</h2>${body}</div>`;
  const countedFor = new Set();
  // one scroll listener for the sheet; the masthead it moves is whichever is rendered now
  const sheetParallax = M.parallaxHead(document.getElementById("sheetscroll"), () => el.tiles.querySelector(".mast"));

  function fmtShort(formats) {
    const f = (formats || []).join(", ")
      .replace(/Vinyl, LP, Album/gi, "LP").replace(/Vinyl, LP/gi, "LP").replace(/, Album/gi, "");
    return f.split(", ").slice(0, 3).join(", ");          // "2 × LP, Deluxe Edition, Limited Edition"
  }

  function creditLines(pr, g) {
    const out = [];
    const byTrack = (pr && state && state.position && pr.credits_by_track) ? pr.credits_by_track[state.position] : null;
    const src = (byTrack && byTrack.length) ? byTrack : (pr ? pr.credits : null);
    const clean = (n) => (n || "").replace(/\s*\(\d+\)\s*$/, "");   // Discogs "Southside (5)" → "Southside"
    const names = (re) => { const seen = []; (src || []).forEach((c) => { const n = clean(c.name); if (re.test(c.role) && n && !seen.includes(n)) seen.push(n); }); return seen; };
    if (src) {
      const prod = names(/produc/i).slice(0, 4);
      const feat = names(/featur/i);
      const mix = names(/^mixed by$/i);
      const wrote = names(/written/i).slice(0, 6);
      if (prod.length) out.push(`<span class="lab">Produced by</span> <b>${esc(prod.join(", "))}</b>`);
      const row2 = [];
      if (feat.length) row2.push(`<span class="lab">Featuring</span> <b>${esc(feat.join(", "))}</b>`);
      if (mix.length) row2.push(`<span class="lab">Mixed by</span> <b>${esc(mix[0])}</b>`);
      if (row2.length) out.push(row2.join(" &nbsp;·&nbsp; "));
      if (wrote.length) out.push(`<span class="lab">Written by</span> ${esc(wrote.join(", "))}`);
    }
    if (!out.length && g.producers && g.producers.length)
      out.push(`<span class="lab">Produced by</span> <b>${esc(g.producers.join(", "))}</b>`);
    if (!out.length) return (trackInfo && info) ? "No credits listed" : "Loading…";
    return out.join("<br>");
  }

  function sampleChip(sm) {
    const art = sm.art
      ? `<img src="${esc(sm.art)}" onerror="this.outerHTML='<div class=\\'ph\\'>${esc((sm.artist || sm.title || "?")[0] || "?")}</div>'">`
      : `<div class="ph">${esc((sm.artist || sm.title || "?")[0] || "?")}</div>`;
    return `<div class="chip">${art}<div><div class="st">${esc(sm.title || "")}</div><div class="sb">${esc(sm.artist || "")}</div></div></div>`;
  }

  function renderInfo(opening) {
    const pr = info && info.pressing, g = trackInfo || {}, loading = !info;
    const art = (state && state.art) ? `/art/${state.art}.jpg` : "";
    const kick = (state && state.position) ? `The record · ${esc(state.position)}` : "The record";
    const label = (pr && pr.labels && pr.labels.length) ? pr.labels[0] : null;
    const press = pr
      ? [esc((state && state.artist) || ""), label ? esc(label.name) : "", esc(pr.country),
         pr.year ? String(pr.year) : "", esc(fmtShort(pr.formats))].filter(Boolean).join(" · ")
      : (loading ? "Loading…" : esc((state && state.artist) || ""));
    let priceN = "", priceS = "";
    if (pr && pr.typical_usd != null) { priceN = `$${Math.round(pr.typical_usd)}`; priceS = "median · VG+ on Discogs"; }
    else if (pr && pr.price_usd != null) { priceN = `$${Math.round(pr.price_usd)}`; priceS = `lowest · ${pr.for_sale || 0} for sale`; }
    const priceHtml = priceN ? `<div class="mprice"><div class="n">${priceN}</div><div class="s">${priceS}</div></div>` : "";
    const mast = `<div class="mast">${art ? `<img class="mcover" src="${art}">` : ""}` +
      `<div><div class="mkick">${kick}</div><h1>${esc((state && state.album) || "")}</h1><div class="msub">${press}</div></div>${priceHtml}</div>`;

    const isTrans = g.url && /[çc]eviri|traduc|translation|перевод/i.test(g.url);
    const story = (g.story && !isTrans) ? esc(g.story).replace(/\n\n/g, "<br><br>")
      : (trackInfo ? "Nothing written up for this one" : "Loading…");
    const credits = creditLines(pr, g);
    const samples = (g.samples && g.samples.length) ? g.samples.map(sampleChip).join("") : "";

    const pl = (info && info.plays) || {}, tp = g.plays || {};
    const trackLine = tp.plays != null ? `<b>${tp.plays}</b> ${tp.plays === 1 ? "play" : "plays"} of this track` : "";
    const albumLine = pl.user_plays != null ? `<b>${pl.user_plays}</b> plays of the record` : (info ? "first spin counted" : "Loading…");
    let hist = `<div class="hist">${trackLine || (albumLine + " on Last.fm")}`;
    if (trackLine) hist += `<div class="row2 imuted">${albumLine} on Last.fm</div>`;
    const subs = [];
    if (pl.last_played) subs.push(`last played ${new Date(pl.last_played * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`);
    if (tp.listeners || pl.listeners) subs.push(`${fmtCount(tp.listeners || pl.listeners)} listeners`);
    if (subs.length) hist += `<div class="sub">${subs.join(" · ")}</div>`;
    hist += "</div>";

    const about = (info && info.story && info.story.text) ? esc(info.story.text) : "";
    el.tiles.innerHTML = mast + `<div class="cols"><div class="col">` +
      sec(`On this track · ${state ? splitFeat(state.title).main : ""}`, `<div class="story">${story}</div>`) +
      sec("Credits", `<div class="cred">${credits}</div>`) +
      `</div><div class="col">` +
      (samples ? sec("Samples", `<div class="samples">${samples}</div>`) : "") +
      sec("Your history", hist) +
      (about ? sec("About the album", `<div class="story sm">${about}</div>`) : "") +
      `</div></div>`;
    if (opening) M.arrive([el.tiles.querySelector(".mast"), ...el.tiles.querySelectorAll(".sec")], { y: 28, stagger: 0.06, delay: 0.15 });
    else M.gsap.fromTo(el.tiles, { autoAlpha: 0.55 }, { autoAlpha: 1, duration: M.D(0.4), ease: "power1.inOut" });
    sheetParallax();
    const num = el.tiles.querySelector(".hist b");
    // the plays roll up once per opening of the sheet (the prefetch renders while it is hidden)
    if (num && info && sheetOpen && !countedFor.has(infoFor + "|" + trackFor)) { countedFor.add(infoFor + "|" + trackFor); M.countUp(num, num.textContent, { delay: 0.25 }); }
  }

  // ── nothing on the platter: the controls ─────────────────────────────────
  const IDLE_CONTROLS_MS = 12000;
  let idleOpen = false, idleTimer = 0;
  function showIdle() {
    if (!state || state.phase !== "idle" || pickOpen || shelfOpen || recentOpen) return;
    if (idleOpen) { armIdleTimer(); return; }
    idleOpen = true;
    show(el.idle);
    M.arrive([el.idle.querySelector(".kicker"), el.btnPick, el.idle.querySelector(".quiet")], { y: 24, stagger: 0.09 });
    armIdleTimer();
    scheduleShelfPrep(1200);
  }
  function armIdleTimer() { clearTimeout(idleTimer); idleTimer = setTimeout(hideIdle, IDLE_CONTROLS_MS); }
  function hideIdle() {
    clearTimeout(idleTimer);
    if (!idleOpen) return;
    idleOpen = false;
    M.leave(el.idle, { duration: 0.35, onComplete: () => { if (!idleOpen) hide(el.idle); } });
  }

  // ── the shelf data ───────────────────────────────────────────────────────
  let collection = null;                  // [{id, artist, title, year, released, formats, added, cover}]
  let collectionLoad = null;
  async function loadCollection() {
    if (collection) return collection;
    if (!collectionLoad) {
      collectionLoad = fetch("/api/collection").then((r) => r.json())
        .then((d) => { collection = d.releases || []; return collection; })
        .catch(() => { collection = []; return collection; })
        .finally(() => { collectionLoad = null; });
    }
    return collectionLoad;
  }
  // Every cover is fetched and decoded once, a few at a time, as soon as the page is up,
  // and the Image objects are kept so the browser cannot drop them: the wheel and the
  // shelf then open with the pictures already there instead of loading them on the spot.
  const covers = new Map();               // cover url → Image (decoded)
  let preloading = false;
  async function preloadCovers() {
    if (preloading) return;
    preloading = true;
    try {
      const col = await loadCollection();
      if (!shelfOpen) { buildGrid(col); scheduleShelfPrep(3000); }   // see prepareShelf
      const urls = col.flatMap((r) => (r.cover ? [r.cover, tileUrl(r.cover), blurUrl(r.cover)] : [])).filter((u) => !covers.has(u));
      const lane = async () => {
        while (urls.length) {
          const u = urls.shift();
          const img = new Image();
          img.decoding = "async";
          img.src = u;
          covers.set(u, img);
          try { await img.decode(); } catch (e) { covers.delete(u); }
        }
      };
      await Promise.all([lane(), lane(), lane()]);
    } finally { preloading = false; }
  }
  const ready = (urls, ms) => Promise.race([
    Promise.all(urls.map((u) => { const img = covers.get(u) || Object.assign(new Image(), { src: u }); return img.decode().catch(() => {}); })),
    new Promise((res) => setTimeout(res, ms)),
  ]);

  // ── the pick ─────────────────────────────────────────────────────────────
  let pickOpen = false, pickCard = null, lastPickId = 0, pickSerial = 0, pickFromShelf = false, wheelTl = null;
  async function startPick() {
    hideIdle();
    const serial = ++pickSerial;
    const again = pickOpen && !el.landed.classList.contains("hidden");   // "Another one": a card is up
    pickOpen = true; pickFromShelf = false;
    if (wheelTl) { wheelTl.kill(); wheelTl = null; }
    show(el.pick);
    if (again) {
      setBackdrop("");                                                    // the old record's colours go with its card
      await M.leave(el.landed, { duration: 0.3 }).then();
      if (serial !== pickSerial) return;
    }
    hide(el.landed);
    M.gsap.set(el.landed, { clearProps: "opacity,visibility" });
    show(el.wheel);
    M.gsap.set(el.wheelKicker, { autoAlpha: 0 });
    el.wheelstrip.innerHTML = "";
    // the draw is a memory lookup and comes back at once; the card (pressing from Discogs,
    // plays from Last.fm) takes a second or two and is fetched behind the roll
    const drawP = fetch(`/api/collection/draw?exclude=${lastPickId}`).then((r) => r.json()).catch(() => null);
    const col = await loadCollection();
    el.wheelKicker.textContent = col.length ? `Picking one of ${col.length}` : "Picking one";
    M.arrive(el.wheelKicker, { y: 12, duration: 0.7, delay: 0.3 });   // after the controls have left
    const row = await drawP;
    if (serial !== pickSerial || !pickOpen) return;
    if (!row || row.error) { el.wheelKicker.textContent = "The shelf is empty"; return; }
    const cardP = fetch(`/api/collection/${row.id}`).then((r) => r.json()).catch(() => null);
    // the covers that wheel past: three sit left of the start so the ring is full from the
    // first frame, nine roll by, the hit lands, two show past it
    const others = sample(col.filter((r) => r.cover && r.id !== row.id), 14);
    const tiles = [...others.slice(0, 12), row, ...others.slice(12, 14)];
    const hit = 12;
    // tile-sized copies for the ring, the full cover for the hit (it flies into the card)
    el.wheelstrip.innerHTML = tiles.map((r, i) => `<img src="${esc(i === hit ? r.cover : tileUrl(r.cover))}" alt="">`).join("");
    await ready(tiles.map((r, i) => (i === hit ? r.cover : tileUrl(r.cover))).filter(Boolean), 400);
    if (serial !== pickSerial || !pickOpen) return;
    const imgs = [...el.wheelstrip.children];
    wheelTl = M.wheel(el.wheelstrip, imgs, hit, {
      onLand: async () => {
        if (serial !== pickSerial || !pickOpen) return;
        const card = (await Promise.race([cardP, new Promise((res) => setTimeout(() => res(null), 60))])) || row;
        land(card, false, imgs[hit]);
        if (!card.pressing) cardP.then((full) => { if (full && !full.error && serial === pickSerial && pickCard && pickCard.id === full.id) fillCard(full); });
      },
    });
  }

  // the card's slower facts (pressing, plays) arriving after the landing
  function cardLines(card) {
    const pr = card.pressing || {};
    const year = card.released || pr.year || card.year;
    const fmt = (pr.formats && pr.formats.length) ? fmtShort(pr.formats) : (card.formats || []).join(", ");
    const meta = [year ? String(year) : "", fmt, card.added ? `on the shelf since ${fmtDate(card.added)}` : ""].filter(Boolean).join(" · ");
    const pl = card.plays || {};
    const plays = pl.user_plays != null ? `${pl.user_plays} ${pl.user_plays === 1 ? "play" : "plays"} on Last.fm` : "";
    const last = pl.last_played ? `last on ${fmtDay(pl.last_played)}` : "";
    return { meta, plays: [plays, last].filter(Boolean).join(" · ") };
  }
  function fillCard(full) {
    pickCard = full;
    const lines = cardLines(full);
    const swap = (node, text) => { if (text === node.textContent) return;
      M.fade(node, 0, { duration: 0.18 }).then(() => { node.textContent = text; M.fade(node, 1, { duration: 0.45, ease: "expo.out" }); }); };
    swap(el.pickMeta, lines.meta);
    if (lines.plays) swap(el.pickPlays, lines.plays);
  }

  function land(card, fromShelf = false, fromTile = null) {
    pickCard = card; lastPickId = card.id; pickFromShelf = fromShelf;
    pickOpen = true;
    show(el.pick);
    el.pickArt.src = card.cover || "";
    el.pickKicker.textContent = fromShelf ? "On the shelf" : "From the shelf";
    el.pickTitle.textContent = card.title || "";
    el.pickArtist.textContent = card.artist || "";
    const lines = cardLines(card);
    el.pickMeta.textContent = lines.meta;
    el.pickPlays.textContent = lines.plays;
    el.pickHint.textContent = "Pull it off the shelf. The screen wakes when the needle drops.";
    el.btnPickDone.textContent = fromShelf ? "Back to the shelf" : "Done";
    show(el.landed);
    fitTitle(el.pickTitle, 108, 48);
    // the blurred backdrop (a 64 px blur of a full screen) is the one expensive paint of the
    // build, so it blooms after the cover has landed rather than under the hand-over
    if (fromTile) {
      // the hit cover on the wheel becomes the card's cover: one move, the kicker fades under it
      M.fade(el.wheelKicker, 0, { duration: 0.3 });
      M.handOver(fromTile, el.pickArt, { onDone: () => { if (pickOpen) { hide(el.wheel); setBackdrop(card.cover || ""); } } });
    } else {
      hide(el.wheel);
      M.arrive(el.pickArt, { y: 30, scale: 0.94, duration: 1.1 });
      setTimeout(() => { if (pickOpen) setBackdrop(card.cover || ""); }, 500);
    }
    M.arrive(el.pickKicker, { y: 12, duration: 0.8, delay: 0.05 });
    M.revealLines(el.pickTitle, { delay: 0.12 });
    M.revealLines(el.pickArtist, { delay: 0.22 });
    M.arrive([el.pickMeta, el.pickPlays], { y: 14, stagger: 0.07, delay: 0.38, duration: 0.8 });
    M.arrive([el.btnAnother, el.btnPickDone], { y: 18, scale: 0.92, stagger: 0.08, delay: 0.5 });
    M.arrive(el.pickHint, { y: 10, delay: 0.75, duration: 0.8 });
  }

  function closePick() {
    pickSerial++;
    if (wheelTl) { wheelTl.kill(); wheelTl = null; }
    pickOpen = false; pickCard = null;
    M.leave(el.pick, { duration: 0.3, onComplete: () => { if (!pickOpen) { hide(el.pick); hide(el.landed); hide(el.wheel); } } });
    if (!state || state.phase !== "playing") setBackdrop("");
  }

  // ── the shelf ────────────────────────────────────────────────────────────
  let shelfOpen = false, shelfSort = "recent";
  const sortTray = M.tray(el.shelfSort);
  const sorted = (col) => {
    const out = col.slice();
    const by = (f) => (a, b) => f(a).localeCompare(f(b), "en", { sensitivity: "base" });
    if (shelfSort === "title") out.sort(by((r) => r.title));
    else if (shelfSort === "artist") out.sort((a, b) => by((r) => r.artist)(a, b) || (a.released || a.year || 0) - (b.released || b.year || 0));
    else if (shelfSort === "price") out.sort((a, b) => (b.price ?? -1) - (a.price ?? -1) || by((r) => r.title)(a, b));
    return out;
  };
  const priceTag = (r) => (r.price != null ? `$${Math.round(r.price)}` : "");
  const tile = (r) => `<div class="shelfitem" data-id="${r.id}"><img src="${esc(tileUrl(r.cover))}" alt="${esc(r.title)}" decoding="async"><span class="price">${priceTag(r)}</span></div>`;
  // ── the shelf grid ───────────────────────────────────────────────────────
  // DOM order is the collection's own (newest first) and is built once; a sort is a permutation
  // laid over it with transforms, so the tiles' layers move and nothing is laid out or painted
  // again. The geometry mirrors style.css: 216 px cells, 40 px row gap, seven columns spread
  // edge to edge.
  const CELL = 216, ROW = 256, COLS = 7;
  let shelfKey = "", placedSort = "recent";
  const gridKey = (col) => col.map((r) => `${r.id}:${r.price ?? ""}`).join(",");
  function buildGrid(col) {
    const key = gridKey(col);
    if (key === shelfKey) return;
    shelfKey = key; placedSort = "recent";
    el.shelfgrid.innerHTML = col.map(tile).join("") || `<div class="empty">Nothing on the shelf yet</div>`;
  }
  const slotPos = (i, step) => ({ x: (i % COLS) * step, y: Math.floor(i / COLS) * ROW });
  const slotRow = (n) => Math.floor((+n.dataset.slot || 0) / COLS);
  const rowsOnScreen = () => Math.ceil(el.shelfgrid.clientHeight / ROW) + 1;
  // Every tile to its slot under the current sort; the ones on screen before or after glide.
  function placeShelf(animate) {
    if (!collection || !collection.length) return null;
    const kids = [...el.shelfgrid.children];
    const step = (el.shelfgrid.clientWidth - CELL) / (COLS - 1);
    const rows = rowsOnScreen(), topRow = Math.floor(el.shelfgrid.scrollTop / ROW), scrolled = el.shelfgrid.scrollTop;
    const slotOf = new Map(sorted(collection).map((r, v) => [String(r.id), v]));
    const seen = (slot, first) => { const row = Math.floor(slot / COLS); return row >= first && row < first + rows; };
    const moving = [], from = [], to = [];
    M.gsap.killTweensOf(kids, "x,y");
    kids.forEach((n, d) => {
      const v = slotOf.get(n.dataset.id);
      if (v == null) return;
      const was = n.dataset.slot === undefined ? d : +n.dataset.slot;
      n.dataset.slot = v;
      const p = slotPos(v, step), q = slotPos(d, step), w = slotPos(was, step);
      const target = { x: p.x - q.x, y: p.y - q.y };
      if (animate && (seen(was, topRow) || seen(v, 0))) { moving.push(n); from.push({ x: w.x - q.x, y: w.y - q.y - scrolled }); to.push(target); }
      else M.gsap.set(n, target);
    });
    el.shelfgrid.scrollTop = 0;
    placedSort = shelfSort;
    return M.glide(moving, from, to);
  }
  const visualOrder = () => [...el.shelfgrid.children].sort((a, b) => (+a.dataset.slot || 0) - (+b.dataset.slot || 0));
  // The shelf is laid out and layered while nobody is looking (a quiet moment after the idle
  // controls arrive), so the tap that opens it pays for nothing but the wave.
  let prepTimer = 0;
  function scheduleShelfPrep(ms) { clearTimeout(prepTimer); prepTimer = setTimeout(prepareShelf, ms); }
  function prepareShelf() {
    if (shelfOpen || pickOpen || !collection || !state || state.phase !== "idle" || !el.shelf.classList.contains("hidden")) return;
    buildGrid(collection);
    el.shelf.classList.remove("hidden");
    M.gsap.set(el.shelf, { autoAlpha: 0 });
  }
  function unprepareShelf() {
    if (shelfOpen || el.shelf.classList.contains("hidden")) return;
    hide(el.shelf);
    M.gsap.set(el.shelf, { clearProps: "opacity,visibility" });
  }
  async function openShelf() {
    hideIdle();
    shelfOpen = true;
    show(el.shelf);
    const col = await loadCollection();
    el.shelfCount.textContent = col.length ? `· ${col.length} records` : "";
    buildGrid(col);
    if (placedSort !== shelfSort) placeShelf(false);
    el.shelfgrid.classList.toggle("priced", shelfSort === "price");
    el.shelfgrid.scrollTop = 0;
    M.arrive(el.shelf.querySelector(".shelfhead"), { y: -18, duration: 0.8 });
    M.waveIn(visualOrder().slice(0, 21));
    [...el.shelfSort.children].forEach((b) => b.classList.toggle("on", b.dataset.sort === shelfSort));
    sortTray.place(el.shelfSort.querySelector(`[data-sort="${shelfSort}"]`));
  }
  function resortShelf(sort) {
    if (sort === shelfSort || !collection) return;
    shelfSort = sort;
    [...el.shelfSort.children].forEach((b) => b.classList.toggle("on", b.dataset.sort === shelfSort));
    sortTray.to(el.shelfSort.querySelector(`[data-sort="${shelfSort}"]`));
    const priced = shelfSort === "price";
    if (!priced) el.shelfgrid.classList.remove("priced");             // the tags fall away as the tiles set off
    const tl = placeShelf(true);
    const tagsIn = () => {                                            // ...and come back once everything has landed
      if (!priced || shelfSort !== "price") return;
      el.shelfgrid.classList.add("priced");
      const rows = rowsOnScreen();
      const pills = [...el.shelfgrid.children].filter((n) => slotRow(n) < rows).map((n) => n.querySelector(".price")).filter((x) => x && x.textContent);
      M.gsap.fromTo(pills, { autoAlpha: 0 }, { autoAlpha: 1, duration: M.D(0.35), stagger: 0.012, clearProps: "opacity,visibility" });
    };
    if (tl) tl.eventCallback("onComplete", tagsIn); else tagsIn();
  }
  // Closed, the shelf stays laid out (faded to nothing) while the platter is idle; see prepareShelf.
  function closeShelf() {
    shelfOpen = false;
    M.leave(el.shelf, { duration: 0.3, onComplete: () => { if (!shelfOpen && (!state || state.phase !== "idle")) unprepareShelf(); } });
  }

  async function openCard(id, fromShelf) {
    // the row is in memory: open at once, the pressing and the plays follow
    const row = (collection || []).find((r) => r.id === id);
    const cardP = fetch(`/api/collection/${id}`).then((r) => r.json()).catch(() => null);
    const first = row || await cardP;
    if (!first || first.error) return;
    if (fromShelf) closeShelf();
    closeRecent();
    land(first, fromShelf);
    cardP.then((full) => { if (full && !full.error && pickCard && pickCard.id === full.id) fillCard(full); });
  }

  // ── recently played ──────────────────────────────────────────────────────
  let recentOpen = false;
  async function openRecent() {
    hideIdle();
    recentOpen = true;
    show(el.recent);
    M.arrive(el.recent.querySelector(".shelfhead"), { y: -18, duration: 0.8 });
    el.recentlist.innerHTML = `<div class="empty">Looking back…</div>`;
    let rows = [];
    try { rows = await (await fetch("/api/history/albums?limit=12")).json(); } catch (e) { rows = []; }
    if (!recentOpen) return;
    if (!Array.isArray(rows) || !rows.length) { el.recentlist.innerHTML = `<div class="empty">Nothing has been on yet</div>`; return; }
    el.recentlist.innerHTML = rows.map((r) => {
      const rel = r.release;
      const art = rel && rel.cover ? `<img src="${esc(tileUrl(rel.cover))}" alt="">` : `<div class="ph"></div>`;
      const n = r.whole ? "Whole record" : (r.sides && r.sides.length) ? `Side${r.sides.length > 1 ? "s" : ""} ${r.sides.join(" and ")}`
        : `${r.tracks} track${r.tracks === 1 ? "" : "s"}`;
      return `<div class="rrow ${rel ? "" : "off"}" ${rel ? `data-id="${rel.id}"` : ""}>${art}<div><div class="rt">${esc(r.album)}</div><div class="ra">${esc(r.artist)}</div></div><div class="rw">${esc(fmtDay(r.last_played))}<br>${n}</div></div>`;
    }).join("");
    M.arrive(el.recentlist.children, { x: -26, y: 0, stagger: 0.06, duration: 0.8 });
  }
  function closeRecent() { recentOpen = false; M.leave(el.recent, { duration: 0.3, onComplete: () => { if (!recentOpen) hide(el.recent); } }); }

  function closeOverlays() { hideIdle(); closePick(); closeShelf(); closeRecent(); }

  // ── gestures ─────────────────────────────────────────────────────────────
  // One pointer: a horizontal drag moves the pages; a vertical swipe opens or closes
  // the info sheet; anything shorter than 14 px is a tap.
  let down = null;
  document.addEventListener("pointerdown", (e) => {
    down = { x: e.clientX, y: e.clientY, t: Date.now(), target: e.target, drag: false };
    if (idleOpen) armIdleTimer();
  }, { passive: true });
  document.addEventListener("pointermove", (e) => {
    if (!down) return;
    const dx = e.clientX - down.x, dy = e.clientY - down.y;
    if (!down.drag) {
      if (sheetOpen || moving || Math.abs(dx) < 16 || Math.abs(dx) < Math.abs(dy) * 1.3) return;
      down.drag = true;
      showDots(0);
      if (pageIndex !== SPINDLE) showIdle();
    }
    down.dx = dx;
    dragTo(dx);
  }, { passive: true });
  document.addEventListener("pointerup", (e) => {
    if (!down) return;
    const dx = e.clientX - down.x, dy = e.clientY - down.y, dt = Date.now() - down.t;
    const start = down; down = null;
    if (start.drag) {
      const speed = Math.abs(dx) / Math.max(1, dt);                       // px per ms
      if (Math.abs(dx) > W() * 0.22 || speed > 0.6) showPage(neighbour(dx < 0 ? 1 : -1), dx < 0 ? 1 : -1, Math.min(1, Math.abs(dx) / W()), speed);
      else snapBack(dx);
      return;
    }
    if (Math.abs(dy) > 100 && Math.abs(dx) < 160 && dt < 1500) {
      if (pageIndex !== SPINDLE) return;
      if (dy < 0 && !sheetOpen && !pickOpen && !shelfOpen && !recentOpen) openSheet();
      else if (dy > 0 && sheetOpen && sheetAtTop()) closeSheet();
      return;
    }
    if (Math.abs(dx) < 14 && Math.abs(dy) < 14) tap(start);
  }, { passive: true });
  document.addEventListener("pointercancel", () => {
    if (down && down.drag) snapBack(down.dx || 0);
    down = null;
  });
  // The scroller owns vertical pans (touch-action: pan-y), so a pull-down on an open
  // sheet reaches us as touchmove, not pointerup: close on a pull past the top.
  let pull = null;
  document.addEventListener("touchstart", (e) => {
    pull = sheetOpen && e.touches.length === 1 ? { y: e.touches[0].clientY, top: sheetAtTop() } : null;
  }, { passive: true });
  document.addEventListener("touchmove", (e) => {
    if (!pull || !pull.top || !sheetOpen) return;
    if (e.touches[0].clientY - pull.y > 110) { pull = null; closeSheet(); }
  }, { passive: true });
  const sheetAtTop = () => document.getElementById("sheetscroll").scrollTop <= 2;
  const closeZone = (t) => !!t.closest(".mast, .grab");
  const hit = (t, node) => !!(node && (node === t || node.contains(t)));

  function tap(start) {
    const target = start.target;
    if (state && state.ended && state.ended.at !== endedDismissed) {
      // "Flip for Side B" / "That's the record" tapped: he is not playing the rest, go dark
      endedDismissed = state.ended.at;
      apply(state);
      if (pageIndex !== OFF) showPage(OFF, -1);
      return;
    }
    if (pageIndex !== SPINDLE) { showDots(1800); if (pageIndex === OFF) flashOff(); return; }
    if (sheetOpen) { if (!el.sheet.contains(target) || closeZone(target) || start.y < 140) closeSheet(); return; }
    if (pickOpen) {
      if (hit(target, el.btnAnother)) { startPick(); return; }
      if (hit(target, el.btnPickDone)) { const back = pickFromShelf; closePick(); if (back) openShelf(); return; }
      return;
    }
    if (shelfOpen) {
      if (hit(target, el.btnShelfClose)) { closeShelf(); return; }
      const chip = target.closest("[data-sort]");
      if (chip) { resortShelf(chip.dataset.sort); return; }
      const cover = target.closest("[data-id]");
      if (cover) openCard(Number(cover.dataset.id), true);
      return;
    }
    if (recentOpen) {
      if (hit(target, el.btnRecentClose)) { closeRecent(); return; }
      const row = target.closest("[data-id]");
      if (row) openCard(Number(row.dataset.id), false);
      return;
    }
    if (idleOpen) {
      if (hit(target, el.btnPick)) { startPick(); return; }
      if (hit(target, el.btnShelf)) { openShelf(); return; }
      if (hit(target, el.btnRecent)) { openRecent(); return; }
      hideIdle();
      return;
    }
    if (state && state.phase === "idle") { showIdle(); return; }
    if (!state || state.phase !== "playing") return;
    if (hit(target, el.btnLyrics) || hit(target, el.title) || hit(target, el.artist)) { toggleLyrics(); return; }
    if (hit(target, el.btnInfo)) { openSheet(); return; }
    if (el.lyrics.contains(target)) { toggleLyrics(false); }
  }

  document.addEventListener("contextmenu", (e) => e.preventDefault());
  window.addEventListener("resize", rest);

  // ── boot ─────────────────────────────────────────────────────────────────
  document.fonts.ready.then(() => M.boot(el.boot, el.bootword));

  // ── stream ───────────────────────────────────────────────────────────────
  // After the engine restarts, its caches are fresh and ours are stale: drop the
  // sheet data on reconnect and reload it if the sheet is open.
  let reconnected = false;
  function connect() {
    const es = new EventSource("/events");
    es.onmessage = (e) => {
      try {
        if (reconnected) { reconnected = false; infoFor = ""; trackFor = ""; info = null; trackInfo = null; collection = null; }
        apply(JSON.parse(e.data));
        if (sheetOpen && !info) loadInfo();
        if (!collection && !collectionLoad) preloadCovers();
      } catch (err) { console.error(err); }
    };
    es.onerror = () => { es.close(); reconnected = true; setTimeout(connect, 2000); };
  }
  connect();
  requestAnimationFrame(frame);
})();
