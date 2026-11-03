"""Spindle display: renders `ScreenState` to a Linux framebuffer.

Built for the 13.3" 1080p OLED on the Pi's HDMI0 (DRM fbdev emulation,
XRGB8888) and still drives the original Waveshare 3.5" SPI LCD
(fb_ili9486, RGB565 with INVON inversion) if that is what /dev/fb0 turns
out to be. Geometry, depth and stride are read from sysfs at init, so the
layout scales to whatever panel is attached.

Three pictures:
  IDLE       pure black (OLED pixels off)
  LISTENING  a dim word and a dim level bar while the record is being identified
  PLAYING    cover left, artist / title / album, the tracklist with the
             current track lit, and a footer with position, progress and time

Static parts are rendered once per state change (and again every
REFRESH_S so the burn-in pixel shift cycles); the progress band is
refreshed once a second by a small ticker thread that rewrites only its
rows of the framebuffer.

`Renderer` is pure (state in, PIL image out) so frames can be previewed
offscreen; `Display` owns the framebuffer.
"""

from __future__ import annotations

import colorsys
import io
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import Phase, ScreenState

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
    _HAS_DEPS = True
    _LANCZOS = Image.Resampling.LANCZOS
    _BILINEAR = Image.Resampling.BILINEAR
except ImportError:  # pragma: no cover - optional "display" extra missing
    _HAS_DEPS = False
    _LANCZOS = _BILINEAR = None

logger = logging.getLogger(__name__)

BLACK = (0, 0, 0)
WHITE = (245, 245, 245)
GREY_ARTIST = (176, 176, 176)
GREY_ALBUM = (122, 122, 122)
GREY_META = (104, 104, 104)
GREY_ROW = (158, 158, 158)
GREY_ROW_DONE = (112, 112, 112)
GREY_ROW_AUX = (96, 96, 96)
GREY_BAR_TRACK = (34, 34, 34)
GREY_LISTEN = (70, 70, 70)
GREY_LISTEN_BAR = (110, 110, 110)
GREY_ACCENT_FALLBACK = (200, 200, 200)

