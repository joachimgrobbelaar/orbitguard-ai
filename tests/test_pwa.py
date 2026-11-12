"""Tests for OrbitGuard PWA manifest, service worker, icons, standalone UI, and docs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import struct
import zlib

from fastapi.testclient import TestClient
import pytest

from orbitguard.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "orbitguard" / "static"
ICONS_DIR = STATIC_DIR / "icons"


def _parse_png_chunks(data: bytes) -> tuple[tuple[int, int, int, int], list[tuple[bytes, bytes]]]:
    """Validate PNG signature, chunk CRCs, and return ((width, height, bit_depth, color_type), chunks)."""
    png_sig = b"\x89PNG\r\n\x1a\n"
    assert data.startswith(png_sig), "Invalid PNG file signature"
    pos = len(png_sig)
    chunks: list[tuple[bytes, bytes]] = []
    ihdr_info: tuple[int, int, int, int] | None = None

    while pos < len(data):
        assert pos + 8 <= len(data), "Truncated PNG chunk header"
        length, chunk_type = struct.unpack(">I4s", data[pos : pos + 8])
        pos += 8
        assert pos + length + 4 <= len(data), f"Truncated PNG chunk {chunk_type!r}"
        chunk_data = data[pos : pos + length]
        pos += length
        (expected_crc,) = struct.unpack(">I", data[pos : pos + 4])
        pos += 4
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        assert actual_crc == expected_crc, f"CRC mismatch in PNG chunk {chunk_type!r}"
        chunks.append((chunk_type, chunk_data))
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            assert comp == 0 and filt == 0 and interlace == 0
            ihdr_info = (width, height, bit_depth, color_type)
        if chunk_type == b"IEND":
            break

    assert ihdr_info is not None, "Missing IHDR chunk"
    assert chunks[-1][0] == b"IEND", "Missing IEND chunk"
    return ihdr_info, chunks


def test_manifest_webmanifest_valid_json_fields_and_media_type():
    client = TestClient(app)
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/manifest+json")

    manifest = res.json()
    assert manifest["name"] == "OrbitGuard"
    assert manifest["short_name"] == "OrbitGuard"
    desc = manifest["description"].lower()
    assert "leo" in desc
    assert "geometric" in desc
    assert "no collision probability" in desc or "collision probability" in desc
    assert manifest["start_url"] == "/?source=pwa"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["display_override"] == ["fullscreen", "standalone"]
    assert manifest["orientation"] == "any"
    assert manifest["background_color"] == "#030712"
    assert manifest["theme_color"] == "#030712"

    icons = manifest["icons"]
    assert isinstance(icons, list) and len(icons) >= 4
    by_src = {icon["src"]: icon for icon in icons}
    assert "/icons/icon-192.png" in by_src
    assert by_src["/icons/icon-192.png"]["sizes"] == "192x192"
    assert by_src["/icons/icon-192.png"]["type"] == "image/png"

    assert "/icons/icon-512.png" in by_src
    assert by_src["/icons/icon-512.png"]["sizes"] == "512x512"
    assert by_src["/icons/icon-512.png"]["type"] == "image/png"

    assert "/icons/icon-512-maskable.png" in by_src
    assert by_src["/icons/icon-512-maskable.png"]["sizes"] == "512x512"
    assert by_src["/icons/icon-512-maskable.png"]["type"] == "image/png"
    assert "maskable" in by_src["/icons/icon-512-maskable.png"]["purpose"]

    assert "/icons/icon.svg" in by_src
    assert by_src["/icons/icon.svg"]["type"] == "image/svg+xml"


def test_service_worker_route_headers_and_media_type():
    client = TestClient(app)
    res = client.get("/sw.js")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/javascript")
    assert res.headers.get("service-worker-allowed") == "/"
    assert "no-cache" in (res.headers.get("cache-control") or "").lower()
    assert "CACHE_NAME" in res.text
    assert "skipWaiting" in res.text
    assert "clients.claim" in res.text


def test_icons_exist_and_are_valid_pngs_of_declared_sizes(tmp_path: Path):
    client = TestClient(app)
    expected_pngs = [
        ("/icons/icon-192.png", ICONS_DIR / "icon-192.png", 192, 192),
        ("/icons/icon-512.png", ICONS_DIR / "icon-512.png", 512, 512),
        ("/icons/icon-512-maskable.png", ICONS_DIR / "icon-512-maskable.png", 512, 512),
        ("/icons/apple-touch-icon.png", ICONS_DIR / "apple-touch-icon.png", 180, 180),
        ("/apple-touch-icon.png", ICONS_DIR / "apple-touch-icon.png", 180, 180),
    ]

    for route, disk_path, exp_w, exp_h in expected_pngs:
        assert disk_path.is_file(), f"Missing committed icon file {disk_path}"
        res = client.get(route)
        assert res.status_code == 200, f"GET {route} failed with {res.status_code}"
        assert res.headers["content-type"].startswith("image/png")
        raw = res.content
        assert raw == disk_path.read_bytes()

        (width, height, bit_depth, color_type), chunks = _parse_png_chunks(raw)
        assert (width, height) == (exp_w, exp_h)
        assert bit_depth == 8
        assert color_type == 6  # RGBA

        idat_payload = b"".join(chunk_data for chunk_type, chunk_data in chunks if chunk_type == b"IDAT")
        decompressed = zlib.decompress(idat_payload)
        assert len(decompressed) == exp_h * (1 + exp_w * 4)
        # Ensure the icon renders a real multi-color globe/orbit glyph, not a blank solid square
        unique_rgb_samples = {
            (decompressed[i], decompressed[i + 1], decompressed[i + 2])
            for i in range(1, len(decompressed), max(4, (exp_w * 4) // 8))
        }
        assert len(unique_rgb_samples) > 25

    svg_res = client.get("/icons/icon.svg")
    assert svg_res.status_code == 200
    assert svg_res.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in svg_res.text and "</svg>" in svg_res.text

    # Verify the generator script under scripts/ uses stdlib zlib/struct and works cleanly
    script_path = REPO_ROOT / "scripts" / "generate_pwa_icons.py"
    assert script_path.is_file()
    spec = importlib.util.spec_from_file_location("generate_pwa_icons", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    generated = mod.generate_all(tmp_path)
    assert set(generated.keys()) == {
        "icon-192.png",
        "icon-512.png",
        "icon-512-maskable.png",
        "apple-touch-icon.png",
        "icon.svg",
    }


def test_icons_route_rejects_path_traversal():
    from fastapi import HTTPException
    from orbitguard.main import icon_file

    client = TestClient(app)
    traversal_paths = [
        "/icons/..%2Fmain.py",
        "/icons/..%2Findex.html",
        "/icons/..%2F..%2F..%2FREADME.md",
        "/icons/%2e%2e%2fmain.py",
        "/icons/%2e%2e/%2e%2e/pyproject.toml",
        "/icons/..%5Cmain.py",
        "/icons/..\\index.html",
        "/icons/%2e%2e",
        "/icons/.hidden.png",
        "/icons/subdir/icon-192.png",
        "/icons/../main.py",
        "/icons/nonexistent.png",
    ]
    for bad_path in traversal_paths:
        res = client.get(bad_path)
        assert res.status_code in (400, 404), f"Path {bad_path!r} was not rejected: {res.status_code}"
        assert "FastAPI" not in res.text
        assert "<!DOCTYPE html>" not in res.text

    for direct_bad_name in ("..", "../main.py", "..\\main.py", "/etc/passwd", ".env", "sub/icon-192.png"):
        with pytest.raises(HTTPException) as exc_info:
            icon_file(direct_bad_name)
        assert exc_info.value.status_code in (400, 404)


def test_service_worker_does_not_cache_api_responses_and_returns_503_offline():
    sw_source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")

    # 1. Verify required precache and lifecycle constructs in sw.js
    for expected in (
        '"/"',
        '"/manifest.webmanifest"',
        '"/apple-touch-icon.png"',
        '"/icons/icon-192.png"',
        '"/icons/icon-512.png"',
        '"/icons/icon-512-maskable.png"',
        '"/icons/icon.svg"',
        "https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js",
        "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js",
        "No network; OrbitGuard does not show cached telemetry as live.",
        "status: 503",
        "caches.delete",
        "skipWaiting()",
        "clients.claim()",
    ):
        assert expected in sw_source, f"Expected {expected!r} in sw.js"

    # 2. If Node.js is available, execute sw.js in a mock ServiceWorkerGlobalScope to verify runtime behavior
    node_bin = shutil.which("node")
    if node_bin is not None:
        harness = r"""
