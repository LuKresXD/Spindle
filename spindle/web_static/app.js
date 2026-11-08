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
    feat: $("feat"), elapsed: $("elapsed"), remaining: $("remaining"), list: $("list"), lyrics: $("lyrics"),
    btnDone: $("btn-done"),
    lyriclines: $("lyriclines"), btnLyrics: $("btn-lyrics"), btnInfo: $("btn-info"),
    listening: $("listening"), sheet: $("sheet"), tiles: $("tiles"),
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
  let screenOff = false;

  // ── helpers ──────────────────────────────────────────────────────────────
  const mmss = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const serverNow = () => Date.now() / 1000 - clockOffset;
  const fmtCount = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n);
  const sideOf = (pos) => (pos && /^[A-Za-z]/.test(pos)) ? pos[0].toUpperCase() : "";
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
    if (playing) renderHome(st);
    renderEnded(st);
    if (!playing && !st.ended) { closeSheet(); }
  }

  function renderHome(st) {
    const key = `${st.artist}|${st.title}|${st.album}`;
    if (key !== trackKey) {
      trackKey = key;
      trackChangedAt = performance.now();
      const { main, feat } = splitFeat(st.title);
      el.title.textContent = main;
      fitTitle();
      el.artist.textContent = st.artist || "";
      el.feat.textContent = feat ? `feat. ${feat}` : "";
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
    while (size > min && el.title.scrollHeight > size * 1.02 * 3.2) { size -= 4; el.title.style.fontSize = size + "px"; }
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
      return `<div class="row ${cls}"><span class="pos">${esc(pos)}</span><span class="name">${esc(splitFeat(r.title).main)}</span><span class="dur">${r.duration ? mmss(r.duration) : ""}</span></div>`;
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
    const n = `${e.scrobbled} track${e.scrobbled === 1 ? "" : "s"} scrobbled`;
    el.endedKicker.textContent = e.side ? `Side ${e.side} · finished` : "Side finished";
    el.endedTitle.textContent = e.next_side ? `Flip for Side ${e.next_side}` : "That's the record";
    el.endedSub.textContent = e.next_side
      ? `${n}${e.next_title ? ` · Side ${e.next_side} opens with ${splitFeat(e.next_title).main}` : ""}`
      : `${n} · ${e.artist} · ${e.album}`;
  }

  // ── progress + lyrics clock ──────────────────────────────────────────────
  // The bar moves by transform (composited) ten times a second; the clock text
  // changes once a second; lyrics are checked every frame (cheap: an index compare).
  function lyricLead() {
    if (!trackChangedAt) return LYRIC_BASE_LEAD;
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

  const sec = (h, body) => `<div class="sec"><h2>${esc(h)}</h2>${body}</div>`;

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

  function renderInfo() {
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
    const trackName = state ? esc(state.title) : "";
    el.tiles.innerHTML = mast + `<div class="cols"><div class="col">` +
      sec(`On this track · ${state ? splitFeat(state.title).main : ""}`, `<div class="story">${story}</div>`) +
      sec("Credits", `<div class="cred">${credits}</div>`) +
      `</div><div class="col">` +
      (samples ? sec("Samples", `<div class="samples">${samples}</div>`) : "") +
      sec("Your history", hist) +
      (about ? sec("About the album", `<div class="story sm">${about}</div>`) : "") +
      `</div></div>`;
  }

  // ── gestures ─────────────────────────────────────────────────────────────
  let down = null;
  document.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, t: Date.now(), target: e.target }; }, { passive: true });
  document.addEventListener("pointerup", (e) => {
    if (!down) return;
    const dx = e.clientX - down.x, dy = e.clientY - down.y, dt = Date.now() - down.t;
    const start = down; down = null;
    if (screenOff) { wake(); return; }
    if (Math.abs(dx) > 120 && Math.abs(dy) < 100 && dt < 1200) {
      if (dx > 0 && !sheetOpen) sleep();   // swipe right: screen off
      return;                              // swipe left: reserved (Claw mode)
    }
    if (Math.abs(dy) > 100 && Math.abs(dx) < 160 && dt < 1500) {
      if (dy < 0 && !sheetOpen) openSheet();
      else if (dy > 0 && sheetOpen && sheetAtTop()) closeSheet();
      return;
    }
    if (Math.abs(dx) < 14 && Math.abs(dy) < 14) tap(start);
  }, { passive: true });
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
  const closeZone = (t) => el.btnDone.contains(t) || !!t.closest(".mast, .grab");

  function tap(start) {
    const target = start.target;
    if (sheetOpen) { if (!el.sheet.contains(target) || closeZone(target) || start.y < 140) closeSheet(); return; }
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
