#!/usr/bin/env python3
"""Generate OrbitGuard PWA icons (PNG and SVG) using Python stdlib only.

Writes valid RGBA PNG files via ``struct`` and ``zlib`` (no third-party image
libraries required) plus a matching SVG icon under
``src/orbitguard/static/icons/``:

- ``icon-192.png`` (192x192)
- ``icon-512.png`` (512x512)
- ``icon-512-maskable.png`` (512x512, W3C maskable safe-zone padded)
- ``apple-touch-icon.png`` (180x180)
- ``icon.svg`` (scalable vector globe + orbit glyph)
"""

from __future__ import annotations

import math
from pathlib import Path
import struct
import zlib

REPO_ROOT = Path(__file__).resolve().parents[1]
ICONS_DIR = REPO_ROOT / "src" / "orbitguard" / "static" / "icons"

# UI palette matching src/orbitguard/static/index.html
BG_DARK = (3, 7, 18)        # --bg: #030712
BG_SURFACE = (11, 19, 41)   # --surface: #0b1329
GLOBE_BASE = (15, 32, 66)   # #0f2042
GLOBE_LIT = (24, 58, 110)   # sunlit hemisphere highlight
CYAN = (0, 229, 255)        # --cyan: #00e5ff
EMERALD = (52, 211, 153)    # --emerald: #34d399
AMBER = (251, 191, 36)      # --amber: #fbbf24
WHITE = (240, 249, 255)


def _clamp_byte(val: float) -> int:
    return max(0, min(255, int(round(val))))


def _lerp_color(
    c1: tuple[int, int, int],
    c2: tuple[int, int, int],
    t: float,
) -> tuple[int, int, int]:
    t_clamped = max(0.0, min(1.0, t))
    return (
        _clamp_byte(c1[0] + (c2[0] - c1[0]) * t_clamped),
        _clamp_byte(c1[1] + (c2[1] - c1[1]) * t_clamped),
        _clamp_byte(c1[2] + (c2[2] - c1[2]) * t_clamped),
    )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def write_rgba_png(path: Path, width: int, height: int, raw_scanlines: bytes) -> None:
    """Write an 8-bit RGBA PNG file from raw filter-prefixed scanlines."""
    signature = b"\x89PNG\r\n\x1a\n"
    # bit depth = 8, color type = 6 (RGBA), compression = 0, filter = 0, interlace = 0
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    compressed = zlib.compress(raw_scanlines, level=9)
    png_bytes = (
        signature
        + _png_chunk(b"IHDR", ihdr_data)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)


