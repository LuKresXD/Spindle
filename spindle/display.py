"""Spindle display driver: now-playing on a Linux framebuffer.

Built for the 13.3" 1080p OLED on the Pi's HDMI0 (DRM fbdev emulation,
XRGB8888) and still drives the original Waveshare 3.5" SPI LCD
(fb_ili9486, RGB565 with INVON inversion) if that is what /dev/fb0 turns
out to be. Geometry, depth and stride are read from sysfs at init, so the
layout scales to whatever panel is attached.

OLED rules:
  * idle = pure black (pixels off: no burn-in, no "listening" text)
  * every frame is offset by a few pixels on a slow time-based cycle, so
    the static parts of the UI never sit on the same pixels for hours;
    a playing track is re-rendered every REFRESH_S so the shift actually
    cycles and a cleared console (VT switch after an HDMI hotplug) heals
  * no saturation/contrast boosting (that was for the washed-out TFT and
    is applied only on the legacy LCD)
"""

import colorsys
import io
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .fingerprint import TrackInfo

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
GREY_META = (92, 92, 92)
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


def _font_path(role: str) -> Optional[str]:
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


def pack_frame(img: Any, fb: FbInfo) -> bytes:
    """PIL RGB image -> raw bytes in the framebuffer's pixel format."""
    rgb = img.convert("RGB")
    if rgb.size != (fb.width, fb.height):
        rgb = rgb.resize((fb.width, fb.height), _LANCZOS)

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

    raw = raw.reshape(fb.height, row)
    if fb.stride == row:
        return raw.tobytes()
    out = np.zeros((fb.height, fb.stride), dtype=np.uint8)
    out[:, :row] = raw
    return out.tobytes()


# ── Rendering ────────────────────────────────────────────────────────────────

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