const fs = require("fs");
const vm = require("vm");

const swCode = fs.readFileSync(process.argv[1], "utf8");
const listeners = {};
const storedInCache = [];
const matchedFromCache = [];
let networkOnline = true;

class MockResponse {
  constructor(body, init = {}) {
    this._body = String(body || "");
    this.status = init.status || 200;
    this.ok = this.status >= 200 && this.status < 300;
    this.headers = init.headers || {};
  }
  clone() {
    return new MockResponse(this._body, { status: this.status, headers: this.headers });
  }
  async json() {
    return JSON.parse(this._body);
  }
}

const cacheStore = new Map();
const mockCache = {
  async addAll(urls) {
    for (const u of urls) {
      cacheStore.set(u, new MockResponse("shell:" + u, { status: 200 }));
    }
  },
  async put(reqOrUrl, resp) {
    const key = typeof reqOrUrl === "string" ? reqOrUrl : reqOrUrl.url;
    storedInCache.push(key);
    cacheStore.set(key, resp);
  }
};

const mockCaches = {
  async open(_name) { return mockCache; },
  async keys() { return ["orbitguard-shell-v0", "orbitguard-shell-v1"]; },
  async delete(k) { return k === "orbitguard-shell-v0"; },
  async match(reqOrUrl) {
    const key = typeof reqOrUrl === "string" ? reqOrUrl : reqOrUrl.url;
    matchedFromCache.push(key);
    return cacheStore.get(key) || cacheStore.get(new URL(key, "https://orbitguard.test").pathname) || null;
  }
};

