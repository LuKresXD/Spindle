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
      el.title.classList.toggle("long", (st.title || "").length > 16);
      el.title.classList.toggle("xlong", (st.title || "").length > 26);
      el.artist.textContent = st.artist || "";
      lyricIndex = -1;
      if (lyricsOn) loadLyrics(st);
    }
    const bits = [st.album, st.year].filter(Boolean);
    if (st.owned) bits.push("in your collection");
    el.albumline.textContent = bits.join(" · ");

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
      if (lyricsOn && lyrics && p != null) highlightLyric(p);
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
    const key = `${state.artist}|${state.album}`;
    if (key !== infoFor) {
      infoFor = key;
      el.tiles.innerHTML = tile("Pressing", "Loading…") + tile("Credits", "") + tile("The story", "") + tile("Your plays", "");
      try {
        const q = new URLSearchParams({ artist: state.artist, album: state.album || "" });
        const r = await fetch(`/api/info?${q}`);
        const info = await r.json();
        if (key !== infoFor) return;
        renderInfo(info);
      } catch (e) {
        el.tiles.innerHTML = tile("Info", "Nothing came back for this record.");
      }
    }
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

  function renderInfo(info) {
    const pr = info.pressing;
    let pressing = "", pressingSub = "";
    if (pr) {
      const label = pr.labels && pr.labels.length ? pr.labels[0] : null;
      pressing = [label ? `<b>${esc(label.name)}</b>` : "", esc(pr.country), pr.year ? String(pr.year) : ""].filter(Boolean).join(" · ");
      const fmt = (pr.formats || []).join("; ");
      const since = pr.added ? `in your collection since ${fmtDate(pr.added)}` : "";
      const catno = label && label.catno && !/^none$/i.test(label.catno) ? esc(label.catno) : "";
      pressingSub = [esc(fmt), catno, since].filter(Boolean).join(" · ");
    } else {
      pressing = "Not in your Discogs collection";
    }
    const creditLines = [];
    if (pr) {
      const cur = state && state.position ? pr.credits_by_track && pr.credits_by_track[state.position] : null;
      const pick = (list) => (list || []).filter((c) => /produc|written|featur|mix|master|record|compos|arrang|vocal/i.test(c.role)).slice(0, 4);
      const picked = pick(cur).length ? pick(cur) : pick(pr.credits);
      for (const c of picked) creditLines.push(`${esc(c.role)}: <b>${esc(c.name)}</b>`);
    }
    const credits = creditLines.length ? creditLines.join("<br>") : "No credits on this pressing";
    const story = info.story && info.story.text ? esc(info.story.text) : "Nothing written up for this record";
    const pl = info.plays || {};
    let plays = pl.user_plays != null ? `<b>${pl.user_plays}</b> plays of this record on Last.fm` : "No plays counted yet";
    const subs = [];
    if (pl.last_played) subs.push(`last played ${new Date(pl.last_played * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`);
    if (pl.listeners) subs.push(`${(pl.listeners / 1e6).toFixed(1)}M listeners worldwide`);
    el.tiles.innerHTML = tile("Pressing", pressing, pressingSub) + tile("Credits", credits)
      + tile("Your plays", plays, subs.join(" · ")) + tile("The story", story, "", true);
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
      else if (dy > 0 && (!inSheet || el.sheet.scrollTop <= 0)) closeSheet();
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