class Renderer:
    """Composes frames for a given panel size. Pure: no framebuffer access."""

    def __init__(self, width: int, height: int, legacy_lcd: bool = False):
        self.layout = make_layout(width, height)
        self.legacy_lcd = legacy_lcd
        s = self.layout.scale
        self._paths = {role: _font_path(role) for role in _FONTS}
        self._cache: dict = {}
        self.size_title = max(14, round(78 * s))
        self.size_artist = max(10, round(44 * s))
        self.size_album = max(9, round(34 * s))
        self.size_meta = max(8, round(28 * s))

    def _font(self, role: str, size: int) -> Any:
        key = (role, size)
        if key not in self._cache:
            p = self._paths.get(role)
            self._cache[key] = (ImageFont.truetype(p, size) if p
                                else ImageFont.load_default(size))
        return self._cache[key]

    def idle(self) -> Any:
        return Image.new("RGB", (self.layout.w, self.layout.h), BLACK)

    def track(self, track: TrackInfo, cover_art: Optional[bytes] = None,
              track_number: int = 0, shift: bool = True) -> Any:
        L = self.layout
        img = Image.new("RGB", (L.w, L.h), BLACK)
        draw = ImageDraw.Draw(img)

        # Album art (left, full height)
        accent = GREY_ACCENT_FALLBACK
        art = self._decode_art(cover_art)
        if art is not None:
            art = art.resize((L.art, L.art), _LANCZOS)
            if self.legacy_lcd:
                art = _enhance(art)
            accent = accent_colour(art)
            img.paste(art, (L.pad, L.pad))
        else:
            self._placeholder(draw)

        # Text column (right), block centred on the art's vertical span
        f_artist = self._font("medium", self.size_artist)
        f_album = self._font("regular", self.size_album)
        f_meta = self._font("regular", self.size_meta)
        artist_text = _truncate(track.artist or "", draw, f_artist, L.text_w)
        album_text = _truncate(track.album or "", draw, f_album, L.text_w) if track.album else ""

        title_size = self.size_title
        f_title = self._font("bold", title_size)
        title_lines, fits = _wrap(track.title or "", draw, f_title, L.text_w, 3)
        for factor in (0.82, 0.68):                      # long titles: shrink before cutting
            if fits:
                break
            title_size = max(14, round(self.size_title * factor))
            f_title = self._font("bold", title_size)
            title_lines, fits = _wrap(track.title or "", draw, f_title, L.text_w, 3)

        lh_artist = round(self.size_artist * 1.35)
        lh_title = round(title_size * 1.16)
        lh_album = round(self.size_album * 1.35)
        bar_h = max(2, round(5 * L.scale))
        bar_w = max(16, round(72 * L.scale))
        bar_gap = max(6, round(26 * L.scale))

        block_h = lh_artist + len(title_lines) * lh_title + bar_gap + bar_h
        if album_text:
            block_h += bar_gap + lh_album
        y = L.pad + (L.art - block_h) // 2

        draw.text((L.text_x, y), artist_text, fill=GREY_ARTIST, font=f_artist)
        y += lh_artist
        for line in title_lines:
            draw.text((L.text_x, y), line, fill=WHITE, font=f_title)
            y += lh_title
        y += bar_gap
        draw.rectangle([L.text_x, y, L.text_x + bar_w, y + bar_h], fill=accent)
        y += bar_h
        if album_text:
            y += bar_gap
            draw.text((L.text_x, y), album_text, fill=GREY_ALBUM, font=f_album)

        if track_number > 0:                              # bottom of the column, art-aligned
            meta = f"TRACK {track_number:02d}"
            draw.text((L.text_x, L.pad + L.art), meta, fill=GREY_META, font=f_meta, anchor="ls")

        if not shift:
            return img
        # Burn-in: slide the whole composition a few px on a slow cycle.
        canvas = Image.new("RGB", (L.w, L.h), BLACK)
        canvas.paste(img, self._shift())
        return canvas

    def _shift(self) -> tuple:
        amp = self.layout.shift
        m = int(time.time() // 60)
        return (m * 7) % (2 * amp + 1) - amp, (m * 11) % (2 * amp + 1) - amp

    def _decode_art(self, cover_art: Optional[bytes]) -> Any:
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


# ── Display (framebuffer owner) ──────────────────────────────────────────────

class Display:
    """Owns /dev/fbN and pushes rendered frames to it."""

    REFRESH_S = 60.0   # re-render a still-playing track this often (pixel shift + console heal)

    def __init__(self, enabled: bool = False, fb_path: str = "/dev/fb0"):
        self.enabled = enabled
        self._fb_path = fb_path
        self._fb: Optional[FbInfo] = None
        self._renderer: Optional[Renderer] = None
        self._lock = threading.Lock()
        self._last_key: Optional[tuple] = None
        self._last_render = 0.0

    def init(self) -> None:
        if not self.enabled:
            logger.debug("Display disabled")
            return
        if not _HAS_DEPS:
            logger.warning("Display enabled but Pillow/numpy missing: disabling")
            self.enabled = False
            return
        self._attach()
        self._last_key = None
        self.show_idle()
        fb = self._fb
        logger.info("Display initialised: %dx%d @%dbpp (%s%s) on %s",
                    fb.width, fb.height, fb.bpp, fb.name,
                    ", legacy LCD" if fb.legacy_lcd else "", fb.path)

    def _attach(self) -> None:
        self._fb = probe_fb(self._fb_path)
        self._renderer = Renderer(self._fb.width, self._fb.height, self._fb.legacy_lcd)

    # Public API --------------------------------------------------------------

    def show_track(self, track: TrackInfo, cover_art: Optional[bytes] = None,
                   track_number: int = 0, side: str = "") -> None:
        if not self.enabled:
            return
        key = (track.artist, track.title, track_number, len(cover_art or b""))
        now = time.monotonic()
        if key == self._last_key and now - self._last_render < self.REFRESH_S:
            return
        self._last_key = key
        self._last_render = now
        try:
            self._write(self._renderer.track(track, cover_art, track_number))
        except Exception:
            logger.exception("show_track failed")

    def show_idle(self) -> None:
        """Nothing playing: pixels off."""
        if not self.enabled:
            return
        self._last_key = None
        try:
            self._write(self._renderer.idle())
        except Exception:
            logger.exception("show_idle failed")

    def clear(self) -> None:
        self.show_idle()

    # Framebuffer -------------------------------------------------------------

    def _write(self, img: Any) -> None:
        with self._lock:
            for attempt in (0, 1):
                fb = self._fb
                try:
                    data = pack_frame(img, fb)
                    with open(fb.path, "r+b", buffering=0) as f:
                        f.write(data)
                    return
                except (OSError, ValueError) as e:
                    if attempt == 0:
                        # Geometry can change under us (hotplug, overlay change): re-probe once.
                        logger.warning("Framebuffer write failed (%s); re-probing %s", e, fb.path)
                        self._attach()
                    else:
                        logger.error("Framebuffer write: %s", e)