# Font candidates, best first. Noto Sans ships on Pi OS; DejaVu is the fallback.
_FONTS = {
    "bold": [
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "medium": [
        "/usr/share/fonts/truetype/noto/NotoSans-Medium.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}


def _font_path(role: str) -> str | None:
    for p in _FONTS[role]:
        if Path(p).exists():
            return p
    return None


# ── Framebuffer ──────────────────────────────────────────────────────────────

@dataclass
class FbInfo:
    """What /dev/fbN is, read from /sys/class/graphics/fbN."""
    path: str
    name: str
    width: int
    height: int
    bpp: int
    stride: int      # bytes per row (can exceed width * bpp / 8)

    @property
    def legacy_lcd(self) -> bool:
        """The Waveshare 3.5" (ILI9486) needs INVON compensation + TFT boosting."""
        return "ili9486" in self.name.lower()


def _read_sys(fb_dir: Path, attr: str) -> str:
    try:
        return (fb_dir / attr).read_text().strip()
    except OSError:
        return ""


def probe_fb(fb_path: str = "/dev/fb0") -> FbInfo:
    """Read the framebuffer geometry from sysfs (no ioctls, no root needed)."""
    fb_dir = Path("/sys/class/graphics") / Path(fb_path).name
    name = _read_sys(fb_dir, "name") or "?"

    width = height = 0
    modes = _read_sys(fb_dir, "modes")                    # "U:1920x1080p-0"
    if "x" in modes:
        try:
            dims = modes.splitlines()[0].split(":")[-1].split("-")[0].rstrip("pi")
            width, height = (int(v) for v in dims.split("x"))
        except ValueError:
            width = height = 0
    if not (width and height):
        try:                                             # "1920,1080"
            width, height = (int(v) for v in _read_sys(fb_dir, "virtual_size").split(","))
        except ValueError:
            width, height = 1920, 1080

    try:
        bpp = int(_read_sys(fb_dir, "bits_per_pixel"))
    except ValueError:
        bpp = 32
    row = width * bpp // 8
    try:
        stride = int(_read_sys(fb_dir, "stride"))
    except ValueError:
        stride = row
    return FbInfo(fb_path, name, width, height, bpp, max(stride, row))


def pack_rows(img: Any, fb: FbInfo) -> bytes:
    """PIL RGB image (any height, full fb width) -> raw bytes, one fb row per image row."""
    rgb = img.convert("RGB")
    if rgb.width != fb.width:
        rgb = rgb.resize((fb.width, rgb.height), _LANCZOS)
    h = rgb.height

    if fb.bpp == 32:                                     # XRGB8888 LE = B,G,R,X
        row = fb.width * 4
        raw = np.frombuffer(rgb.tobytes("raw", "BGRX"), dtype=np.uint8)
    elif fb.bpp == 16:                                   # RGB565 LE
        arr = np.asarray(rgb, dtype=np.uint16)
        px = (arr[:, :, 0] >> 3) << 11 | (arr[:, :, 1] >> 2) << 5 | (arr[:, :, 2] >> 3)
        if fb.legacy_lcd:
            px ^= 0xFFFF                                 # ILI9486 INVON pre-compensation
        row = fb.width * 2
        raw = np.frombuffer(px.astype("<u2").tobytes(), dtype=np.uint8)
    else:
        raise ValueError(f"unsupported framebuffer depth: {fb.bpp} bpp")

    raw = raw.reshape(h, row)
    if fb.stride == row:
        return raw.tobytes()
    out = np.zeros((h, fb.stride), dtype=np.uint8)
    out[:, :row] = raw
    return out.tobytes()


def pack_frame(img: Any, fb: FbInfo) -> bytes:
    """Whole-screen variant of pack_rows (resizes to the panel if needed)."""
    rgb = img.convert("RGB")
    if rgb.size != (fb.width, fb.height):
        rgb = rgb.resize((fb.width, fb.height), _LANCZOS)
    return pack_rows(rgb, fb)


# ── Layout ───────────────────────────────────────────────────────────────────

@dataclass
class Layout:
    w: int
    h: int
    scale: float      # 1.0 at 1080p
    pad: int          # outer margin, also the art's x/y
    art: int          # art square side
    text_x: int
    text_w: int
    shift: int        # max pixel-shift amplitude (burn-in)

    @property
    def text_right(self) -> int:
        return self.text_x + self.text_w

    @property
    def bottom(self) -> int:
        return self.pad + self.art


def make_layout(w: int, h: int) -> Layout:
    s = h / 1080
    pad = max(8, round(60 * s))
    gap = max(10, round(80 * s))
    art = min(h - 2 * pad, int(w * 0.55))
    text_x = pad + art + gap
    return Layout(w, h, s, pad, art, text_x, w - text_x - pad, max(2, round(8 * s)))


def _enhance(img: Any) -> Any:
    """Boost saturation + contrast for the washed-out SPI TFT only."""
    img = ImageEnhance.Color(img).enhance(1.3)
    img = ImageEnhance.Contrast(img).enhance(1.2)
    return ImageEnhance.Sharpness(img).enhance(1.15)


def _truncate(text: str, draw: Any, font: Any, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1:
        text = text[:-1]
        if draw.textlength(text + "…", font=font) <= max_w:
            return text + "…"
    return text


def _wrap(text: str, draw: Any, font: Any, max_w: int, max_lines: int) -> tuple:
    """Word-wrap into at most max_lines; returns (lines, fits_completely)."""
    words = text.split()
    lines: list = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font) <= max_w:
            current = test
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
        current = ""
    fits = not current and " ".join(lines) == " ".join(words)
    if lines:
        lines[-1] = _truncate(lines[-1], draw, font, max_w)
    return lines[:max_lines], fits


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def accent_colour(art: Any) -> tuple:
    """A readable accent pulled from the cover: dominant, not too dark, lifted."""
    try:
        small = art.resize((48, 48), _BILINEAR).convert("RGB")
        q = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        best, best_score = None, -1.0
        for count, idx in q.getcolors() or []:
            r, g, b = pal[idx * 3: idx * 3 + 3]
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if v < 0.18:                                  # near-black: never an accent
                continue
            score = count * (0.15 + s) * (0.4 + v)
            if score > best_score:
                best, best_score = (h, s, v), score
        if best is None:
            return GREY_ACCENT_FALLBACK
        h, s, v = best
        if s > 0.12:
            s = max(s, 0.5)
        v = max(v, 0.78)
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        return (round(r * 255), round(g * 255), round(b * 255))
    except Exception:
        return GREY_ACCENT_FALLBACK


# ── Renderer ─────────────────────────────────────────────────────────────────

@dataclass
class Frame:
    """A rendered static frame plus what the ticker needs to refresh the footer."""
    image: Any                      # full-size PIL image
    shift: tuple = (0, 0)
    accent: tuple = GREY_ACCENT_FALLBACK
    band: tuple | None = None       # (y0, y1) rows of the dynamic footer band, pre-shift


class Renderer:
    """Composes frames for a given panel size. Pure: no framebuffer access."""

    def __init__(self, width: int, height: int, legacy_lcd: bool = False):
        self.layout = make_layout(width, height)
        self.legacy_lcd = legacy_lcd
        s = self.layout.scale
        self._paths = {role: _font_path(role) for role in _FONTS}
        self._cache: dict = {}
        self.size_title = max(14, round(66 * s))
        self.size_artist = max(10, round(42 * s))
        self.size_album = max(9, round(34 * s))
        self.size_row = max(9, round(32 * s))
        self.size_meta = max(8, round(28 * s))
        self.size_time = max(8, round(30 * s))
        self.row_h = max(12, round(46 * s))
        self.footer_h = max(30, round(126 * s))

    # fonts ------------------------------------------------------------------

    def _font(self, role: str, size: int) -> Any:
        key = (role, size)
        if key not in self._cache:
            p = self._paths.get(role)
            self._cache[key] = (ImageFont.truetype(p, size) if p
                                else ImageFont.load_default(size))
        return self._cache[key]

    # frames -----------------------------------------------------------------

    def idle(self) -> Frame:
        return Frame(Image.new("RGB", (self.layout.w, self.layout.h), BLACK))

    def listening(self, shift: bool = True) -> Frame:
        """Dim word, centred; the level bar lives in the band below it."""
        L = self.layout
        img = Image.new("RGB", (L.w, L.h), BLACK)
        draw = ImageDraw.Draw(img)
        f = self._font("medium", self.size_meta)
        word = "L I S T E N I N G"
        tw = draw.textlength(word, font=f)
        cy = L.h // 2
        draw.text(((L.w - tw) / 2, cy - self.size_meta * 1.6), word, fill=GREY_LISTEN, font=f)
        band_h = max(8, round(24 * L.scale))
        y0 = cy
        return self._finish(img, shift, GREY_LISTEN_BAR, (y0, y0 + band_h))

    def listening_band(self, frame: Frame, level_db: float) -> tuple[int, Any]:
        """Re-draw the level bar rows of a listening frame."""
        L = self.layout
        y0, y1 = frame.band
        band = Image.new("RGB", (L.w, y1 - y0), BLACK)
        draw = ImageDraw.Draw(band)
        bar_w = round(360 * L.scale)
        bar_h = max(2, round(4 * L.scale))
        x0 = (L.w - bar_w) // 2
        y = (y1 - y0 - bar_h) // 2
        draw.rectangle([x0, y, x0 + bar_w, y + bar_h], fill=GREY_BAR_TRACK)
        frac = min(1.0, max(0.0, (level_db + 60) / 50))       # -60 dB .. -10 dB
        if frac > 0:
            draw.rectangle([x0, y, x0 + round(bar_w * frac), y + bar_h], fill=GREY_LISTEN_BAR)
        return self._band_rows(frame, band)

    def playing(self, st: ScreenState, shift: bool = True) -> Frame:
        L = self.layout
        img = Image.new("RGB", (L.w, L.h), BLACK)
        draw = ImageDraw.Draw(img)

        # Cover (left, full height) -------------------------------------------
        accent = GREY_ACCENT_FALLBACK
        art = self._decode_art(st.art)
        if art is not None:
            art = art.resize((L.art, L.art), _LANCZOS)
            if self.legacy_lcd:
                art = _enhance(art)
            accent = accent_colour(art)
            img.paste(art, (L.pad, L.pad))
        else:
            self._placeholder(draw)

        # Header: artist / title / album · year -------------------------------
        y = L.pad
        f_artist = self._font("medium", self.size_artist)
        f_album = self._font("regular", self.size_album)
        draw.text((L.text_x, y), _truncate(st.artist, draw, f_artist, L.text_w),
                  fill=GREY_ARTIST, font=f_artist)
        y += round(self.size_artist * 1.4)

        title_size = self.size_title
        f_title = self._font("bold", title_size)
        lines, fits = _wrap(st.title, draw, f_title, L.text_w, 2)
        for factor in (0.84, 0.72):
            if fits:
                break
            title_size = max(14, round(self.size_title * factor))
            f_title = self._font("bold", title_size)
            lines, fits = _wrap(st.title, draw, f_title, L.text_w, 2)
        lh_title = round(title_size * 1.14)
        for line in lines:
            draw.text((L.text_x, y), line, fill=WHITE, font=f_title)
            y += lh_title
        y += round(8 * L.scale)

        album_line = st.album
        if st.year:
            album_line = f"{album_line} · {st.year}" if album_line else st.year
        if album_line:
            draw.text((L.text_x, y), _truncate(album_line, draw, f_album, L.text_w),
                      fill=GREY_ALBUM, font=f_album)
            y += round(self.size_album * 1.4)

        # Tracklist ------------------------------------------------------------
        list_top = y + round(28 * L.scale)
        list_bottom = L.bottom - self.footer_h - round(20 * L.scale)
        if st.tracklist and list_bottom - list_top >= self.row_h * 2:
            self._tracklist(draw, st, list_top, list_bottom, accent)

        # Footer band (static parts: meta line); progress + times are dynamic --
        band_y0 = L.bottom - self.footer_h
        self._footer_static(draw, st, band_y0, accent)

        return self._finish(img, shift, accent, (band_y0, L.bottom + 1))

    def playing_band(self, frame: Frame, st: ScreenState,
                     now: float | None = None) -> tuple[int, Any]:
        """Re-draw the footer rows: static meta line + live progress bar + times."""
        L = self.layout
        y0, y1 = frame.band
        dx, dy = frame.shift
        # the frame is stored shifted; lift these rows back into pre-shift
        # coordinates (PIL pads out-of-range crops with black), draw, re-shift
        band = frame.image.crop((dx, y0 + dy, dx + L.w, y1 + dy))
        draw = ImageDraw.Draw(band)
        # wipe the text column rows below the meta line, then redraw bar + times
        meta_h = round(self.size_meta * 1.5)
        draw.rectangle([L.text_x - round(24 * L.scale), meta_h, L.w, y1 - y0], fill=BLACK)

        bar_h = max(2, round(6 * L.scale))
        bar_y = meta_h + round(18 * L.scale)
        draw.rectangle([L.text_x, bar_y, L.text_right, bar_y + bar_h], fill=GREY_BAR_TRACK)

        elapsed = st.elapsed_now() if now is None else _elapsed_at(st, now)
        f_time = self._font("regular", self.size_time)
        ty = bar_y + bar_h + round(8 * L.scale)
        if elapsed is not None:
            if st.track_duration:
                frac = min(1.0, elapsed / st.track_duration)
                draw.rectangle([L.text_x, bar_y, L.text_x + round(L.text_w * frac), bar_y + bar_h],
                               fill=frame.accent)
                remaining = f"-{_mmss(st.track_duration - elapsed)}"
                tw = draw.textlength(remaining, font=f_time)
                draw.text((L.text_right - tw, ty), remaining, fill=GREY_META, font=f_time)
            draw.text((L.text_x, ty), _mmss(elapsed), fill=GREY_ARTIST, font=f_time)
        return self._band_rows(frame, band)

    # parts --------------------------------------------------------------------

    def _tracklist(self, draw: Any, st: ScreenState, top: int, bottom: int, accent: tuple) -> None:
        L = self.layout
        f_row = self._font("regular", self.size_row)
        f_row_cur = self._font("medium", self.size_row)
        f_aux = self._font("regular", self.size_meta)
        rows = st.tracklist
        n_fit = max(2, (bottom - top) // self.row_h)
        cur = st.current_index

        # window the list so the current row sits in the upper third
        if len(rows) <= n_fit:
            start = 0
        else:
            start = max(0, min(cur - n_fit // 3, len(rows) - n_fit)) if cur >= 0 else 0
        end = min(len(rows), start + n_fit)
        more_above = start
        more_below = len(rows) - end

        pos_w = round(76 * L.scale)
        dur_w = round(84 * L.scale)
        title_x = L.text_x + pos_w
        title_w = L.text_w - pos_w - dur_w - round(12 * L.scale)
        tick_w = max(2, round(3 * L.scale))

        y = top
        for i in range(start, end):
            row = rows[i]
            is_cur = i == cur
            colour = WHITE if is_cur else (GREY_ROW_DONE if row.scrobbled else GREY_ROW)
            aux = GREY_ARTIST if is_cur else GREY_ROW_AUX
            font = f_row_cur if is_cur else f_row
            baseline = y + (self.row_h - self.size_row) // 2

            if is_cur:
                bar_x = L.text_x - round(22 * L.scale)
                draw.rectangle([bar_x, y + round(8 * L.scale), bar_x + max(3, round(5 * L.scale)),
                                y + self.row_h - round(8 * L.scale)], fill=accent)

            pos = row.position or f"{i + 1:02d}"
            draw.text((L.text_x, baseline + 2), pos, fill=aux, font=f_aux)
            draw.text((title_x, baseline), _truncate(row.title, draw, font, title_w),
                      fill=colour, font=font)
            if row.duration:
                d = _mmss(row.duration)
                tw = draw.textlength(d, font=f_aux)
                draw.text((L.text_right - tw, baseline + 2), d, fill=aux, font=f_aux)
            if row.scrobbled and not is_cur:
                # small tick left of the duration column
                cx = L.text_right - dur_w - round(4 * L.scale)
                cy = baseline + self.size_row // 2 + 1
                s = max(4, round(7 * L.scale))
                draw.line([(cx - s, cy), (cx - s // 3, cy + s // 1.5), (cx + s, cy - s)],
                          fill=GREY_ROW_AUX, width=tick_w)
            y += self.row_h

        f_hint = self._font("regular", self.size_meta)
        for hint, hy in ((f"↑ {more_above}", top - self.size_meta - 4), (f"↓ {more_below}", y + 2)):
            if hint[2:] != "0":
                tw = draw.textlength(hint, font=f_hint)
                draw.text((L.text_right - tw, hy), hint, fill=GREY_ROW_AUX, font=f_hint)

    def _footer_static(self, draw: Any, st: ScreenState, y0: int, accent: tuple) -> None:
        """Meta line at the top of the footer: position left, scrobble info right."""
        L = self.layout
        f_meta = self._font("medium", self.size_meta)
        left = ""
        if st.position:
            left = st.position
            if st.side:
                left += f"  ·  SIDE {st.side}"
        elif st.current_index >= 0:
            left = f"TRACK {st.current_index + 1:02d}"
            if st.tracklist:
                left += f" / {len(st.tracklist):02d}"
        if left:
            draw.text((L.text_x, y0), left, fill=GREY_META, font=f_meta)

        right_parts = []
        if st.album_playcount:
            right_parts.append(f"{_ordinal(st.album_playcount)} play")
        if st.scrobbled:
            right_parts.append("scrobbled")
        right = "  ·  ".join(right_parts)
        if right:
            tw = draw.textlength(right, font=f_meta)
            x = L.text_right - tw
            draw.text((x, y0), right, fill=GREY_META, font=f_meta)
            if st.scrobbled:
                # tick glyph drawn, not a font character (Noto Sans has no ✓)
                s = max(4, round(8 * L.scale))
                cx = x - round(18 * L.scale)
                cy = y0 + self.size_meta * 0.62
                draw.line([(cx - s, cy), (cx - s // 3, cy + s * 0.7), (cx + s, cy - s)],
                          fill=accent, width=max(2, round(3 * L.scale)))

    def _finish(self, img: Any, shift: bool, accent: tuple, band: tuple | None) -> Frame:
        if not shift:
            return Frame(img, (0, 0), accent, band)
        dx, dy = self._shift()
        canvas = Image.new("RGB", img.size, BLACK)
        canvas.paste(img, (dx, dy))
        return Frame(canvas, (dx, dy), accent, band)

    def _band_rows(self, frame: Frame, band: Any) -> tuple[int, Any]:
        """Apply the frame's pixel shift to a freshly drawn band and return (fb_y0, rows)."""
        dx, dy = frame.shift
        y0, y1 = frame.band
        out = Image.new("RGB", (self.layout.w, y1 - y0), BLACK)
        out.paste(band, (dx, 0))
        fb_y0 = y0 + dy
        if fb_y0 < 0:
            out = out.crop((0, -fb_y0, out.width, out.height))
            fb_y0 = 0
        if fb_y0 + out.height > self.layout.h:
            out = out.crop((0, 0, out.width, self.layout.h - fb_y0))
        return fb_y0, out

    def _shift(self) -> tuple:
        amp = self.layout.shift
        m = int(time.time() // 60)
        return (m * 7) % (2 * amp + 1) - amp, (m * 11) % (2 * amp + 1) - amp

    def _decode_art(self, cover_art: bytes | None) -> Any:
        if not cover_art:
            return None
        try:
            return Image.open(io.BytesIO(cover_art)).convert("RGB")
        except Exception:
            logger.warning("Cover art could not be decoded (%d bytes)", len(cover_art))
            return None

    def _placeholder(self, draw: Any) -> None:
        """No cover: a quiet record silhouette in the art slot."""
        L = self.layout
        cx = cy = L.pad + L.art // 2
        w = max(1, round(3 * L.scale))
        r = L.art * 0.46
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(38, 38, 38), width=w)
        r = L.art * 0.16
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(26, 26, 26))
        r = max(2, L.art * 0.02)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(70, 70, 70))


def _elapsed_at(st: ScreenState, now: float) -> float | None:
    if st.track_elapsed is None:
        return None
    e = st.track_elapsed + (now - st.updated_at)
    if st.track_duration:
        e = min(e, st.track_duration)
    return max(0.0, e)


# ── Display (framebuffer owner) ──────────────────────────────────────────────

class Display:
    """Owns /dev/fbN; renders ScreenState changes and ticks the progress band."""

    REFRESH_S = 60.0     # re-render the static frame this often (pixel shift cycles)
    TICK_S = 1.0         # progress band refresh

    def __init__(self, enabled: bool = False, fb_path: str = "/dev/fb0"):
        self.enabled = enabled
        self._fb_path = fb_path
        self._fb: FbInfo | None = None
        self._renderer: Renderer | None = None
        self._lock = threading.Lock()
        self._state = ScreenState()
        self._frame: Frame | None = None
        self._identity: tuple | None = None
        self._static_at = 0.0
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()

    # lifecycle ----------------------------------------------------------------

    def init(self) -> None:
        if not self.enabled:
            logger.debug("Display disabled")
            return
        if not _HAS_DEPS:
            logger.warning("Display enabled but Pillow/numpy missing: disabling")
            self.enabled = False
            return
        self._attach()
        self.clear()
        fb = self._fb
        logger.info("Display initialised: %dx%d @%dbpp (%s%s) on %s",
                    fb.width, fb.height, fb.bpp, fb.name,
                    ", legacy LCD" if fb.legacy_lcd else "", fb.path)
        self._stop.clear()
        self._ticker = threading.Thread(target=self._tick_loop, name="display-tick", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ticker:
            self._ticker.join(timeout=2)

    def _attach(self) -> None:
        self._fb = probe_fb(self._fb_path)
        self._renderer = Renderer(self._fb.width, self._fb.height, self._fb.legacy_lcd)

    # public -------------------------------------------------------------------

    def update(self, st: ScreenState) -> None:
        """Called by the engine every tick with the current state."""
        if not self.enabled:
            return
        with self._lock:
            self._state = st
            ident = st.identity()
            now = time.monotonic()
            if ident != self._identity or now - self._static_at >= self.REFRESH_S:
                self._identity = ident
                self._static_at = now
                self._render_static(st)
            elif st.phase == Phase.LISTENING and self._frame and self._frame.band:
                self._write_band(*self._renderer.listening_band(self._frame, st.level_db))

    def clear(self) -> None:
        """Pixels off."""
        if not self.enabled:
            return
        with self._lock:
            self._state = ScreenState()
            self._identity = None
            self._frame = self._renderer.idle()
            self._write_frame(self._frame.image)

    # rendering ----------------------------------------------------------------

    def _render_static(self, st: ScreenState) -> None:
        r = self._renderer
        try:
            if st.phase == Phase.PLAYING:
                frame = r.playing(st)
            elif st.phase == Phase.LISTENING:
                frame = r.listening()
            else:
                frame = r.idle()
            self._frame = frame
            self._write_frame(frame.image)
            if frame.band:
                if st.phase == Phase.PLAYING:
                    self._write_band(*r.playing_band(frame, st))
                elif st.phase == Phase.LISTENING:
                    self._write_band(*r.listening_band(frame, st.level_db))
        except Exception:
            logger.exception("render failed")

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.TICK_S):
            with self._lock:
                st, frame = self._state, self._frame
                if (st.phase == Phase.PLAYING and frame and frame.band
                        and st.track_elapsed is not None):
                    try:
                        self._write_band(*self._renderer.playing_band(frame, st))
                    except Exception:
                        logger.exception("band refresh failed")

    # framebuffer ----------------------------------------------------------------

    def _write_frame(self, img: Any) -> None:
        self._write_at(0, pack_frame(img, self._fb))

    def _write_band(self, y0: int, rows_img: Any) -> None:
        self._write_at(y0 * self._fb.stride, pack_rows(rows_img, self._fb))

    def _write_at(self, offset: int, data: bytes) -> None:
        for attempt in (0, 1):
            fb = self._fb
            try:
                with open(fb.path, "r+b", buffering=0) as f:
                    f.seek(offset)
                    f.write(data)
                return
            except (OSError, ValueError) as e:
                if attempt == 0:
                    # Geometry can change under us (hotplug, overlay change): re-probe once.
                    logger.warning("Framebuffer write failed (%s); re-probing %s", e, fb.path)
                    self._attach()
                    self._identity = None
                else:
                    logger.error("Framebuffer write: %s", e)