const sandbox = {
  URL,
  Promise,
  JSON,
  Response: MockResponse,
  caches: mockCaches,
  fetch: async (reqOrUrl) => {
    const u = typeof reqOrUrl === "string" ? reqOrUrl : reqOrUrl.url;
    if (!networkOnline) {
      throw new Error("Simulated offline network error");
    }
    return new MockResponse(JSON.stringify({ live_ok: true, url: u }), { status: 200 });
  },
  self: {
    location: { origin: "https://orbitguard.test" },
    skipWaiting: () => {},
    clients: { claim: async () => {} },
    addEventListener: (type, fn) => { listeners[type] = fn; }
  }
};
sandbox.self.self = sandbox.self;

vm.createContext(sandbox);
vm.runInContext(swCode, sandbox);

(async () => {
  // Run install & activate
  let installPromise;
  listeners.install({ waitUntil: (p) => { installPromise = p; } });
  await installPromise;

  let activatePromise;
  listeners.activate({ waitUntil: (p) => { activatePromise = p; } });
  await activatePromise;

  // 1. Online /api/ request must NOT be cached or looked up in caches
  storedInCache.length = 0;
  matchedFromCache.length = 0;
  let apiOnlinePromise;
  listeners.fetch({
    request: { url: "https://orbitguard.test/api/orbitguard/loop/status", method: "GET", mode: "cors" },
    respondWith: (p) => { apiOnlinePromise = p; }
  });
  const apiOnlineResp = await apiOnlinePromise;
  if (apiOnlineResp.status !== 200) throw new Error("Expected 200 online API response");
  if (storedInCache.some((u) => u.includes("/api/"))) {
    throw new Error("sw.js cached an /api/ response: " + JSON.stringify(storedInCache));
  }
  if (matchedFromCache.some((u) => u.includes("/api/"))) {
    throw new Error("sw.js checked cache for an /api/ request: " + JSON.stringify(matchedFromCache));
  }

  // 2. Offline /api/ request must return HTTP 503 JSON with the required error/detail and never read cache
  networkOnline = false;
  let apiOfflinePromise;
  listeners.fetch({
    request: { url: "https://orbitguard.test/api/orbitguard/loop/status", method: "GET", mode: "cors" },
    respondWith: (p) => { apiOfflinePromise = p; }
  });
  const apiOfflineResp = await apiOfflinePromise;
  if (apiOfflineResp.status !== 503) {
    throw new Error("Expected 503 for offline /api/ request, got " + apiOfflineResp.status);
  }
  const offlineJson = await apiOfflineResp.json();
  if (
    offlineJson.error !== "offline" ||
    offlineJson.detail !== "No network; OrbitGuard does not show cached telemetry as live."
  ) {
    throw new Error("Unexpected offline /api/ JSON: " + JSON.stringify(offlineJson));
  }
  if (matchedFromCache.some((u) => u.includes("/api/"))) {
    throw new Error("sw.js read cache on offline /api/ request");
  }

  // 3. Offline "/" navigation falls back to cached app shell
  let rootOfflinePromise;
  listeners.fetch({
    request: { url: "https://orbitguard.test/", method: "GET", mode: "navigate" },
    respondWith: (p) => { rootOfflinePromise = p; }
  });
  const rootOfflineResp = await rootOfflinePromise;
  if (rootOfflineResp.status !== 200) {
    throw new Error("Expected cached 200 for offline '/', got " + rootOfflineResp.status);
  }

  console.log("SW_HARNESS_OK");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        proc = subprocess.run(
            [node_bin, "-e", harness, str(STATIC_DIR / "sw.js")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"SW harness failed: {proc.stderr or proc.stdout}"
        assert "SW_HARNESS_OK" in proc.stdout


def test_index_html_pwa_links_standalone_mode_and_offline_banner():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<meta name="theme-color" content="#030712">' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
    assert '<meta name="mobile-web-app-capable" content="yes">' in html
    assert '<meta name="apple-mobile-web-app-status-bar-style"' in html
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">' in html
    assert 'navigator.serviceWorker.register("/sw.js"' in html

    # Standalone detection, full-screen globe, overlay panel toggle, status chip, offline banner, install prompts
    assert '(display-mode: standalone)' in html
    assert '(display-mode: fullscreen)' in html
    assert 'searchParams.get("source") === "pwa"' in html
    assert 'id="pwa-status-chip"' in html
    assert 'id="pwa-panels-toggle"' in html
    assert 'id="offline-banner"' in html
    assert "navigator.onLine" in html
    assert "beforeinstallprompt" in html
    assert 'id="install-pwa-btn"' in html
    assert 'id="ios-install-hint"' in html
    assert "Share -&gt; Add to Home Screen" in html or "Share -> Add to Home Screen" in html


def test_readme_and_docs_include_pwa_widget_note_and_tracking_loop():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    docs = (REPO_ROOT / "docs" / "orbitguard.md").read_text(encoding="utf-8")

    # HF Space YAML front matter at the top of README.md must remain intact
    expected_front_matter = (
        "---\n"
        "title: OrbitGuard\n"
        "emoji: 🛰️\n"
        "colorFrom: blue\n"
        "colorTo: indigo\n"
        "sdk: docker\n"
        "app_port: 7860\n"
        "pinned: false\n"
        "license: mit\n"
        "short_description: Human-reviewed LEO catalog monitoring\n"
        "---\n"
    )
    assert readme.startswith(expected_front_matter)

    for text in (readme, docs):
        assert "Install on your phone (PWA)" in text
        assert "WidgetKit" in text
        assert "App Widget" in text
        assert "Add to Home Screen" in text

    # README tracking loop section checks
    assert "Real-time tracking loop" in readme
    assert "1/8" in readme
    assert "tracking_loop.sqlite" in readme
    assert "ORBITGUARD_LOOP_ENABLED" in readme
    assert "snapshot_fallback" in readme
    assert "co_located" in readme
    assert "ObservationSource" in readme
    assert "lidar" in readme and "xvision" in readme
    assert "/api/orbitguard/loop/status" in readme
    assert "/api/orbitguard/loop/conjunctions" in readme
    assert "/api/orbitguard/loop/objects" in readme
    assert "/api/orbitguard/loop/run-once" in readme
