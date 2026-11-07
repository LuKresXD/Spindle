/* Spindle touch display client.
   One EventSource carries the engine's ScreenState; the page interpolates the
   progress between ticks, fetches lyrics and the info sheet on demand, and
   turns swipes and taps into the few things the screen can do. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = {
    body: document.body, bg: $("bg"), home: $("home"), art: $("art"), title: $("title"),
    artist: $("artist"), albumline: $("albumline"), sidetext: $("sidetext"), fill: $("fill"),
    elapsed: $("elapsed"), remaining: $("remaining"), list: $("list"), lyrics: $("lyrics"),
    lyriclines: $("lyriclines"), btnLyrics: $("btn-lyrics"), btnInfo: $("btn-info"),
    listening: $("listening"), levelfill: $("levelfill"), sheet: $("sheet"), tiles: $("tiles"),
    ended: $("ended"), endedKicker: $("ended-kicker"), endedTitle: $("ended-title"),
    endedSub: $("ended-sub"), off: $("off"),
  };

  const BULK_IMPORT_DAY = "2025-04-17";   // the day the first 46 records were entered at once
  let state = null;
  let clockOffset = 0;                    // client seconds minus server seconds
  let artKey = "";
  let listKey = "";
  let trackKey = "";
  let lyricsOn = false;
  let lyrics = null;                      // {synced: [[t, line]], plain}
  let lyricsFor = "";
  let lyricIndex = -1;
  let infoFor = "";
  let info = null;                        // album info (pressing, story, plays)
  let trackFor = "";
  let trackInfo = null;                   // Genius: story, samples, producers
  const LYRIC_LEAD = 0.6;                 // show a line a touch early rather than late
  let sheetOpen = false;
  let screenOff = false;

  // ── helpers ──────────────────────────────────────────────────────────────
  const mmss = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const serverNow = () => Date.now() / 1000 - clockOffset;
  const sideOf = (pos) => (pos && /^[A-Za-z]/.test(pos)) ? pos[0].toUpperCase() : "";
  const fmtDate = (iso) => {
    if (!iso) return "";
    if (iso.slice(0, 10) === BULK_IMPORT_DAY) return iso.slice(0, 4);
    const d = new Date(iso.slice(0, 10) + "T12:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  };

  function position() {
    if (!state || state.track_elapsed == null) return null;
    const speed = state.deck_speed || 1;
    let p = state.track_elapsed + Math.max(0, serverNow() - state.updated_at) * speed;
    if (state.track_duration) p = Math.min(p, state.track_duration);
    return p;
  }

  // ── phases ───────────────────────────────────────────────────────────────
  function apply(st) {
    state = st;
    clockOffset = Date.now() / 1000 - st.server_time;
    el.body.className = `phase-${st.phase}` + (sheetOpen ? " sheet-open" : "") + (st.ended ? " ended-open" : "");
    const playing = st.phase === "playing";
    el.home.classList.toggle("hidden", !playing && !st.ended);
    requestAnimationFrame(() => el.home.classList.toggle("shown", playing || !!st.ended));
    el.listening.classList.toggle("hidden", st.phase !== "listening");
    if (st.phase === "listening") {
      const pct = Math.max(0, Math.min(100, (st.level_db + 60) / 60 * 100));
      el.levelfill.style.width = pct + "%";
    }
    if (playing) renderHome(st);
    renderEnded(st);
    if (!playing && !st.ended) { closeSheet(); }
  }

  function renderHome(st) {
    const key = `${st.artist}|${st.title}|${st.album}`;
    if (key !== trackKey) {
      trackKey = key;
      el.title.textContent = st.title || "";
      fitTitle();
      el.artist.textContent = st.artist || "";
      lyricIndex = -1;
      if (lyricsOn) loadLyrics(st);
      if (sheetOpen) loadInfo();
    }
    el.albumline.textContent = [st.album, st.year].filter(Boolean).join(" · ");

    if (st.art && st.art !== artKey) {
      artKey = st.art;
      const url = `/art/${st.art}.jpg`;
      el.art.classList.add("swap");
      const img = new Image();
      img.onload = () => {
        el.art.src = url;
        el.bg.style.backgroundImage = `url("${url}")`;
        el.bg.classList.add("ready");
        requestAnimationFrame(() => el.art.classList.remove("swap"));
      };
      img.src = url;
    } else if (!st.art && artKey) {
      artKey = ""; el.art.removeAttribute("src"); el.bg.style.backgroundImage = ""; el.bg.classList.remove("ready");
    }

    const rows = st.tracklist || [];
    const lk = rows.map((r, i) => `${r.position}|${r.title}|${r.scrobbled ? 1 : 0}|${i === st.current_index ? 1 : 0}`).join("\n");
    if (lk !== listKey) {
      listKey = lk;
      renderList(st, rows);
    }
    renderSide(st, rows);
  }

  // Shrink the title until no word has to break (Plasticworld came out as "Plasticworl / d").
  function fitTitle() {
    const max = 108, min = 44;
    let size = max;
    el.title.style.fontSize = size + "px";
    const fits = () => { const w = el.title.clientWidth; return [...el.title.childNodes].length === 0 ||
      Array.from(el.title.textContent.split(/\s+/)).every((word) => measure(word, size) <= w); };
    while (size > min && !fits()) { size -= 4; el.title.style.fontSize = size + "px"; }
  }
  const canvas = document.createElement("canvas").getContext("2d");
  function measure(text, size) {
    canvas.font = `700 ${size}px Inter, sans-serif`;
    return canvas.measureText(text).width * 0.975;             // letter-spacing -0.025em
  }

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
    el.list.innerHTML = shown.map(({ r, i }) => {
      const cls = i === cur ? "cur" : (r.scrobbled ? "done" : "");
      const pos = r.position || String(i + 1).padStart(2, "0");
      return `<div class="row ${cls}"><span class="pos">${esc(pos)}</span><span class="name">${esc(r.title)}</span><span class="dur">${r.duration ? mmss(r.duration) : ""}</span></div>`;
    }).join("");
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
    const parts = [];
    if (side) parts.push(`Side ${side}`);
    if (left != null) parts.push(left >= 90 ? `${Math.round(left / 60)} min left` : `${Math.round(left)} s left`);
    el.sidetext.textContent = parts.join(" · ") || "Now playing";
  }

  function renderEnded(st) {
    const e = st.ended;
    el.ended.classList.toggle("hidden", !e);
    if (!e) return;
    el.endedKicker.textContent = e.side ? `SIDE ${e.side} FINISHED` : "SIDE FINISHED";
    el.endedTitle.textContent = `${e.scrobbled} track${e.scrobbled === 1 ? "" : "s"} scrobbled`;
    el.endedSub.textContent = e.next_side
      ? `Flip for Side ${e.next_side}${e.next_title ? ` · starts with ${e.next_title}` : ""}`
      : `${e.artist} · ${e.album}`;
  }

  // ── progress + lyrics clock ──────────────────────────────────────────────
  // The bar moves by transform (composited) ten times a second; the clock text
  // changes once a second; lyrics are checked every frame (cheap: an index compare).
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
      if (lyricsOn && lyrics && p != null) highlightLyric(p + LYRIC_LEAD);
    }
    requestAnimationFrame(frame);
  }

  // ── lyrics ───────────────────────────────────────────────────────────────
  async function loadLyrics(st) {
    const key = `${st.artist}|${st.title}|${st.track_duration || ""}`;
    if (key === lyricsFor && lyrics) { renderLyrics(); return; }
    lyricsFor = key; lyrics = null; lyricIndex = -1;
    el.lyriclines.innerHTML = `<div class="lyric empty">Finding lyrics…</div>`;
    el.lyriclines.style.transform = "translateY(0)";
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
      el.lyriclines.style.transform = "translateY(0)";
      return;
    }
    el.lyriclines.innerHTML = lyrics.synced.map(([, line]) => `<div class="lyric">${esc(line) || "♪"}</div>`).join("");
    lyricIndex = -1;
  }

  function highlightLyric(p) {
    const lines = lyrics.synced;
    let idx = -1;
    for (let i = 0; i < lines.length; i++) { if (lines[i][0] <= p) idx = i; else break; }
    if (idx === lyricIndex) return;
    lyricIndex = idx;
    const nodes = el.lyriclines.children;
    for (let i = 0; i < nodes.length; i++) nodes[i].classList.toggle("cur", i === idx);
    const target = idx >= 0 ? nodes[idx] : null;
    const y = target ? target.offsetTop - 300 : 0;
    el.lyriclines.style.transform = `translateY(${-y}px)`;
  }

  function toggleLyrics(on) {
    lyricsOn = on == null ? !lyricsOn : on;
    el.btnLyrics.classList.toggle("on", lyricsOn);
    el.lyrics.classList.toggle("hidden", !lyricsOn);
    el.list.classList.toggle("hidden", lyricsOn);
    if (lyricsOn && state) loadLyrics(state);
  }

  // ── info sheet ───────────────────────────────────────────────────────────
  async function openSheet() {
    if (!state || state.phase !== "playing") return;
    sheetOpen = true;
    el.btnInfo.classList.add("on");
    el.body.classList.add("sheet-open");
    el.sheet.classList.remove("hidden");
    requestAnimationFrame(() => el.sheet.classList.add("open"));
    await loadInfo();
  }

  async function loadInfo() {
    const albumKey = `${state.artist}|${state.album}`;
    const trackKey2 = `${state.artist}|${state.title}`;
    const needAlbum = albumKey !== infoFor, needTrack = trackKey2 !== trackFor;
    if (!needAlbum && !needTrack) { renderInfo(); return; }
    if (needAlbum) { infoFor = albumKey; info = null; }
    if (needTrack) { trackFor = trackKey2; trackInfo = null; }
    renderInfo();
    const jobs = [];
    if (needAlbum) jobs.push(fetch(`/api/info?${new URLSearchParams({ artist: state.artist, album: state.album || "" })}`)
      .then((r) => r.json()).then((d) => { if (albumKey === infoFor) info = d; }).catch(() => {}));
    if (needTrack) jobs.push(fetch(`/api/track?${new URLSearchParams({ artist: state.artist, title: state.title })}`)
      .then((r) => r.json()).then((d) => { if (trackKey2 === trackFor) trackInfo = d; }).catch(() => {}));
    await Promise.all(jobs);
    renderInfo();
  }

  function closeSheet() {
    if (!sheetOpen) return;
    sheetOpen = false;
    el.btnInfo.classList.remove("on");
    el.body.classList.remove("sheet-open");
    el.sheet.classList.remove("open");
    setTimeout(() => { if (!sheetOpen) el.sheet.classList.add("hidden"); }, 520);
  }

  const tile = (h, p, sub, wide) => `<div class="tile${wide ? " wide" : ""}"><h3>${esc(h)}</h3><p>${p}</p>${sub ? `<p class="sub">${sub}</p>` : ""}</div>`;

  function renderInfo() {
    const pr = info && info.pressing;
    let pressing = info ? "" : "Loading…", pressingSub = "";
    if (pr) {
      const label = pr.labels && pr.labels.length ? pr.labels[0] : null;
      pressing = [label ? `<b>${esc(label.name)}</b>` : "", esc(pr.country), pr.year ? String(pr.year) : ""].filter(Boolean).join(" · ");
      const fmt = (pr.formats || []).join("; ");
      const catno = label && label.catno && !/^none$/i.test(label.catno) ? esc(label.catno) : "";
      let price = "";
      if (pr.typical_usd != null) price = `typically $${Math.round(pr.typical_usd)} on Discogs (VG+)`;
      else if (pr.price_usd != null) price = `from $${Math.round(pr.price_usd)} on Discogs${pr.for_sale ? ` · ${pr.for_sale} for sale` : ""}`;
      else if (pr.for_sale === null && pr.price_usd === null) price = "not sold on Discogs";
      pressingSub = [esc(fmt), catno, price].filter(Boolean).join(" · ");
    } else if (info) {
      pressing = "Not in your Discogs collection";
    }
    // credits: Discogs per-track first, Genius producers/writers next, samples last
    const lines = [];
    if (pr) {
      const cur = state && state.position ? pr.credits_by_track && pr.credits_by_track[state.position] : null;
      const pick = (list) => (list || []).filter((c) => /produc|written|featur|mix|master|record|compos|arrang|vocal/i.test(c.role)).slice(0, 4);
      const picked = pick(cur).length ? pick(cur) : pick(pr.credits);
      for (const c of picked) lines.push(`${esc(c.role)}: <b>${esc(c.name)}</b>`);
    }
    const g = trackInfo || {};
    if (!lines.length && g.producers && g.producers.length) lines.push(`Produced by <b>${esc(g.producers.join(", "))}</b>`);
    if (g.samples && g.samples.length) lines.push(`Samples <b>${esc(g.samples.join("; "))}</b>`);
    if (g.sampled_in && g.sampled_in.length) lines.push(`<span class="dim">Sampled in ${esc(g.sampled_in.slice(0, 2).join("; "))}</span>`);
    const credits = lines.length ? lines.join("<br>") : (trackInfo && info ? "No credits found" : "Loading…");
    const story = g.story ? esc(g.story).replace(/\n\n/g, "<br><br>")
      : (trackInfo ? "Nothing written up for this track" : "Loading…");
    const about = info && info.story && info.story.text ? esc(info.story.text) : (info ? "" : "Loading…");
    const pl = (info && info.plays) || {};
    let plays = pl.user_plays != null ? `<b>${pl.user_plays}</b> plays of this record on Last.fm` : (info ? "No plays counted yet" : "Loading…");
    const subs = [];
    if (pl.last_played) subs.push(`last played ${new Date(pl.last_played * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`);
    if (pl.listeners) subs.push(`${(pl.listeners / 1e6).toFixed(1)}M listeners worldwide`);
    el.tiles.innerHTML = tile("Pressing", pressing, pressingSub) + tile("Credits & samples", credits)
      + tile("Your plays", plays, subs.join(" · ")) + tile(state ? `The story · ${esc(state.title)}` : "The story", story, "", true)
      + (about ? tile("About the record", about, "", true) : "");
  }

  // ── gestures ─────────────────────────────────────────────────────────────
  let down = null;
  document.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, t: Date.now(), target: e.target }; }, { passive: true });
  document.addEventListener("pointerup", (e) => {
    if (!down) return;
    const dx = e.clientX - down.x, dy = e.clientY - down.y, dt = Date.now() - down.t;
    const start = down; down = null;
    if (screenOff) { wake(); return; }
    if (Math.abs(dx) > 120 && Math.abs(dy) < 90 && dt < 900) {
      if (dx > 0) sleep();            // swipe right: screen off
      return;                         // swipe left: reserved (Claw mode)
    }
    if (Math.abs(dy) > 120 && Math.abs(dx) < 90 && dt < 900) {
      const inSheet = el.sheet.contains(start.target);
      if (dy < 0 && !inSheet) openSheet();
      else if (dy > 0 && (!inSheet || document.getElementById("sheetscroll").scrollTop <= 0)) closeSheet();
      return;
    }
    if (Math.abs(dx) < 12 && Math.abs(dy) < 12) tap(start.target);
  }, { passive: true });

  function tap(target) {
    if (sheetOpen) { if (!el.sheet.contains(target)) closeSheet(); return; }
    if (el.btnLyrics.contains(target) || el.title.contains(target) || el.artist.contains(target)) { toggleLyrics(); return; }
    if (el.btnInfo.contains(target)) { openSheet(); return; }
    if (el.lyrics.contains(target)) { toggleLyrics(false); }
  }

  function sleep() { screenOff = true; el.off.classList.remove("hidden"); }
  function wake() { screenOff = false; el.off.classList.add("hidden"); }

  document.addEventListener("contextmenu", (e) => e.preventDefault());

  // ── stream ───────────────────────────────────────────────────────────────
  function connect() {
    const es = new EventSource("/events");
    es.onmessage = (e) => { try { apply(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    es.onerror = () => { es.close(); setTimeout(connect, 2000); };
  }
  connect();
  requestAnimationFrame(frame);
})();