def render_globe_orbit_png(size: int, *, maskable: bool = False) -> bytes:
    """Render a square RGBA globe-and-orbit icon as filter-0 PNG scanlines."""
    scale = 0.76 if maskable else 0.92
    half = size / 2.0
    earth_r = half * 0.46 * scale
    orbit_rx = half * 0.78 * scale
    orbit_ry = half * 0.28 * scale
    orbit_stroke = max(1.8, size * 0.024 * scale)
    rim_stroke = max(1.2, size * 0.012 * scale)
    grid_stroke = max(1.0, size * 0.008 * scale)

    # Orbit tilt angle (-28 deg)
    tilt = math.radians(-28.0)
    cos_t = math.cos(tilt)
    sin_t = math.sin(tilt)

    # Primary satellite marker on the front orbital arc (angle = +35 deg along ellipse)
    sat_angle = math.radians(35.0)
    sat_ex = orbit_rx * math.cos(sat_angle)
    sat_ey = orbit_ry * math.sin(sat_angle)
    sat_x = sat_ex * cos_t - sat_ey * sin_t
    sat_y = sat_ex * sin_t + sat_ey * cos_t
    sat_r = max(3.0, size * 0.052 * scale)

    # Secondary tracked object marker on the upper-left front arc
    sat2_angle = math.radians(145.0)
    sat2_ex = orbit_rx * math.cos(sat2_angle)
    sat2_ey = orbit_ry * math.sin(sat2_angle)
    sat2_x = sat2_ex * cos_t - sat2_ey * sin_t
    sat2_y = sat2_ex * sin_t + sat2_ey * cos_t
    sat2_r = max(2.2, size * 0.036 * scale)

    rows = bytearray()
    inv_half = 1.0 / half

    for py in range(size):
        rows.append(0)  # PNG filter type 0 (None)
        dy = (py + 0.5) - half
        for px in range(size):
            dx = (px + 0.5) - half
            dist_center = math.hypot(dx, dy)

            # Radial space background
            bg_t = min(1.0, dist_center * inv_half)
            color = _lerp_color(BG_SURFACE, BG_DARK, bg_t * 0.85)

            # Subtle outer radar ring
            outer_ring_r = half * 0.86 * scale
            outer_ring_dist = abs(dist_center - outer_ring_r)
            if outer_ring_dist < grid_stroke * 1.2:
                alpha = max(0.0, 1.0 - outer_ring_dist / (grid_stroke * 1.2)) * 0.22
                color = _lerp_color(color, CYAN, alpha)

            # Rotate coordinates into the tilted orbit frame
            ox = dx * cos_t + dy * sin_t
            oy = -dx * sin_t + dy * cos_t

            # Approximate distance to the tilted ellipse (ox/orbit_rx)^2 + (oy/orbit_ry)^2 = 1
            norm_e = math.hypot(ox / orbit_rx, oy / orbit_ry)
            # Local gradient magnitude for consistent stroke width
            grad = math.hypot(ox / (orbit_rx * orbit_rx), oy / (orbit_ry * orbit_ry))
            ellipse_dist = abs(norm_e - 1.0) / max(grad, 1e-6)

            in_back_half = oy < 0.0
            in_front_half = oy >= 0.0

            # 1. Back half of the orbital ring (occluded by the globe)
            if in_back_half and ellipse_dist < orbit_stroke * 1.1:
                orb_alpha = max(0.0, 1.0 - ellipse_dist / (orbit_stroke * 1.1)) * 0.55
                color = _lerp_color(color, CYAN, orb_alpha)

            # 2. Earth globe body, graticule lines, and atmosphere rim
            if dist_center <= earth_r + rim_stroke:
                if dist_center <= earth_r:
                    # Normalized sphere coordinates
                    nx = dx / earth_r
                    ny = dy / earth_r
                    nz = math.sqrt(max(0.0, 1.0 - nx * nx - ny * ny))
                    # Sun illumination from upper-left (-0.45, -0.45, 0.77)
                    lambert = max(0.0, -0.45 * nx - 0.45 * ny + 0.77 * nz)
                    globe_col = _lerp_color(GLOBE_BASE, GLOBE_LIT, lambert)

                    # Latitude & longitude graticule lines on the sphere
                    lat = math.asin(max(-1.0, min(1.0, ny)))
                    lon = math.atan2(nx, nz)
                    lat_grid = min(
                        abs(lat),
                        abs(lat - math.radians(30.0)),
                        abs(lat + math.radians(30.0)),
                    ) * earth_r
                    lon_grid = min(
                        abs(lon),
                        abs(lon - math.radians(35.0)),
                        abs(lon + math.radians(35.0)),
                    ) * earth_r * math.cos(lat)
                    grid_d = min(lat_grid, lon_grid)
                    if grid_d < grid_stroke:
                        g_alpha = (1.0 - grid_d / grid_stroke) * 0.35
                        globe_col = _lerp_color(globe_col, CYAN, g_alpha)

                    # Smooth edge anti-aliasing
                    edge_t = max(0.0, min(1.0, (earth_r - dist_center) / 1.2))
                    color = _lerp_color(color, globe_col, edge_t)

                # Cyan atmosphere rim on globe boundary
                rim_d = abs(dist_center - earth_r)
                if rim_d < rim_stroke * 1.3:
                    r_alpha = (1.0 - rim_d / (rim_stroke * 1.3)) * 0.85
                    color = _lerp_color(color, CYAN, r_alpha)

            # 3. Front half of the orbital ring (passes in front of the globe)
            if in_front_half and ellipse_dist < orbit_stroke * 1.25:
                orb_alpha = max(0.0, 1.0 - ellipse_dist / (orbit_stroke * 1.25))
                color = _lerp_color(color, CYAN, min(1.0, orb_alpha * 1.1))
                if ellipse_dist < orbit_stroke * 0.35:
                    core_alpha = (1.0 - ellipse_dist / (orbit_stroke * 0.35)) * 0.55
                    color = _lerp_color(color, WHITE, core_alpha)

            # 4. Secondary object marker (amber)
            d_sat2 = math.hypot(dx - sat2_x, dy - sat2_y)
            if d_sat2 < sat2_r * 1.4:
                s2_alpha = max(0.0, 1.0 - d_sat2 / (sat2_r * 1.4))
                color = _lerp_color(color, AMBER, min(1.0, s2_alpha * 1.2))

            # 5. Primary tracked LEO object marker (emerald with bright core)
            d_sat = math.hypot(dx - sat_x, dy - sat_y)
            if d_sat < sat_r * 1.6:
                glow_alpha = max(0.0, 1.0 - d_sat / (sat_r * 1.6)) * 0.45
                color = _lerp_color(color, EMERALD, glow_alpha)
            if d_sat < sat_r:
                edge_a = max(0.0, min(1.0, (sat_r - d_sat) / 1.2))
                color = _lerp_color(color, EMERALD, edge_a)
            if d_sat < sat_r * 0.45:
                core_a = max(0.0, 1.0 - d_sat / (sat_r * 0.45)) * 0.85
                color = _lerp_color(color, WHITE, core_a)

            rows.extend((color[0], color[1], color[2], 255))

    return bytes(rows)


def build_svg_icon() -> str:
    """Return a clean SVG globe-and-orbit icon matching the generated PNGs."""
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" role="img" aria-label="OrbitGuard LEO monitoring icon">
  <defs>
    <radialGradient id="bg" cx="50%" cy="50%" r="55%">
      <stop offset="0%" stop-color="#0b1329"/>
      <stop offset="100%" stop-color="#030712"/>
    </radialGradient>
    <radialGradient id="globe" cx="35%" cy="35%" r="65%">
      <stop offset="0%" stop-color="#183a6e"/>
      <stop offset="100%" stop-color="#0f2042"/>
    </radialGradient>
  </defs>
  <rect width="512" height="512" rx="96" fill="url(#bg)"/>
  <circle cx="256" cy="256" r="202" fill="none" stroke="#00e5ff" stroke-opacity="0.2" stroke-width="3"/>
  <g transform="translate(256 256) rotate(-28)">
    <path d="M -184 0 A 184 66 0 0 1 184 0" fill="none" stroke="#00e5ff" stroke-opacity="0.5" stroke-width="10"/>
  </g>
  <circle cx="256" cy="256" r="108" fill="url(#globe)" stroke="#00e5ff" stroke-width="5"/>
  <ellipse cx="256" cy="256" rx="108" ry="42" fill="none" stroke="#00e5ff" stroke-opacity="0.3" stroke-width="2.5"/>
  <ellipse cx="256" cy="256" rx="48" ry="108" fill="none" stroke="#00e5ff" stroke-opacity="0.3" stroke-width="2.5"/>
  <g transform="translate(256 256) rotate(-28)">
    <path d="M 184 0 A 184 66 0 0 1 -184 0" fill="none" stroke="#00e5ff" stroke-width="11" stroke-linecap="round"/>
    <circle cx="-151" cy="38" r="9" fill="#fbbf24"/>
    <circle cx="151" cy="38" r="14" fill="#34d399" stroke="#f0f9ff" stroke-width="4"/>
  </g>
</svg>
"""


def generate_all(output_dir: Path = ICONS_DIR) -> dict[str, Path]:
    """Generate all required PNG and SVG icons into ``output_dir``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    specs = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-512-maskable.png", 512, True),
        ("apple-touch-icon.png", 180, False),
    ]
    for filename, dim, maskable in specs:
        target = output_dir / filename
        scanlines = render_globe_orbit_png(dim, maskable=maskable)
        write_rgba_png(target, dim, dim, scanlines)
        outputs[filename] = target

    svg_path = output_dir / "icon.svg"
    svg_path.write_text(build_svg_icon(), encoding="utf-8")
    outputs["icon.svg"] = svg_path
    return outputs


if __name__ == "__main__":
    generated = generate_all()
    for name, path in generated.items():
        print(f"Generated {name}: {path} ({path.stat().st_size} bytes)")
