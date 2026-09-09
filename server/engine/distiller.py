"""
Distillation Engine — Generates platform-specific app packages with icons.
Each platform gets a real, installable, few-KB launcher with proper app icon.
"""

import json
import re
import shlex
import uuid
import secrets
import zipfile
import tarfile
import struct
import base64
import io
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, urljoin
from PIL import Image, ImageOps, UnidentifiedImageError
from server import config
from server.engine.apk_builder import ApkBuilder
from server.engine import mobileconfig_signer
from server.engine.cache import html_cache, icon_cache
from server.engine.storage import r2_storage
from server import html_site
from server.htmlmeta import parse_html_metadata
from server.logging_util import log_event
from server.net import fetch_public_url_bytes


# Compiled WKWebView helper shipped inside every macos.zip (see
# macos_template/ for source + build.sh). The Linux build server cannot
# cross-compile macOS binaries, so the ad-hoc-signed universal binary is
# committed next to its source and packed verbatim.
_MACOS_TEMPLATE_DIR = Path(__file__).parent / "macos_template"
_MACOS_HELPER_NAME = "wta_webview"

# Standalone-window runtime for the macOS .app launcher: a tiny JXA script that
# opens the target URL in a real WKWebView window using only system frameworks —
# no bundled binaries, nothing to codesign. Kept as a static string so the
# shipped file is byte-identical to what is tested locally. The launcher feeds
# it WTA_URL / WTA_NAME / WTA_ICON through the environment.
_MACOS_WEBVIEW_APP_JS = """\
ObjC.import('Cocoa');
ObjC.import('WebKit');

(function () {
  var env = $.NSProcessInfo.processInfo.environment;
  function envStr(key) { var v = env.objectForKey(key); return v ? v.js : ''; }
  var url = envStr('WTA_URL');
  var name = envStr('WTA_NAME') || 'App';
  var iconPath = envStr('WTA_ICON');
  if (!/^https:\\/\\//i.test(url)) {
    // App Transport Security blocks plain-http loads inside WKWebView; exit
    // non-zero so the shell launcher falls back to browser app mode.
    throw new Error('WTA: embedded WebView requires an https target');
  }
  var app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(0); // regular app: own dock icon + menu bar presence
  app.activateIgnoringOtherApps(true);
  if (iconPath) {
    var img = $.NSImage.alloc.initWithContentsOfFile(iconPath);
    if (img && !img.isNil()) { app.setApplicationIconImage(img); }
  }
  var W = 1280;
  var H = 800;
  var frame = $.NSMakeRect(0, 0, W, H);
  var win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(frame, 15, 2, false);
  win.setTitle(name);
  var screen = $.NSScreen.mainScreen.frame;
  var x = screen.origin.x + (screen.size.width - W) / 2;
  var y = screen.origin.y + (screen.size.height - H) / 2;
  win.setFrameDisplay($.NSMakeRect(x, y, W, H), true);
  var wv = $.WKWebView.alloc.initWithFrame(frame);
  win.contentView = wv;
  // Quit the whole app when the window closes. NSWindow does not retain its
  // delegate, so the instance must stay alive in this scope while run() spins.
  ObjC.registerSubclass({
    name: 'WTALauncherDelegate',
    methods: {
      'windowWillClose:': {
        types: ['void', ['id']],
        implementation: function () { $.NSApp.terminate(null); }
      }
    }
  });
  var delegate = $.WTALauncherDelegate.alloc.init;
  win.setDelegate(delegate);
  wv.loadRequest($.NSURLRequest.requestWithURL($.NSURL.URLWithString(url)));
  win.makeKeyAndOrderFront(null);
  app.run();
})();
"""


class Distiller:
    DOWNLOAD_PAGE_MARKER = "<!-- WebToAppDownloadPage:v4-i18n -->"

    _ANDROID_PACKAGE_PART_RE = re.compile(r"[^a-z0-9_]")
    _ANDROID_VERSION_NAME_RE = re.compile(r"[^0-9A-Za-z._-]")
    _DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?:;charset=[^;,]+)?;base64,(?P<data>.+)$", re.I | re.S)
    _FEATURE_DOWNLOAD_DIR_VALUES = {"public_downloads", "app_folder"}
    _FEATURE_PERMISSION_VALUES = {"prompt", "deny"}

    def create_recipe(self, app_id, url, name, color, display, orientation, options,
                      source_type="url", content_hash=None):
        clean_name = name or url.split("//")[-1].split("/")[0].replace("www.", "")
        version_code = self._android_version_code(app_id, options)
        version_name = self._android_version_name(options)
        package_prefix = self._android_package_prefix(options)
        feature_options = self._feature_options(options)
        recipe = {
            "id": app_id, "url": url, "name": clean_name,
            "color": color, "display": display, "orientation": orientation,
            "source_type": source_type or "url",
            "android_version_code": version_code,
            "android_version_name": version_name,
            "android_package_prefix": package_prefix,
            "_custom_icon_data_url": self._custom_icon_data_url(options),
            "custom_icon_uploaded": bool(self._custom_icon_data_url(options)),
            "edit_token": self._edit_token(app_id),
            "options": feature_options,
        }
        if recipe["source_type"] == "html":
            recipe["content_hash"] = content_hash or ""
        return recipe

    def _edit_token(self, app_id):
        """Secret token gating URL hot-swaps for this app. Stable per app_id:
        we reuse the existing token on rebuild so the original creator keeps
        control, and only mint a fresh one for a brand-new app."""
        recipe_path = self._generated_root() / app_id / "recipe.json"
        try:
            existing = json.loads(recipe_path.read_text()).get("edit_token")
            if isinstance(existing, str) and existing:
                return existing
        except Exception:
            pass
        return secrets.token_urlsafe(24)

    def _android_version_code(self, app_id, options):
        raw = options.get("android-version-code")
        if raw in (None, ""):
            raw = options.get("android_version_code")
        if raw in (None, ""):
            return self._next_android_version_code(app_id)
        try:
            value = int(str(raw).strip())
        except Exception:
            return self._next_android_version_code(app_id)
        return max(1, value)

    def _generated_root(self):
        return Path(__file__).resolve().parents[2] / "generated"

    def _next_android_version_code(self, app_id):
        recipe_path = self._generated_root() / app_id / "recipe.json"
        if not recipe_path.exists():
            return 1
        try:
            recipe = json.loads(recipe_path.read_text())
            prior = int(recipe.get("android_version_code", 0))
            return max(1, prior + 1)
        except Exception:
            return 1

    def _android_version_name(self, options):
        raw = options.get("android-version-name") or options.get("android_version_name") or "1.0.0"
        cleaned = self._ANDROID_VERSION_NAME_RE.sub("", str(raw).strip())
        cleaned = cleaned.strip(".-_")
        return cleaned or "1.0.0"

    def _android_package_prefix(self, options):
        raw = (
            options.get("android-package-prefix")
            or options.get("android_package_prefix")
            or config.android_package_prefix()
        )
        parts = []
        for chunk in str(raw or "").lower().split("."):
            token = self._ANDROID_PACKAGE_PART_RE.sub("", chunk)
            if not token:
                continue
            if token[0].isdigit():
                token = f"p{token}"
            parts.append(token)
        if len(parts) < 2:
            return "com.webtoapp"
        return ".".join(parts)

    def _custom_icon_data_url(self, options):
        raw = options.get("custom-icon-data-url") or options.get("custom_icon_data_url") or ""
        return str(raw).strip()

    def _feature_options(self, options):
        raw = options or {}
        normalized = {
            "feature-immersive-fullscreen": bool(
                raw.get("feature-immersive-fullscreen") or raw.get("feature_immersive_fullscreen")
            ),
            "feature-desktop-mode": bool(
                raw.get("feature-desktop-mode") or raw.get("feature_desktop_mode")
            ),
        }
        # Desktop browser-app shells (Windows .bat, Linux .desktop) honor
        # Chromium's --window-size / --start-maximized flags. Each OS gets its
        # own namespaced keys so a kiosk-style Linux box and a Windows laptop
        # can be sized independently.
        for prefix in ("windows", "linux"):
            width = self._window_dimension(raw, prefix, "width")
            height = self._window_dimension(raw, prefix, "height")
            normalized[f"{prefix}-window-width"] = width
            normalized[f"{prefix}-window-height"] = height
            normalized[f"{prefix}-window-maximized"] = bool(
                raw.get(f"{prefix}-window-maximized") or raw.get(f"{prefix}_window_maximized")
            )
        return normalized

    _WINDOW_MIN_PX = 320
    _WINDOW_MAX_PX = 7680

    def _window_dimension(self, raw_options, prefix, key):
        """Parse a namespaced window dimension (px) or return None when unset.

        Accepts both dash and underscore spellings. Out-of-range values are
        clamped; garbage yields None (= today's behavior, no flag emitted),
        so old/empty option payloads produce byte-identical launchers.
        """
        raw = raw_options if isinstance(raw_options, dict) else {}
        for variant in (f"{prefix}-window-{key}", f"{prefix}_window_{key}"):
            value = raw.get(variant)
            if value in (None, ""):
                continue
            try:
                parsed = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            return max(self._WINDOW_MIN_PX, min(self._WINDOW_MAX_PX, parsed))
        return None

    def _desktop_window_flags(self, opts, prefix):
        """Chromium app-mode flags for a desktop shell, or "" when default.

        Maximized wins over an explicit size (the two contradict each other).
        Dimensions are re-parsed (not just read) so direct builder calls with
        raw user payloads behave exactly like the normalized recipe path.
        The size is only a suggestion: Chromium may restore the last session's
        window instead — the UI copy says so.
        """
        raw = opts if isinstance(opts, dict) else {}
        if raw.get(f"{prefix}-window-maximized") or raw.get(f"{prefix}_window_maximized"):
            return " --start-maximized"
        width = self._window_dimension(raw, prefix, "width")
        height = self._window_dimension(raw, prefix, "height")
        if width is not None and height is not None:
            return f" --window-size={width},{height}"
        return ""

    def _feature_flag(self, raw_options, *keys, default=False):
        raw = raw_options if isinstance(raw_options, dict) else {}
        for key in keys:
            if key in raw:
                return bool(raw.get(key))
        return default

    def write_app_files(
        self,
        app_dir: Path,
        recipe: dict,
        base_url: Optional[str] = None,
        progress_cb: Optional[Callable[[str, Optional[dict]], None]] = None,
    ) -> dict:
        def report(stage: str, extra: Optional[dict] = None):
            if progress_cb:
                try:
                    progress_cb(stage, extra)
                except Exception:
                    pass

        app_dir.mkdir(parents=True, exist_ok=True)
        dl = app_dir / "downloads"
        dl.mkdir(exist_ok=True)

        stored_recipe = dict(recipe)
        stored_recipe.pop("_custom_icon_data_url", None)

        report("fetching_icon")
        icon_png = self._fetch_icon(recipe)
        if icon_png:
            (app_dir / "icon.png").write_bytes(icon_png)

        report("writing_shell")
        self._write_download_page(app_dir, recipe)
        direct_url = recipe["url"]
        self._write_pwa_files(app_dir, recipe, direct_url)

        ios_meta = {"signed": False, "dynamic_url": False}
        android_meta = {"apk": False, "fallback": False}
        platform_errors = {}

        def build_windows():
            self._build_windows(dl, recipe, icon_png, direct_url)
            return "windows", None

        def build_macos():
            self._build_macos(dl, recipe, icon_png, direct_url)
            return "macos", None

        def build_linux():
            self._build_linux(dl, recipe, icon_png, direct_url)
            return "linux", None

        def build_ios():
            meta = self._build_ios(dl, recipe, icon_png, base_url)
            return "ios", meta

        def build_android():
            meta = self._build_android(dl, recipe, icon_png, direct_url)
            return "android", meta

        builders = {
            "windows": build_windows,
            "macos": build_macos,
            "linux": build_linux,
            "ios": build_ios,
            "android": build_android,
        }
        platform_status = {name: "pending" for name in builders}
        report("building_platforms", {
            "platforms": list(builders),
            "platform_status": dict(platform_status),
            "done": 0,
            "total": len(builders),
        })
        max_workers = max(1, min(len(builders), int(config.build_parallelism() or 1)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fn): name for name, fn in builders.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    platform_name, meta = future.result()
                    if platform_name == "ios" and isinstance(meta, dict):
                        ios_meta = meta
                    elif platform_name == "android" and isinstance(meta, dict):
                        android_meta = meta
                    platform_status[name] = "done"
                    report("platform_done", {
                        "platform": platform_name,
                        "meta": meta or {},
                        "platform_status": dict(platform_status),
                        "done": sum(1 for v in platform_status.values() if v == "done"),
                        "total": len(builders),
                    })
                except Exception as exc:
                    platform_errors[name] = str(exc)
                    platform_status[name] = "error"
                    log_event("platform_build_failed", platform=name, error=str(exc), app_id=recipe.get("id"))
                    report("platform_error", {
                        "platform": name,
                        "error": str(exc),
                        "platform_status": dict(platform_status),
                        "done": sum(1 for v in platform_status.values() if v in {"done", "error"}),
                        "total": len(builders),
                    })

        report("uploading_cdn")
        cdn_downloads = self._upload_downloads_to_cdn(recipe.get("id") or app_dir.name, dl)
        if cdn_downloads:
            stored_recipe["downloads_cdn"] = cdn_downloads
            recipe["downloads_cdn"] = cdn_downloads

        stored_recipe["android"] = android_meta
        if platform_errors:
            stored_recipe["platform_errors"] = platform_errors
        (app_dir / "recipe.json").write_text(json.dumps(stored_recipe, ensure_ascii=False, indent=2))
        report("done", {"android": android_meta, "ios": ios_meta})

        return {
            "ios": ios_meta,
            "android": android_meta,
            "runtime_url": direct_url,
            "downloads_cdn": cdn_downloads,
            "platform_errors": platform_errors,
        }

    def _upload_downloads_to_cdn(self, app_id: str, downloads_dir: Path) -> dict:
        """Push every file in ``downloads/`` to R2. Failures are non-fatal:
        we keep the local files so the endpoint can still serve via FileResponse."""
        if not r2_storage.configured:
            return {}
        try:
            return r2_storage.upload_app_downloads(app_id, downloads_dir)
        except Exception as exc:  # noqa: BLE001 - network/storage errors are non-fatal
            print(f"[Storage] R2 upload failed for {app_id}: {exc}")
            return {}

    # ===== Icon Fetching =====
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    def _fetch_icon(self, recipe):
        custom_icon = self._custom_icon_png(recipe.get("_custom_icon_data_url"))
        if custom_icon:
            return custom_icon
        if recipe.get("source_type") == "html":
            # Content is local: custom icon > favicon declared in the uploaded
            # HTML > solid-color placeholder. No network fallback.
            return self._local_site_icon(recipe) or self._make_placeholder_png(recipe.get("color", "#7c3aed"))
        url = recipe["url"]
        host = urlparse(url).netloc.lower()
        cache_key = f"icon:{host}"
        cached = icon_cache.get(cache_key)
        if cached is not None:
            # b"" marks a known icon-less host (see the miss path below).
            return cached or self._make_placeholder_png(recipe.get("color", "#7c3aed"))
        candidates = self._collect_icon_candidates(url)
        best = self._choose_best_icon(candidates)
        if best:
            icon_cache.set(cache_key, best)
            return best
        # No icon anywhere (offline site, blocked fallbacks...). Cache the
        # miss too so every later build of this host skips the same
        # candidate sweep — on a CN host that sweep was ~2.3 s of serial
        # 404s plus an unreachable Google s2 fallback.
        icon_cache.set(cache_key, b"")
        return self._make_placeholder_png(recipe.get("color", "#7c3aed"))

    def _local_site_icon(self, recipe):
        """Favicon extracted from the staged HTML bundle (PNG or ICO → PNG)."""
        site_dir = self._generated_root() / recipe["id"] / "site"
        if not site_dir.is_dir():
            return None
        meta = html_site.extract_site_meta(site_dir)
        raw = meta.get("icon_png")
        if not raw:
            return None
        return self._normalize_to_png(raw)

    def _custom_icon_png(self, data_url):
        if not data_url:
            return None
        match = self._DATA_URL_RE.match(str(data_url))
        if not match:
            return None
        try:
            raw = base64.b64decode(match.group("data"), validate=False)
        except Exception:
            return None
        return self._normalize_uploaded_icon(raw)

    def _normalize_uploaded_icon(self, raw_bytes):
        try:
            with Image.open(io.BytesIO(raw_bytes)) as image:
                image = ImageOps.exif_transpose(image).convert("RGBA")
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
                x = (512 - image.width) // 2
                y = (512 - image.height) // 2
                canvas.paste(image, (x, y), image)
                out = io.BytesIO()
                canvas.save(out, format="PNG")
                return out.getvalue()
        except (UnidentifiedImageError, OSError, ValueError):
            return None

    def _fetch_url_bytes(self, url, timeout=8, use_cache=False):
        cache_key = None
        if use_cache:
            cache_key = f"bytes:{url}"
            cached = html_cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            data = fetch_public_url_bytes(
                url,
                timeout=timeout,
                headers={"User-Agent": self.USER_AGENT},
                max_bytes=config.outbound_response_max_bytes(),
                max_redirects=config.outbound_redirect_limit(),
            )
            if use_cache and cache_key and data is not None:
                html_cache.set(cache_key, data)
            return data
        except Exception:
            pass
        return None

    def _collect_icon_candidates(self, page_url):
        scored = []
        parsed = urlparse(page_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        html_bytes = self._fetch_url_bytes(page_url, timeout=10, use_cache=True)
        html = html_bytes.decode("utf-8", errors="ignore") if html_bytes else ""

        if html:
            doc = parse_html_metadata(html)
            for attrs in doc.link_attrs_by_rel("apple-touch-icon", "apple-touch-icon-precomposed"):
                href = attrs.get("href", "").strip()
                if not href:
                    continue
                size = self._extract_sizes_value(attrs.get("sizes", ""))
                scored.append((1000 + size, urljoin(page_url, href)))

            for attrs in doc.link_attrs_by_rel("icon"):
                href = attrs.get("href", "").strip()
                if not href:
                    continue
                size = self._extract_sizes_value(attrs.get("sizes", ""))
                bonus = 50 if href.lower().endswith(".png") else 0
                scored.append((500 + size + bonus, urljoin(page_url, href)))

            for attrs in doc.link_attrs_by_rel("manifest"):
                manifest_href = attrs.get("href", "").strip()
                if not manifest_href:
                    continue
                manifest_url = urljoin(page_url, manifest_href)
                for size, icon_url in self._icons_from_manifest(manifest_url):
                    scored.append((900 + size, icon_url))

            tile_href = doc.meta_content("msapplication-tileimage")
            if tile_href:
                scored.append((400, urljoin(page_url, tile_href)))

        if not scored:
            for prio, path in [
                (300, "/apple-touch-icon.png"),
                (290, "/apple-touch-icon-precomposed.png"),
                (280, "/favicon-192x192.png"),
                (270, "/favicon-96x96.png"),
                (250, "/favicon.png"),
                (200, "/favicon.ico"),
            ]:
                scored.append((prio, base + path))
        else:
            for prio, path in [
                (250, "/favicon.png"),
                (200, "/favicon.ico"),
            ]:
                scored.append((prio, base + path))
        # Google s2 is the last-resort fallback and unreachable from CN
        # networks (its 4 s connect timeout would stall every icon fetch).
        # Try DuckDuckGo's icon service FIRST (anycast, works globally) and
        # keep Google behind it for non-CN hosts as a second fallback.
        scored.append((150, f"https://icons.duckduckgo.com/ip3/{parsed.netloc}.ico"))
        scored.append((100, f"https://www.google.com/s2/favicons?domain={parsed.netloc}&sz=256"))
        seen = set()
        ordered = []
        for _, u in sorted(scored, key=lambda x: -x[0]):
            if u not in seen:
                seen.add(u)
                ordered.append(u)
        return ordered

    def _extract_sizes_value(self, value):
        sizes = str(value or "").lower()
        if "any" in sizes:
            return 512
        nums = re.findall(r"(\d+)\s*x\s*\d+", sizes)
        return max((int(n) for n in nums), default=0)

    def _icons_from_manifest(self, manifest_url):
        """Parse a Web App Manifest and yield (size, absolute_url) for each icon."""
        # The same URL is fetched on every build of this site (the analysis
        # step may have grabbed it too), so go through the HTML cache.
        data = self._fetch_url_bytes(manifest_url, timeout=6, use_cache=True)
        if not data:
            return
        try:
            manifest = json.loads(data.decode("utf-8", errors="ignore"))
        except (ValueError, json.JSONDecodeError):
            return
        for icon in manifest.get("icons", []) or []:
            src = icon.get("src")
            if not src:
                continue
            sizes = icon.get("sizes", "")
            nums = re.findall(r"(\d+)x\d+", sizes)
            size = max((int(n) for n in nums), default=0)
            yield size, urljoin(manifest_url, src)

    def _choose_best_icon(self, candidate_urls):
        best_png = None
        best_dim = 0
        limit = int(config.icon_candidate_limit() or 6)
        timeout = float(config.icon_fetch_timeout() or 4)
        urls = list(candidate_urls[:limit])
        if not urls:
            return None

        def fetch_one(url):
            data = self._fetch_url_bytes(url, timeout=timeout)
            if not data:
                return None
            png = self._normalize_to_png(data)
            if not png:
                return None
            return png, self._png_dimension(png), url

        # Wait on the whole batch, not a "good-enough" early exit: candidate
        # priority already encodes which source is likely best, so racing
        # completion-by-size made a fast 96px ico beat a slow 512px
        # apple-touch-icon whenever the smaller one landed first. The full
        # batch runs in parallel anyway, so total latency = the slowest
        # candidate (bounded by the per-request timeout) — and accepting a
        # strict-excellent icon instead of merely good one is worth ~100ms.
        workers = max(1, min(6, len(urls)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(fetch_one, urls):
                if not result:
                    continue
                png, dim, _url = result
                if dim > best_dim:
                    best_dim = dim
                    best_png = png
        return best_png

    def _png_dimension(self, png_data):
        if len(png_data) < 24 or png_data[:4] != b"\x89PNG":
            return 0
        return int.from_bytes(png_data[16:20], "big")

    def _normalize_to_png(self, data):
        """Accept a PNG or ICO blob; return PNG bytes or None.
        Other formats (SVG, JPEG, BMP, ...) are skipped — keeping the converter dependency-free."""
        if data[:4] == b"\x89PNG":
            return data
        if data[:4] == b"\x00\x00\x01\x00":  # ICO magic
            return self._ico_to_png(data)
        return None

    def _ico_to_png(self, ico_data):
        """Pick the largest entry from an ICO. Only Vista+ ICOs (with embedded PNG) succeed;
        legacy BMP-encoded entries are skipped (BMP→PNG conversion is non-trivial)."""
        if len(ico_data) < 6:
            return None
        count = int.from_bytes(ico_data[4:6], "little")
        if count == 0:
            return None
        entries = []
        for i in range(count):
            off = 6 + i * 16
            if off + 16 > len(ico_data):
                break
            e = ico_data[off:off + 16]
            w = e[0] or 256
            h = e[1] or 256
            size = int.from_bytes(e[8:12], "little")
            offset = int.from_bytes(e[12:16], "little")
            entries.append((w * h, size, offset))
        if not entries:
            return None
        entries.sort(reverse=True)  # largest first
        for _, size, offset in entries:
            if offset + size > len(ico_data):
                continue
            blob = ico_data[offset:offset + size]
            if blob[:4] == b"\x89PNG":
                return blob
        return None

    def _make_placeholder_png(self, hex_color: str, size: int = 128) -> bytes:
        """Generate a valid solid-color RGBA PNG. Used when favicon download fails.
        aapt2 has a bug parsing some malformed/tiny PNGs, so we always emit a proper one."""
        h = hex_color.lstrip('#')
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        try:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except ValueError:
            r, g, b = 0x7c, 0x3a, 0xed
        # IHDR
        ihdr_data = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
        ihdr = b'IHDR' + ihdr_data
        ihdr_chunk = struct.pack('>I', len(ihdr_data)) + ihdr + struct.pack('>I', zlib.crc32(ihdr))
        # IDAT
        raw = b''.join(b'\x00' + bytes([r, g, b, 0xFF]) * size for _ in range(size))
        idat_data = zlib.compress(raw, 9)
        idat = b'IDAT' + idat_data
        idat_chunk = struct.pack('>I', len(idat_data)) + idat + struct.pack('>I', zlib.crc32(idat))
        # IEND
        iend_chunk = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', zlib.crc32(b'IEND'))
        return b'\x89PNG\r\n\x1a\n' + ihdr_chunk + idat_chunk + iend_chunk

    # ===== Icon Format Converters =====
    def _png_to_ico(self, png_data):
        """Convert PNG to ICO (embedded PNG, Windows Vista+)."""
        w = int.from_bytes(png_data[16:20], 'big')
        h = int.from_bytes(png_data[20:24], 'big')
        return (
            struct.pack('<HHH', 0, 1, 1) +  # ICONDIR: reserved, type=ICO, count=1
            struct.pack('<BBBBHHII',
                0 if w >= 256 else w, 0 if h >= 256 else h,
                0, 0, 1, 32, len(png_data), 22) +  # ICONDIRENTRY
            png_data
        )

    def _png_to_icns(self, png_data):
        """Convert PNG to minimal ICNS (macOS icon)."""
        icon_type = b'ic08'  # 256x256
        entry_size = 8 + len(png_data)
        total_size = 8 + entry_size
        return (
            b'icns' + struct.pack('>I', total_size) +
            icon_type + struct.pack('>I', entry_size) +
            png_data
        )

    # ===== Download Landing Page =====
    def _write_download_page(self, app_dir: Path, r: dict):
        (app_dir / "page.html").write_text(self.render_download_page(app_dir, r))

    def render_download_page(self, app_dir: Path, r: dict) -> str:
        base = f"/a/{r['id']}"
        parsed = urlparse(r["url"])
        is_html_app = r.get("source_type") == "html"
        source_host = "HTML App" if is_html_app else (parsed.netloc.replace("www.", "") or r["url"])
        open_site_key = "openApp" if is_html_app else "openSite"
        favicon = f"{base}/icon.png" if (app_dir / "icon.png").exists() \
            else f"https://www.google.com/s2/favicons?domain={parsed.netloc}&sz=128"
        cfg = app_dir / "downloads" / "ios.mobileconfig"
        ios_signed = cfg.exists() and cfg.read_bytes()[:1] == b"\x30"
        ios_badge = (
            '<span class="ios-badge ios-badge-ok" data-i18n="iosBadgeSigned">Signed</span>'
            if ios_signed else
            '<span class="ios-badge ios-badge-warn" data-i18n="iosBadgeUnsigned">Unsigned, but still installable</span>'
        )
        android_apk = (app_dir / "downloads" / "android.apk").exists()
        android_zip = (app_dir / "downloads" / "android.zip").exists()
        android_meta = r.get("android") if isinstance(r.get("android"), dict) else {}
        if android_meta.get("apk") is True or (android_apk and not android_zip):
            android_detail_key = "platAndroidApkDetail"
            android_badge = '<span class="android-badge android-badge-ok" data-i18n="androidBadgeApk">Signed APK</span>'
        elif android_zip or android_meta.get("fallback") is True:
            android_detail_key = "platAndroidZipDetail"
            android_badge = '<span class="android-badge android-badge-warn" data-i18n="androidBadgeZip">PWA package</span>'
        else:
            android_detail_key = "platAndroidDetail"
            android_badge = ""
        platform_rows = [
            ("iPhone / iPad", "apple", f"{base}/download/ios", "platIosDetail", "actionInstall", ""),
            ("Android", "android", f"{base}/download/android", android_detail_key, "actionDownload", android_badge),
            ("macOS", "apple", f"{base}/download/macos", "platMacDetail", "actionDownload", ""),
            ("Windows", "windows", f"{base}/download/windows", "platWinDetail", "actionDownload", ""),
            ("Linux", "linux", f"{base}/download/linux", "platLinuxDetail", "actionDownload", ""),
        ]
        platform_icons = {
            "windows": (
                '<svg viewBox="0 0 88 88" aria-hidden="true">'
                '<path d="M0 12.402 35.687 7.542l.016 34.423-35.67.203zm35.67 33.529.028 34.453L.028 75.48.026 45.7zm4.326-39.025L87.314 0v41.527l-47.318.376zm47.329 39.349-.011 41.34-47.318-6.678-.066-34.739z"/>'
                '</svg>'
            ),
            "apple": (
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                '<path d="M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701"/>'
                '</svg>'
            ),
            "linux": (
                '<svg viewBox="0 0 448 512" aria-hidden="true">'
                '<path d="M220.8 123.3c1 .5 1.8 1.7 3 1.7 1.1 0 2.8-.4 2.9-1.5.2-1.4-1.9-2.3-3.2-2.9-1.7-.7-3.9-1-5.5-.1-.4.2-.8.7-.6 1.1.3 1.3 2.3 1.1 3.4 1.7zm-21.9 1.7c1.2 0 2-1.2 3-1.7 1.1-.6 3.1-.4 3.5-1.6.2-.4-.2-.9-.6-1.1-1.6-.9-3.8-.6-5.5.1-1.3.6-3.4 1.5-3.2 2.9.1 1 1.8 1.5 2.8 1.4zM420 403.8c-3.6-4-5.3-11.6-7.2-19.7-1.8-8.1-3.9-16.8-10.5-22.4-1.3-1.1-2.6-2.1-4-2.9-1.3-.8-2.7-1.5-4.1-2 9.2-27.3 5.6-54.5-3.7-79.1-11.4-30.1-31.3-56.4-46.5-74.4-17.1-21.5-33.7-41.9-33.4-72C311.1 85.4 315.7.1 234.8 0 132.4-.2 158 103.4 156.9 135.2c-1.7 23.4-6.4 41.8-22.5 64.7-18.9 22.5-45.5 58.8-58.1 96.7-6 17.9-8.8 36.1-6.2 53.3-6.5 5.8-11.4 14.7-16.6 20.2-4.2 4.3-10.3 5.9-17 8.3s-14 6-18.5 14.5c-2.1 3.9-2.8 8.1-2.8 12.4 0 3.9.6 7.9 1.2 11.8 1.2 8.1 2.5 15.7.8 20.8-5.2 14.4-5.9 24.4-2.2 31.7 3.8 7.3 11.4 10.5 20.1 12.3 17.3 3.6 40.8 2.7 59.3 12.5 19.8 10.4 39.9 14.1 55.9 10.4 11.6-2.6 21.1-9.6 25.9-20.2 12.5-.1 26.3-5.4 48.3-6.6 14.9-1.2 33.6 5.3 55.1 4.1.6 2.3 1.4 4.6 2.5 6.7v.1c8.3 16.7 23.8 24.3 40.3 23 16.6-1.3 34.1-11 48.3-27.9 13.6-16.4 36-23.2 50.9-32.2 7.4-4.5 13.4-10.1 13.9-18.3.4-8.2-4.4-17.3-15.5-29.7zM223.7 87.3c9.8-22.2 34.2-21.8 44-.4 6.5 14.2 3.6 30.9-4.3 40.4-1.6-.8-5.9-2.6-12.6-4.9 1.1-1.2 3.1-2.7 3.9-4.6 4.8-11.8-.2-27-9.1-27.3-7.3-.5-13.9 10.8-11.8 23-4.1-2-9.4-3.5-13-4.4-1-6.9-.3-14.6 2.9-21.8zM183 75.8c10.1 0 20.8 14.2 19.1 33.5-3.5 1-7.1 2.5-10.2 4.6 1.2-8.9-3.3-20.1-9.6-19.6-8.4.7-9.8 21.2-1.8 28.1 1 .8 1.9-.2-5.9 5.5-15.6-14.6-10.5-52.1 8.4-52.1zm-13.6 60.7c6.2-4.6 13.6-10 14.1-10.5 4.7-4.4 13.5-14.2 27.9-14.2 7.1 0 15.6 2.3 25.9 8.9 6.3 4.1 11.3 4.4 22.6 9.3 8.4 3.5 13.7 9.7 10.5 18.2-2.6 7.1-11 14.4-22.7 18.1-11.1 3.6-19.8 16-38.2 14.9-3.9-.2-7-1-9.6-2.1-8-3.5-12.2-10.4-20-15-8.6-4.8-13.2-10.4-14.7-15.3-1.4-4.9 0-9 4.2-12.3zm3.3 334c-2.7 35.1-43.9 34.4-75.3 18-29.9-15.8-68.6-6.5-76.5-21.9-2.4-4.7-2.4-12.7 2.6-26.4v-.2c2.4-7.6.6-16-.6-23.9-1.2-7.8-1.8-15 .9-20 3.5-6.7 8.5-9.1 14.8-11.3 10.3-3.7 11.8-3.4 19.6-9.9 5.5-5.7 9.5-12.9 14.3-18 5.1-5.5 10-8.1 17.7-6.9 8.1 1.2 15.1 6.8 21.9 16l19.6 35.6c9.5 19.9 43.1 48.4 41 68.9zm-1.4-25.9c-4.1-6.6-9.6-13.6-14.4-19.6 7.1 0 14.2-2.2 16.7-8.9 2.3-6.2 0-14.9-7.4-24.9-13.5-18.2-38.3-32.5-38.3-32.5-13.5-8.4-21.1-18.7-24.6-29.9s-3-23.3-.3-35.2c5.2-22.9 18.6-45.2 27.2-59.2 2.3-1.7.8 3.2-8.7 20.8-8.5 16.1-24.4 53.3-2.6 82.4.6-20.7 5.5-41.8 13.8-61.5 12-27.4 37.3-74.9 39.3-112.7 1.1.8 4.6 3.2 6.2 4.1 4.6 2.7 8.1 6.7 12.6 10.3 12.4 10 28.5 9.2 42.4 1.2 6.2-3.5 11.2-7.5 15.9-9 9.9-3.1 17.8-8.6 22.3-15 7.7 30.4 25.7 74.3 37.2 95.7 6.1 11.4 18.3 35.5 23.6 64.6 3.3-.1 7 .4 10.9 1.4 13.8-35.7-11.7-74.2-23.3-84.9-4.7-4.6-4.9-6.6-2.6-6.5 12.6 11.2 29.2 33.7 35.2 59 2.8 11.6 3.3 23.7.4 35.7 16.4 6.8 35.9 17.9 30.7 34.8-2.2-.1-3.2 0-4.2 0 3.2-10.1-3.9-17.6-22.8-26.1-19.6-8.6-36-8.6-38.3 12.5-12.1 4.2-18.3 14.7-21.4 27.3-2.8 11.2-3.6 24.7-4.4 39.9-.5 7.7-3.6 18-6.8 29-32.1 22.9-76.7 32.9-114.3 7.2zm257.4-11.5c-.9 16.8-41.2 19.9-63.2 46.5-13.2 15.7-29.4 24.4-43.6 25.5s-26.5-4.8-33.7-19.3c-4.7-11.1-2.4-23.1 1.1-36.3 3.7-14.2 9.2-28.8 9.9-40.6.8-15.2 1.7-28.5 4.2-38.7 2.6-10.3 6.6-17.2 13.7-21.1.3-.2.7-.3 1-.5.8 13.2 7.3 26.6 18.8 29.5 12.6 3.3 30.7-7.5 38.4-16.3 9-.3 15.7-.9 22.6 5.1 9.9 8.5 7.1 30.3 17.1 41.6 10.6 11.6 14 19.5 13.7 24.6zM173.3 148.7c2 1.9 4.7 4.5 8 7.1 6.6 5.2 15.8 10.6 27.3 10.6 11.6 0 22.5-5.9 31.8-10.8 4.9-2.6 10.9-7 14.8-10.4s5.9-6.3 3.1-6.6-2.6 2.6-6 5.1c-4.4 3.2-9.7 7.4-13.9 9.8-7.4 4.2-19.5 10.2-29.9 10.2s-18.7-4.8-24.9-9.7c-3.1-2.5-5.7-5-7.7-6.9-1.5-1.4-1.9-4.6-4.3-4.9-1.4-.1-1.8 3.7 1.7 6.5z"/>'
                '</svg>'
            ),
            "android": (
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                '<path d="M18.4395 5.5586c-.675 1.1664-1.352 2.3318-2.0274 3.498-.0366-.0155-.0742-.0286-.1113-.043-1.8249-.6957-3.484-.8-4.42-.787-1.8551.0185-3.3544.4643-4.2597.8203-.084-.1494-1.7526-3.021-2.0215-3.4864a1.1451 1.1451 0 0 0-.1406-.1914c-.3312-.364-.9054-.4859-1.379-.203-.475.282-.7136.9361-.3886 1.5019 1.9466 3.3696-.0966-.2158 1.9473 3.3593.0172.031-.4946.2642-1.3926 1.0177C2.8987 12.176.452 14.772 0 18.9902h24c-.119-1.1108-.3686-2.099-.7461-3.0683-.7438-1.9118-1.8435-3.2928-2.7402-4.1836a12.1048 12.1048 0 0 0-2.1309-1.6875c.6594-1.122 1.312-2.2559 1.9649-3.3848.2077-.3615.1886-.7956-.0079-1.1191a1.1001 1.1001 0 0 0-.8515-.5332c-.5225-.0536-.9392.3128-1.0488.5449zm-.0391 8.461c.3944.5926.324 1.3306-.1563 1.6503-.4799.3197-1.188.0985-1.582-.4941-.3944-.5927-.324-1.3307.1563-1.6504.4727-.315 1.1812-.1086 1.582.4941zM7.207 13.5273c.4803.3197.5506 1.0577.1563 1.6504-.394.5926-1.1038.8138-1.584.4941-.48-.3197-.5503-1.0577-.1563-1.6504.4008-.6021 1.1087-.8106 1.584-.4941z"/>'
                '</svg>'
            ),
        }
        platform_links = "\n".join(
            (
                f'<a href="{href}" class="plat">'
                f'<span class="plat-icon plat-icon-{icon_key}">{platform_icons[icon_key]}</span>'
                f'<div class="plat-info"><div class="plat-name">{name}{extra_badge}</div>'
                f'<div class="plat-detail" data-i18n="{detail_key}"></div></div>'
                f'<span class="plat-badge plat-dl" data-i18n="{action_key}"></span>'
                f'</a>'
            )
            for name, icon_key, href, detail_key, action_key, extra_badge in platform_rows
        )
        # ---- i18n: in-page translations (visitor can switch; default English) ----
        dl_i18n = self._download_page_translations()
        dl_i18n_json = json.dumps(dl_i18n, ensure_ascii=False)
        safe_name = (r["name"] or "").replace("\\", "\\\\").replace('"', '\\"')
        html = f"""{self.DOWNLOAD_PAGE_MARKER}
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{r['name']} — Download | WebToApp</title>
<meta name="description" content="Download the installer or config profile of {r['name']} for your device. Works on iPhone, Android, Windows, macOS and Linux.">
<meta name="theme-color" content="{r['color']}">
<link rel="icon" href="/assets/site-logo.jpg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Serif+SC:wght@400;500;600;700;900&family=Spectral:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{{
  --paper:#f7f3ec;
  --ink:#1e1914;
  --ink-soft:rgba(30,25,20,.68);
  --line:rgba(30,25,20,.1);
  --line-strong:rgba(30,25,20,.16);
  --accent:#c97953;
  --accent-deep:#1e1914;
  --surface:#fbf8f3;
  --surface-strong:#fbf8f3;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html{{-webkit-font-smoothing:antialiased}}
body{{
  min-height:100vh;
  font-family:'Inter',system-ui,sans-serif;
  color:var(--ink);
  background:var(--paper);
}}
a{{color:inherit;text-decoration:none}}
.page{{max-width:1360px;margin:0 auto;padding:28px 28px 44px}}
.nav{{display:flex;align-items:center;justify-content:space-between;padding-bottom:18px;border-bottom:1px solid var(--line)}}
.brand{{display:inline-flex;align-items:center;gap:12px;font-size:1.15rem;font-weight:700}}
.brand img{{display:block;width:28px;height:28px;border-radius:8px;object-fit:cover}}
.brand-note{{font-size:.82rem;color:var(--ink-soft);letter-spacing:.14em;text-transform:uppercase}}
.nav-right{{display:inline-flex;align-items:center;gap:14px}}
#dl-lang{{appearance:none;-webkit-appearance:none;font:inherit;font-size:.9rem;color:var(--ink);background-color:rgba(255,251,246,.72);background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23736357' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;border:1px solid var(--line-strong);border-radius:8px;padding:7px 32px 7px 14px;cursor:pointer}}
[dir="rtl"] #dl-lang{{background-position:left 12px center;padding:7px 14px 7px 32px}}
.hero{{display:grid;grid-template-columns:minmax(0,1.02fr) minmax(420px,.98fr);gap:36px;align-items:start;padding-top:44px}}
.hero-copy{{padding:8px 0 0}}
.eyebrow{{margin-bottom:14px;font-size:.78rem;letter-spacing:.16em;color:rgba(30,25,20,.46);text-transform:uppercase}}
.title{{max-width:14ch;font-family:'Spectral','Noto Serif SC','Songti SC',serif;font-size:clamp(2.2rem,4.2vw,3.4rem);font-weight:600;line-height:1.1;letter-spacing:0}}
.meta-row{{display:flex;flex-wrap:wrap;gap:8px;margin-top:20px}}
.meta-chip{{display:inline-flex;align-items:center;padding:7px 12px;border:1px solid var(--line);border-radius:6px;background:transparent;font-size:.86rem;color:var(--ink-soft)}}
.desc{{max-width:30rem;margin-top:20px;font-size:1rem;line-height:1.78;color:var(--ink-soft)}}
.source{{margin-top:16px;font-size:.9rem;color:rgba(24,20,18,.48);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.hero-panel{{display:flex;flex-direction:column;justify-content:space-between;border:1px solid var(--line);border-radius:16px;background:var(--surface-strong);overflow:hidden}}
.app-top{{padding:26px 26px 20px;border-bottom:1px solid var(--line)}}
.app-head{{display:flex;align-items:center;gap:18px}}
.icon{{width:72px;height:72px;border-radius:14px;flex-shrink:0;object-fit:contain;background:rgba(255,255,255,.7);border:1px solid rgba(30,25,20,.08);padding:8px}}
.app-title{{font-family:'Spectral','Noto Serif SC','Songti SC',serif;font-size:1.6rem;line-height:1.15;letter-spacing:0}}
.app-sub{{margin-top:8px;font-size:.95rem;color:var(--ink-soft);line-height:1.7}}
.app-actions{{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}}
.action{{display:inline-flex;align-items:center;justify-content:center;height:44px;padding:0 16px;border-radius:8px;border:1px solid var(--line-strong);background:rgba(255,255,255,.6);font-size:.95rem;font-weight:600}}
.action.primary{{background:var(--accent-deep);border-color:transparent;color:#fff8f2}}
  .platform-wrap{{padding:24px 24px 26px}}
  .ios-install{{margin-bottom:18px;padding:18px;border:1px solid rgba(30,25,20,.08);border-radius:12px;background:rgba(255,255,255,.5)}}
  .ios-top{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}}
  .ios-title{{font-size:1rem;font-weight:700}}
  .ios-badge{{display:inline-flex;align-items:center;height:26px;padding:0 10px;border-radius:6px;font-size:.78rem;font-weight:700;white-space:nowrap}}
  .ios-badge-ok{{background:#d9f4e4;color:#17603d}}
  .ios-badge-warn{{background:#f8ebc7;color:#8b6114}}
  .android-badge{{display:inline-flex;align-items:center;height:22px;margin-left:8px;padding:0 8px;border-radius:6px;font-size:.72rem;font-weight:700;vertical-align:middle;white-space:nowrap}}
  .android-badge-ok{{background:#d9f4e4;color:#17603d}}
  .android-badge-warn{{background:#f8ebc7;color:#8b6114}}
  .plat-name{{display:flex;align-items:center;flex-wrap:wrap;gap:4px}}
  .ios-copy{{font-size:.9rem;line-height:1.7;color:rgba(24,20,18,.62)}}
  .ios-steps{{margin-top:12px;padding-left:18px;color:rgba(24,20,18,.56);font-size:.84rem;line-height:1.7}}
  .section-label{{margin-bottom:14px;font-size:.82rem;letter-spacing:.14em;color:rgba(24,20,18,.42);text-transform:uppercase}}
.platforms{{display:flex;flex-direction:column;gap:12px}}
.plat{{display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid rgba(30,25,20,.08);border-radius:12px;background:rgba(255,255,255,.5);transition:border-color .18s ease, background .18s ease}}
.plat:hover{{border-color:rgba(30,25,20,.16);background:rgba(255,255,255,.85)}}
.plat-icon{{width:44px;height:44px;display:flex;align-items:center;justify-content:center;flex-shrink:0;border-radius:10px;background:rgba(255,255,255,.7);border:1px solid rgba(30,25,20,.08)}}
.plat-icon svg{{width:24px;height:24px;fill:currentColor;display:block}}
.plat-icon-windows{{color:#1889d6}}
.plat-icon-apple{{color:#181412}}
.plat-icon-linux{{color:#3f6b4c}}
.plat-icon-android{{color:#4f8f4b}}
.plat-info{{flex:1;min-width:0}}
.plat-name{{font-size:1rem;font-weight:700}}
.plat-detail{{margin-top:4px;font-size:.86rem;color:rgba(24,20,18,.52);line-height:1.55}}
.plat-badge{{display:inline-flex;align-items:center;justify-content:center;min-width:74px;height:38px;padding:0 14px;border-radius:8px;background:var(--accent);color:#fff8f2;font-size:.86rem;font-weight:700;white-space:nowrap}}
.footnote{{margin-top:16px;font-size:.84rem;color:rgba(24,20,18,.48);line-height:1.7}}
@media (max-width:1080px){{
  .hero{{grid-template-columns:1fr}}
}}
@media (max-width:720px){{
  .page{{padding:20px 20px 36px}}
  .hero{{padding-top:28px;gap:24px}}
  .app-top{{padding:20px 20px 16px}}
  .platform-wrap{{padding:18px}}
  .app-head{{align-items:flex-start}}
  .icon{{width:60px;height:60px;border-radius:12px}}
  .app-title{{font-size:1.35rem}}
  .title{{font-size:clamp(1.9rem,8vw,2.6rem)}}
}}
</style>
</head>
<body>
<div class="page">
  <div class="nav">
    <a class="brand" href="/">
      <img src="/assets/site-logo.jpg" alt="WebToApp">
      <span>WebToApp</span>
    </a>
    <div class="nav-right">
      <span class="brand-note" data-i18n="navDownload">Download</span>
      <select id="dl-lang" aria-label="Language"></select>
    </div>
  </div>

  <main class="hero">
    <section class="hero-copy">
      <div class="eyebrow" data-i18n="eyebrow">INSTALLATION / DOWNLOAD</div>
      <h1 class="title">{r['name']}</h1>
      <div class="meta-row">
        <span class="meta-chip" data-i18n="chipPlatforms">5 platforms ready</span>
        <span class="meta-chip" data-i18n="chipIcons">Real icons built in</span>
        <span class="meta-chip" data-i18n="chipShare">Link is shareable</span>
      </div>
      <p class="desc" data-i18n="heroDesc">This is not an app store page, just this site's install entry. Pick your device, then download to install, unzip, or add to the iPhone home screen.</p>
      <p class="source">{source_host}</p>
    </section>

    <section class="hero-panel">
      <div class="app-top">
        <div class="app-head">
          <img src="{favicon}" alt="{r['name']}" class="icon" onerror="this.style.display='none'">
          <div>
            <h2 class="app-title">{r['name']}</h2>
            <p class="app-sub" data-i18n="appSub">The multi-platform installers and config profile generated for this site. You can send this page directly to users without explaining the download paths.</p>
          </div>
        </div>
        <div class="app-actions">
          <a class="action primary" href="{r['url']}" target="_blank" rel="noopener noreferrer" data-i18n="{open_site_key}">Open original site</a>
          <a class="action" href="{base}/download/ios" data-i18n="downloadIosProfile">Download iPhone profile</a>
        </div>
      </div>

      <div class="platform-wrap">
        <div class="ios-install">
          <div class="ios-top">
            <div class="ios-title" data-i18n="iosTitle">iPhone install guide</div>
            {ios_badge}
          </div>
          <p class="ios-copy" data-i18n="iosCopy">iPhone and iPad don't need a separate page. Download the profile right in Safari, then finish installing in Settings and the icon appears on your home screen.</p>
          <ol class="ios-steps">
            <li data-i18n="iosStep1">In Safari, tap the iPhone install entry above or below</li>
            <li data-i18n="iosStep2">Download the <code>.mobileconfig</code> profile</li>
            <li data-i18n="iosStep3">Open "Profile Downloaded" in Settings and finish installing</li>
            <li data-i18n="iosStep4">Return to the home screen and tap the icon to open</li>
          </ol>
        </div>
        <p class="section-label" data-i18n="chooseDevice">Choose your device</p>
        <div class="platforms">
{platform_links}
        </div>
        <p class="footnote" data-i18n="footnote">On iPhone install via Safari; on desktop just unzip after downloading. Android ships an installer, while macOS and Windows keep the app icon.</p>
      </div>
    </section>
  </main>
</div>
<script>
(function(){{
  var T = {dl_i18n_json};
  var APP_NAME = "{safe_name}";
  var SUPPORTED = ["en","zh","ja","ar","ru","es","pt","fr","de"];
  var RTL = ["ar"];
  var NAMES = {{en:"English",zh:"\\u4e2d\\u6587",ja:"\\u65e5\\u672c\\u8a9e",ar:"\\u0627\\u0644\\u0639\\u0631\\u0628\\u064a\\u0629",ru:"\\u0420\\u0443\\u0441\\u0441\\u043a\\u0438\\u0439",es:"Espa\\u00f1ol",pt:"Portugu\\u00eas",fr:"Fran\\u00e7ais",de:"Deutsch"}};
  var KEY = "webtoapp-lang-v1";
  function pick(){{
    try{{ var s=localStorage.getItem(KEY); if(s&&SUPPORTED.indexOf(s)!==-1) return s; }}catch(e){{}}
    return "en";
  }}
  var cur = pick();
  function t(k){{ var tb=T[cur]||T.en||{{}}; return (tb[k]!=null)?tb[k]:((T.en||{{}})[k]!=null?T.en[k]:""); }}
  function apply(){{
    document.documentElement.lang = (cur==="zh")?"zh-CN":cur;
    document.documentElement.dir = (RTL.indexOf(cur)!==-1)?"rtl":"ltr";
    document.querySelectorAll("[data-i18n]").forEach(function(el){{
      var v=t(el.getAttribute("data-i18n")); if(v) el.textContent=v;
    }});
    var titleT=t("pageTitle"); if(titleT) document.title=titleT.replace("{{name}}",APP_NAME);
    try{{ localStorage.setItem(KEY,cur); }}catch(e){{}}
  }}
  var sel=document.getElementById("dl-lang");
  SUPPORTED.forEach(function(l){{ var o=document.createElement("option"); o.value=l; o.textContent=NAMES[l]; sel.appendChild(o); }});
  sel.value=cur;
  sel.addEventListener("change",function(){{ cur=sel.value; apply(); }});
  apply();
}})();
</script>
</body>
</html>"""
        return html

    def _download_page_translations(self) -> dict:
        """Translation tables for the in-page language switcher on the download
        page (9 languages). '{name}' in pageTitle is filled in by the page JS."""
        return {
            "en": {
                "pageTitle": "{name} — Download | WebToApp",
                "navDownload": "Download",
                "eyebrow": "INSTALLATION / DOWNLOAD",
                "chipPlatforms": "5 platforms ready",
                "chipIcons": "Real icons built in",
                "chipShare": "Link is shareable",
                "heroDesc": "This is not an app store page, just this site's install entry. Pick your device, then download to install, unzip, or add to the iPhone home screen.",
                "appSub": "The multi-platform installers and config profile generated for this site. You can send this page directly to users without explaining the download paths.",
                "openSite": "Open original site",
                "openApp": "Open app",
                "downloadIosProfile": "Download iPhone profile",
                "iosTitle": "iPhone install guide",
                "iosBadgeSigned": "Signed",
                "iosBadgeUnsigned": "Unsigned, but still installable",
                "iosCopy": "iPhone and iPad don't need a separate page. Download the profile right in Safari, then finish installing in Settings and the icon appears on your home screen.",
                "iosStep1": "In Safari, tap the iPhone install entry above or below",
                "iosStep2": "Download the .mobileconfig profile",
                "iosStep3": "Open \u201cProfile Downloaded\u201d in Settings and finish installing",
                "iosStep4": "Return to the home screen and tap the icon to open",
                "chooseDevice": "Choose your device",
                "footnote": "On iPhone install via Safari; on desktop just unzip after downloading. Android ships an installer, while macOS and Windows keep the app icon.",
                "actionInstall": "Install",
                "actionDownload": "Download",
                "platIosDetail": ".mobileconfig \u00b7 download & install via Safari",
                "platAndroidDetail": ".apk / .zip \u00b7 install directly or unzip",
                "platAndroidApkDetail": ".apk \u00b7 signed WebView installer",
                "platAndroidZipDetail": ".zip \u00b7 lightweight PWA package",
                "androidBadgeApk": "Signed APK",
                "androidBadgeZip": "PWA package",
                "platMacDetail": ".zip \u00b7 native .app icon \u00b7 drag to Applications",
                "platWinDetail": ".zip \u00b7 native icon \u00b7 unzip and run",
                "platLinuxDetail": ".tar.gz \u00b7 desktop icon included \u00b7 unzip and run",
            },
            "zh": {
                "pageTitle": "{name} — 下载安装 | WebToApp",
                "navDownload": "下载",
                "eyebrow": "INSTALLATION / 下载页",
                "chipPlatforms": "5 个平台已就绪",
                "chipIcons": "真实图标已内置",
                "chipShare": "链接可直接分享",
                "heroDesc": "这不是一个市场页，只是这个网站的安装入口。选好你的设备，下载后就可以直接安装、解压，或者在 iPhone 上添加到主屏幕。",
                "appSub": "为当前站点生成的多端安装包与安装描述文件。你可以直接把这个页面发给用户，不需要再解释下载路径。",
                "openSite": "打开原站",
                "openApp": "打开应用",
                "downloadIosProfile": "下载 iPhone 描述文件",
                "iosTitle": "iPhone 安装说明",
                "iosBadgeSigned": "已签名",
                "iosBadgeUnsigned": "未签名，但仍可安装",
                "iosCopy": "iPhone 和 iPad 不需要单独跳去另一个页面。直接在 Safari 里下载描述文件，然后去设置里完成安装，桌面就会出现图标。",
                "iosStep1": "在 Safari 中点击上方或下方的 iPhone 安装入口",
                "iosStep2": "下载 .mobileconfig 描述文件",
                "iosStep3": "打开“设置”中的“已下载描述文件”并完成安装",
                "iosStep4": "回到桌面，点击图标即可打开",
                "chooseDevice": "选择设备",
                "footnote": "iPhone 请在 Safari 中安装；桌面端下载后直接解压即可。Android 提供安装包，macOS 与 Windows 会保留应用图标。",
                "actionInstall": "安装",
                "actionDownload": "下载",
                "platIosDetail": ".mobileconfig · Safari 下载并安装描述文件",
                "platAndroidDetail": ".apk / .zip · 直接安装或解压使用",
                "platAndroidApkDetail": ".apk · 已签名 WebView 安装包",
                "platAndroidZipDetail": ".zip · 轻量 PWA 包",
                "androidBadgeApk": "已签名 APK",
                "androidBadgeZip": "PWA 包",
                "platMacDetail": ".zip · 原生 .app 图标 · 拖入应用文件夹",
                "platWinDetail": ".zip · 原生图标 · 解压即用",
                "platLinuxDetail": ".tar.gz · 桌面图标已内置 · 解压运行",
            },
            "ja": {
                "pageTitle": "{name} — ダウンロード | WebToApp",
                "navDownload": "ダウンロード",
                "eyebrow": "INSTALLATION / ダウンロード",
                "chipPlatforms": "5 プラットフォーム対応",
                "chipIcons": "実アイコン内蔵",
                "chipShare": "リンク共有可",
                "heroDesc": "これはアプリストアのページではなく、このサイトのインストール入口です。デバイスを選び、ダウンロードしてインストール・解凍、または iPhone のホーム画面に追加してください。",
                "appSub": "このサイト向けに生成したマルチプラットフォームのインストーラーと構成プロファイルです。このページをそのままユーザーに送れます。ダウンロード手順を説明する必要はありません。",
                "openSite": "元のサイトを開く",
                "openApp": "アプリを開く",
                "downloadIosProfile": "iPhone プロファイルをダウンロード",
                "iosTitle": "iPhone インストール手順",
                "iosBadgeSigned": "署名済み",
                "iosBadgeUnsigned": "未署名ですがインストール可能",
                "iosCopy": "iPhone と iPad は別ページに移動する必要はありません。Safari でプロファイルをダウンロードし、設定でインストールを完了するとホーム画面にアイコンが表示されます。",
                "iosStep1": "Safari で上または下の iPhone インストール入口をタップ",
                "iosStep2": ".mobileconfig プロファイルをダウンロード",
                "iosStep3": "設定の「ダウンロード済みプロファイル」を開いてインストールを完了",
                "iosStep4": "ホーム画面に戻り、アイコンをタップして開く",
                "chooseDevice": "デバイスを選択",
                "footnote": "iPhone は Safari でインストール。デスクトップはダウンロード後に解凍するだけ。Android はインストーラー、macOS と Windows はアプリアイコンを保持します。",
                "actionInstall": "インストール",
                "actionDownload": "ダウンロード",
                "platIosDetail": ".mobileconfig · Safari でダウンロードしてインストール",
                "platAndroidDetail": ".apk / .zip · そのままインストールまたは解凍",
                "platAndroidApkDetail": ".apk · 署名済み WebView インストーラー",
                "platAndroidZipDetail": ".zip · 軽量 PWA パッケージ",
                "androidBadgeApk": "署名済み APK",
                "androidBadgeZip": "PWA パッケージ",
                "platMacDetail": ".zip · ネイティブ .app アイコン · アプリケーションにドラッグ",
                "platWinDetail": ".zip · ネイティブアイコン · 解凍して実行",
                "platLinuxDetail": ".tar.gz · デスクトップアイコン内蔵 · 解凍して実行",
            },
            "ar": {
                "pageTitle": "{name} — تنزيل | WebToApp",
                "navDownload": "تنزيل",
                "eyebrow": "INSTALLATION / التثبيت",
                "chipPlatforms": "5 منصات جاهزة",
                "chipIcons": "أيقونات حقيقية مدمجة",
                "chipShare": "الرابط قابل للمشاركة",
                "heroDesc": "هذه ليست صفحة متجر تطبيقات، بل مجرد مدخل التثبيت لهذا الموقع. اختر جهازك، ثم نزّل للتثبيت أو فك الضغط أو الإضافة إلى الشاشة الرئيسية في iPhone.",
                "appSub": "حِزم التثبيت متعددة المنصات وملف التهيئة المُولّدة لهذا الموقع. يمكنك إرسال هذه الصفحة مباشرةً إلى المستخدمين دون شرح مسارات التنزيل.",
                "openSite": "فتح الموقع الأصلي",
                "openApp": "فتح التطبيق",
                "downloadIosProfile": "تنزيل ملف تعريف iPhone",
                "iosTitle": "دليل تثبيت iPhone",
                "iosBadgeSigned": "موقّع",
                "iosBadgeUnsigned": "غير موقّع، لكن قابل للتثبيت",
                "iosCopy": "لا يحتاج iPhone وiPad إلى صفحة منفصلة. نزّل ملف التعريف مباشرةً في Safari، ثم أكمل التثبيت من الإعدادات وستظهر الأيقونة على الشاشة الرئيسية.",
                "iosStep1": "في Safari، اضغط مدخل تثبيت iPhone أعلى أو أسفل",
                "iosStep2": "نزّل ملف تعريف .mobileconfig",
                "iosStep3": "افتح \u201cملف التعريف الذي تم تنزيله\u201d في الإعدادات وأكمل التثبيت",
                "iosStep4": "ارجع إلى الشاشة الرئيسية واضغط الأيقونة لفتحه",
                "chooseDevice": "اختر جهازك",
                "footnote": "على iPhone ثبّت عبر Safari؛ على سطح المكتب فك الضغط بعد التنزيل. يوفّر Android حزمة تثبيت، بينما يحتفظ macOS وWindows بأيقونة التطبيق.",
                "actionInstall": "تثبيت",
                "actionDownload": "تنزيل",
                "platIosDetail": ".mobileconfig · التنزيل والتثبيت عبر Safari",
                "platAndroidDetail": ".apk / .zip · التثبيت مباشرةً أو فك الضغط",
                "platAndroidApkDetail": ".apk · مثبت WebView موقّع",
                "platAndroidZipDetail": ".zip · حزمة PWA خفيفة",
                "androidBadgeApk": "APK موقّع",
                "androidBadgeZip": "حزمة PWA",
                "platMacDetail": ".zip · أيقونة .app أصلية · اسحبها إلى التطبيقات",
                "platWinDetail": ".zip · أيقونة أصلية · فك الضغط والتشغيل",
                "platLinuxDetail": ".tar.gz · أيقونة سطح المكتب مدمجة · فك الضغط والتشغيل",
            },
            "ru": {
                "pageTitle": "{name} — Скачать | WebToApp",
                "navDownload": "Скачать",
                "eyebrow": "INSTALLATION / УСТАНОВКА",
                "chipPlatforms": "5 платформ готовы",
                "chipIcons": "Настоящие значки встроены",
                "chipShare": "Ссылкой можно делиться",
                "heroDesc": "Это не страница магазина приложений, а просто точка установки этого сайта. Выберите устройство, затем скачайте, чтобы установить, распаковать или добавить на главный экран iPhone.",
                "appSub": "Кроссплатформенные установщики и профиль конфигурации, созданные для этого сайта. Эту страницу можно отправлять пользователям без объяснения путей загрузки.",
                "openSite": "Открыть исходный сайт",
                "openApp": "Открыть приложение",
                "downloadIosProfile": "Скачать профиль iPhone",
                "iosTitle": "Инструкция по установке на iPhone",
                "iosBadgeSigned": "Подписано",
                "iosBadgeUnsigned": "Без подписи, но устанавливается",
                "iosCopy": "Для iPhone и iPad не нужна отдельная страница. Скачайте профиль прямо в Safari, затем завершите установку в Настройках — значок появится на главном экране.",
                "iosStep1": "В Safari нажмите точку установки iPhone выше или ниже",
                "iosStep2": "Скачайте профиль .mobileconfig",
                "iosStep3": "Откройте «Загруженный профиль» в Настройках и завершите установку",
                "iosStep4": "Вернитесь на главный экран и нажмите значок, чтобы открыть",
                "chooseDevice": "Выберите устройство",
                "footnote": "На iPhone устанавливайте через Safari; на десктопе просто распакуйте после загрузки. Android поставляется с установщиком, а macOS и Windows сохраняют значок приложения.",
                "actionInstall": "Установить",
                "actionDownload": "Скачать",
                "platIosDetail": ".mobileconfig · загрузка и установка через Safari",
                "platAndroidDetail": ".apk / .zip · установка напрямую или распаковка",
                "platAndroidApkDetail": ".apk · подписанный WebView-установщик",
                "platAndroidZipDetail": ".zip · лёгкий PWA-пакет",
                "androidBadgeApk": "Подписанный APK",
                "androidBadgeZip": "PWA-пакет",
                "platMacDetail": ".zip · родной значок .app · перетащите в Программы",
                "platWinDetail": ".zip · родной значок · распакуйте и запустите",
                "platLinuxDetail": ".tar.gz · значок рабочего стола включён · распакуйте и запустите",
            },
            "es": {
                "pageTitle": "{name} — Descargar | WebToApp",
                "navDownload": "Descargar",
                "eyebrow": "INSTALLATION / INSTALACIÓN",
                "chipPlatforms": "5 plataformas listas",
                "chipIcons": "Iconos reales incluidos",
                "chipShare": "Enlace para compartir",
                "heroDesc": "Esta no es una página de tienda de apps, solo la entrada de instalación de este sitio. Elige tu dispositivo y descarga para instalar, descomprimir o añadir a la pantalla de inicio del iPhone.",
                "appSub": "Los instaladores multiplataforma y el perfil de configuración generados para este sitio. Puedes enviar esta página directamente a los usuarios sin explicar las rutas de descarga.",
                "openSite": "Abrir sitio original",
                "openApp": "Abrir aplicación",
                "downloadIosProfile": "Descargar perfil de iPhone",
                "iosTitle": "Guía de instalación en iPhone",
                "iosBadgeSigned": "Firmado",
                "iosBadgeUnsigned": "Sin firmar, pero instalable",
                "iosCopy": "iPhone y iPad no necesitan otra página. Descarga el perfil en Safari, luego termina de instalar en Ajustes y el icono aparecerá en la pantalla de inicio.",
                "iosStep1": "En Safari, toca la entrada de instalación de iPhone arriba o abajo",
                "iosStep2": "Descarga el perfil .mobileconfig",
                "iosStep3": "Abre \u201cPerfil descargado\u201d en Ajustes y termina de instalar",
                "iosStep4": "Vuelve a la pantalla de inicio y toca el icono para abrir",
                "chooseDevice": "Elige tu dispositivo",
                "footnote": "En iPhone instala con Safari; en escritorio solo descomprime tras descargar. Android incluye un instalador, mientras que macOS y Windows conservan el icono de la app.",
                "actionInstall": "Instalar",
                "actionDownload": "Descargar",
                "platIosDetail": ".mobileconfig · descarga e instala con Safari",
                "platAndroidDetail": ".apk / .zip · instala directamente o descomprime",
                "platAndroidApkDetail": ".apk · instalador WebView firmado",
                "platAndroidZipDetail": ".zip · paquete PWA ligero",
                "androidBadgeApk": "APK firmado",
                "androidBadgeZip": "Paquete PWA",
                "platMacDetail": ".zip · icono nativo .app · arrastra a Aplicaciones",
                "platWinDetail": ".zip · icono nativo · descomprime y ejecuta",
                "platLinuxDetail": ".tar.gz · icono de escritorio incluido · descomprime y ejecuta",
            },
            "pt": {
                "pageTitle": "{name} — Baixar | WebToApp",
                "navDownload": "Baixar",
                "eyebrow": "INSTALLATION / INSTALAÇÃO",
                "chipPlatforms": "5 plataformas prontas",
                "chipIcons": "Ícones reais incluídos",
                "chipShare": "Link compartilhável",
                "heroDesc": "Esta não é uma página de loja de apps, apenas a entrada de instalação deste site. Escolha seu dispositivo e baixe para instalar, descompactar ou adicionar à tela inicial do iPhone.",
                "appSub": "Os instaladores multiplataforma e o perfil de configuração gerados para este site. Você pode enviar esta página diretamente aos usuários sem explicar os caminhos de download.",
                "openSite": "Abrir site original",
                "openApp": "Abrir aplicativo",
                "downloadIosProfile": "Baixar perfil do iPhone",
                "iosTitle": "Guia de instalação no iPhone",
                "iosBadgeSigned": "Assinado",
                "iosBadgeUnsigned": "Não assinado, mas instalável",
                "iosCopy": "iPhone e iPad não precisam de outra página. Baixe o perfil no Safari, depois conclua a instalação em Ajustes e o ícone aparecerá na tela inicial.",
                "iosStep1": "No Safari, toque na entrada de instalação do iPhone acima ou abaixo",
                "iosStep2": "Baixe o perfil .mobileconfig",
                "iosStep3": "Abra \u201cPerfil Baixado\u201d em Ajustes e conclua a instalação",
                "iosStep4": "Volte à tela inicial e toque no ícone para abrir",
                "chooseDevice": "Escolha seu dispositivo",
                "footnote": "No iPhone instale via Safari; no desktop apenas descompacte após baixar. O Android traz um instalador, enquanto macOS e Windows mantêm o ícone do app.",
                "actionInstall": "Instalar",
                "actionDownload": "Baixar",
                "platIosDetail": ".mobileconfig · baixe e instale via Safari",
                "platAndroidDetail": ".apk / .zip · instale direto ou descompacte",
                "platAndroidApkDetail": ".apk · instalador WebView assinado",
                "platAndroidZipDetail": ".zip · pacote PWA leve",
                "androidBadgeApk": "APK assinado",
                "androidBadgeZip": "Pacote PWA",
                "platMacDetail": ".zip · ícone nativo .app · arraste para Aplicativos",
                "platWinDetail": ".zip · ícone nativo · descompacte e execute",
                "platLinuxDetail": ".tar.gz · ícone de desktop incluído · descompacte e execute",
            },
            "fr": {
                "pageTitle": "{name} — Télécharger | WebToApp",
                "navDownload": "Télécharger",
                "eyebrow": "INSTALLATION / INSTALLATION",
                "chipPlatforms": "5 plateformes prêtes",
                "chipIcons": "Vraies icônes intégrées",
                "chipShare": "Lien partageable",
                "heroDesc": "Ce n'est pas une page de boutique d'applications, juste le point d'installation de ce site. Choisissez votre appareil, puis téléchargez pour installer, décompresser ou ajouter à l'écran d'accueil de l'iPhone.",
                "appSub": "Les installateurs multiplateformes et le profil de configuration générés pour ce site. Vous pouvez envoyer cette page directement aux utilisateurs sans expliquer les chemins de téléchargement.",
                "openSite": "Ouvrir le site d'origine",
                "openApp": "Ouvrir l'application",
                "downloadIosProfile": "Télécharger le profil iPhone",
                "iosTitle": "Guide d'installation iPhone",
                "iosBadgeSigned": "Signé",
                "iosBadgeUnsigned": "Non signé, mais installable",
                "iosCopy": "iPhone et iPad n'ont pas besoin d'une page séparée. Téléchargez le profil dans Safari, puis terminez l'installation dans Réglages et l'icône apparaît sur l'écran d'accueil.",
                "iosStep1": "Dans Safari, touchez l'entrée d'installation iPhone ci-dessus ou ci-dessous",
                "iosStep2": "Téléchargez le profil .mobileconfig",
                "iosStep3": "Ouvrez \u201cProfil téléchargé\u201d dans Réglages et terminez l'installation",
                "iosStep4": "Revenez à l'écran d'accueil et touchez l'icône pour ouvrir",
                "chooseDevice": "Choisissez votre appareil",
                "footnote": "Sur iPhone, installez via Safari ; sur ordinateur, décompressez après le téléchargement. Android fournit un installateur, tandis que macOS et Windows conservent l'icône de l'app.",
                "actionInstall": "Installer",
                "actionDownload": "Télécharger",
                "platIosDetail": ".mobileconfig · téléchargez et installez via Safari",
                "platAndroidDetail": ".apk / .zip · installez directement ou décompressez",
                "platAndroidApkDetail": ".apk · installateur WebView signé",
                "platAndroidZipDetail": ".zip · paquet PWA léger",
                "androidBadgeApk": "APK signé",
                "androidBadgeZip": "Paquet PWA",
                "platMacDetail": ".zip · icône native .app · glissez vers Applications",
                "platWinDetail": ".zip · icône native · décompressez et exécutez",
                "platLinuxDetail": ".tar.gz · icône de bureau incluse · décompressez et exécutez",
            },
            "de": {
                "pageTitle": "{name} — Herunterladen | WebToApp",
                "navDownload": "Herunterladen",
                "eyebrow": "INSTALLATION / INSTALLATION",
                "chipPlatforms": "5 Plattformen bereit",
                "chipIcons": "Echte Icons integriert",
                "chipShare": "Link teilbar",
                "heroDesc": "Dies ist keine App-Store-Seite, nur der Installationseinstieg dieser Website. Wähle dein Gerät und lade herunter, um zu installieren, zu entpacken oder zum iPhone-Startbildschirm hinzuzufügen.",
                "appSub": "Die plattformübergreifenden Installer und das Konfigurationsprofil, die für diese Website erstellt wurden. Du kannst diese Seite direkt an Nutzer senden, ohne die Download-Pfade zu erklären.",
                "openSite": "Originalseite öffnen",
                "openApp": "App öffnen",
                "downloadIosProfile": "iPhone-Profil herunterladen",
                "iosTitle": "iPhone-Installationsanleitung",
                "iosBadgeSigned": "Signiert",
                "iosBadgeUnsigned": "Unsigniert, aber installierbar",
                "iosCopy": "iPhone und iPad brauchen keine separate Seite. Lade das Profil direkt in Safari herunter, schließe die Installation in den Einstellungen ab und das Icon erscheint auf dem Startbildschirm.",
                "iosStep1": "Tippe in Safari oben oder unten auf den iPhone-Installationseinstieg",
                "iosStep2": "Lade das .mobileconfig-Profil herunter",
                "iosStep3": "Öffne \u201eGeladenes Profil\u201c in den Einstellungen und schließe die Installation ab",
                "iosStep4": "Kehre zum Startbildschirm zurück und tippe auf das Icon zum Öffnen",
                "chooseDevice": "Gerät auswählen",
                "footnote": "Auf dem iPhone über Safari installieren; am Desktop nach dem Download einfach entpacken. Android liefert einen Installer, während macOS und Windows das App-Icon behalten.",
                "actionInstall": "Installieren",
                "actionDownload": "Herunterladen",
                "platIosDetail": ".mobileconfig · über Safari herunterladen & installieren",
                "platAndroidDetail": ".apk / .zip · direkt installieren oder entpacken",
                "platAndroidApkDetail": ".apk · signierter WebView-Installer",
                "platAndroidZipDetail": ".zip · leichtes PWA-Paket",
                "androidBadgeApk": "Signiertes APK",
                "androidBadgeZip": "PWA-Paket",
                "platMacDetail": ".zip · natives .app-Icon · in Programme ziehen",
                "platWinDetail": ".zip · natives Icon · entpacken und ausführen",
                "platLinuxDetail": ".tar.gz · Desktop-Icon enthalten · entpacken und ausführen",
            },
        }

    def _pwa_translations(self) -> dict:
        """Translations for the Android PWA shell page (loading + fallback
        notice). '{name}' in loadingTitle is filled in by the page JS."""
        return {
            "en": {
                "loadingTitle": "Opening {name}",
                "loadingSub": "Loading the target site for you",
                "noticeTitle": "This site can't be shown directly in this window",
                "noticeBody": "Some sites block iframes or require the system browser to finish login, payment or redirects. If that happens, go back and use \u201cOpen full\u201d in the top-right of the site preview.",
                "retry": "Reload",
            },
            "zh": {
                "loadingTitle": "正在打开 {name}",
                "loadingSub": "正在为你打开目标站点",
                "noticeTitle": "这个站点无法在当前窗口直接显示",
                "noticeBody": "部分站点会阻止 iframe 或要求系统浏览器完成登录、支付与跳转。遇到这种情况，请返回上一级，在网站预览窗口右上角使用完整打开。",
                "retry": "重新载入",
            },
            "ja": {
                "loadingTitle": "{name} を開いています",
                "loadingSub": "対象サイトを読み込んでいます",
                "noticeTitle": "このサイトはこのウィンドウで直接表示できません",
                "noticeBody": "一部のサイトは iframe をブロックするか、ログイン・支払い・リダイレクトをシステムブラウザで完了するよう求めます。その場合は前の画面に戻り、サイトプレビュー右上の「フルで開く」を使ってください。",
                "retry": "再読み込み",
            },
            "ar": {
                "loadingTitle": "جارٍ فتح {name}",
                "loadingSub": "جارٍ تحميل الموقع المستهدف لك",
                "noticeTitle": "لا يمكن عرض هذا الموقع مباشرةً في هذه النافذة",
                "noticeBody": "تحظر بعض المواقع إطارات iframe أو تتطلب متصفح النظام لإكمال تسجيل الدخول أو الدفع أو إعادة التوجيه. في هذه الحالة، ارجع واستخدم \u201cفتح كامل\u201d في أعلى يمين معاينة الموقع.",
                "retry": "إعادة التحميل",
            },
            "ru": {
                "loadingTitle": "Открываем {name}",
                "loadingSub": "Загружаем целевой сайт для вас",
                "noticeTitle": "Этот сайт нельзя показать прямо в этом окне",
                "noticeBody": "Некоторые сайты блокируют iframe или требуют системный браузер для входа, оплаты или переходов. В этом случае вернитесь назад и используйте «Открыть полностью» в правом верхнем углу предпросмотра сайта.",
                "retry": "Перезагрузить",
            },
            "es": {
                "loadingTitle": "Abriendo {name}",
                "loadingSub": "Cargando el sitio de destino para ti",
                "noticeTitle": "Este sitio no se puede mostrar directamente en esta ventana",
                "noticeBody": "Algunos sitios bloquean los iframes o requieren el navegador del sistema para completar el inicio de sesión, el pago o las redirecciones. Si ocurre, vuelve atrás y usa \u201cAbrir completo\u201d en la esquina superior derecha de la vista previa del sitio.",
                "retry": "Recargar",
            },
            "pt": {
                "loadingTitle": "Abrindo {name}",
                "loadingSub": "Carregando o site de destino para você",
                "noticeTitle": "Este site não pode ser exibido diretamente nesta janela",
                "noticeBody": "Alguns sites bloqueiam iframes ou exigem o navegador do sistema para concluir login, pagamento ou redirecionamentos. Se isso acontecer, volte e use \u201cAbrir completo\u201d no canto superior direito da pré-visualização do site.",
                "retry": "Recarregar",
            },
            "fr": {
                "loadingTitle": "Ouverture de {name}",
                "loadingSub": "Chargement du site cible pour vous",
                "noticeTitle": "Ce site ne peut pas s'afficher directement dans cette fenêtre",
                "noticeBody": "Certains sites bloquent les iframes ou exigent le navigateur système pour terminer la connexion, le paiement ou les redirections. Dans ce cas, revenez en arrière et utilisez \u201cOuvrir en entier\u201d en haut à droite de l'aperçu du site.",
                "retry": "Recharger",
            },
            "de": {
                "loadingTitle": "{name} wird geöffnet",
                "loadingSub": "Die Zielseite wird für dich geladen",
                "noticeTitle": "Diese Seite kann in diesem Fenster nicht direkt angezeigt werden",
                "noticeBody": "Manche Seiten blockieren iframes oder verlangen den System-Browser für Login, Zahlung oder Weiterleitungen. Gehe in dem Fall zurück und nutze \u201eVollständig öffnen\u201c oben rechts in der Seitenvorschau.",
                "retry": "Neu laden",
            },
        }

    def _write_pwa_files(self, app_dir: Path, r: dict, launch_url: str):
        # Prefer the locally-cached high-res icon for the manifest & meta tags.
        icon_path = app_dir / "icon.png"
        if icon_path.exists():
            icon_url = f"/a/{r['id']}/icon.png"
            icon_dim = self._png_dimension(icon_path.read_bytes()) or 256
            icon_sizes = f"{icon_dim}x{icon_dim}"
        else:
            host = r["url"].split("//")[-1].split("/")[0]
            icon_url = f"https://www.google.com/s2/favicons?domain={host}&sz=512"
            icon_sizes = "512x512"

        shell_start_url = f"/a/{r['id']}/pwa"
        shell_scope = f"/a/{r['id']}/"

        manifest = {
            "name": r["name"], "short_name": r["name"][:12],
            "start_url": shell_start_url, "scope": shell_scope, "display": self._normalized_display_mode(r.get("display")),
            "orientation": r["orientation"],
            "background_color": r["color"], "theme_color": r["color"],
            "icons": [
                {"src": icon_url, "sizes": icon_sizes, "type": "image/png", "purpose": "any maskable"},
            ],
        }
        (app_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

        cache = f"distill-{r['id']}-v1"
        (app_dir / "sw.js").write_text(
            f"const C='{cache}';"
            "self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(['.'])));self.skipWaiting()});"
            "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))));self.clients.claim()});"
            "self.addEventListener('fetch',e=>{e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)))});"
        )

        pwa_i18n = self._pwa_translations()
        pwa_i18n_json = json.dumps(pwa_i18n, ensure_ascii=False)
        safe_name = (r["name"] or "").replace("\\", "\\\\").replace('"', '\\"')
        pwa_html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<meta name="theme-color" content="{r['color']}">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="{icon_url}">
<title>{r['name']}</title>
<link rel="manifest" href="manifest.json">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;overflow:hidden;background:{r['color']};font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
body{{position:relative}}
.shell{{position:relative;width:100%;height:100%}}
iframe{{position:absolute;inset:0;width:100%;height:100%;border:none;overflow:hidden;background:#fff}}
.loading{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:linear-gradient(180deg,rgba(9,9,11,.18),rgba(9,9,11,.34));z-index:3;transition:opacity .2s ease}}
.loading.hidden{{opacity:0;pointer-events:none}}
.loading-card{{display:flex;flex-direction:column;align-items:center;gap:12px;padding:20px 22px;border-radius:18px;background:rgba(9,9,11,.68);color:#fff;border:1px solid rgba(255,255,255,.12);backdrop-filter:blur(14px)}}
.spinner{{width:28px;height:28px;border-radius:50%;border:2px solid rgba(255,255,255,.22);border-top-color:#fff;animation:spin 1s linear infinite}}
.loading-title{{font-size:14px;font-weight:600}}
.loading-sub{{font-size:12px;color:rgba(255,255,255,.7)}}
.notice{{position:absolute;left:12px;right:12px;bottom:max(12px,env(safe-area-inset-bottom));z-index:4;padding:16px;border-radius:18px;background:rgba(9,9,11,.82);border:1px solid rgba(255,255,255,.14);color:#fff;backdrop-filter:blur(16px);display:none}}
.notice.show{{display:block}}
.notice strong{{display:block;font-size:14px;margin-bottom:6px}}
.notice p{{font-size:12px;line-height:1.55;color:rgba(255,255,255,.72)}}
.notice-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}}
.btn{{display:inline-flex;align-items:center;justify-content:center;min-width:112px;height:38px;padding:0 14px;border-radius:999px;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.05);color:#fff;text-decoration:none;font-size:12px;font-weight:600}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head><body>
<div class="shell">
  <iframe
    id="app-frame"
    src="{launch_url}"
    allow="fullscreen; clipboard-read; clipboard-write"
    loading="eager"
    referrerpolicy="no-referrer"
    sandbox="allow-downloads allow-forms allow-modals allow-orientation-lock allow-pointer-lock allow-presentation allow-same-origin allow-scripts"
  ></iframe>
  <div id="loading" class="loading">
    <div class="loading-card">
      <div class="spinner"></div>
      <div class="loading-title" id="loading-title"></div>
      <div class="loading-sub" id="loading-sub"></div>
    </div>
  </div>
  <div id="notice" class="notice">
    <strong id="notice-title"></strong>
    <p id="notice-body"></p>
    <div class="notice-actions">
      <button id="retry-btn" class="btn" type="button"></button>
    </div>
  </div>
</div>
<script>
if('serviceWorker' in navigator)navigator.serviceWorker.register('sw.js');
(function(){{
  var T = {pwa_i18n_json};
  var APP_NAME = "{safe_name}";
  var SUPPORTED = ["en","zh","ja","ar","ru","es","pt","fr","de"];
  var RTL = ["ar"];
  var KEY = "webtoapp-lang-v1";
  var cur = (function(){{
    try{{ var s=localStorage.getItem(KEY); if(s&&SUPPORTED.indexOf(s)!==-1) return s; }}catch(e){{}}
    return "en";
  }})();
  function t(k){{ var tb=T[cur]||T.en||{{}}; return (tb[k]!=null)?tb[k]:((T.en||{{}})[k]!=null?T.en[k]:""); }}
  document.documentElement.lang = (cur==="zh")?"zh-CN":cur;
  document.documentElement.dir = (RTL.indexOf(cur)!==-1)?"rtl":"ltr";
  document.getElementById('loading-title').textContent = t('loadingTitle').replace("{{name}}", APP_NAME);
  document.getElementById('loading-sub').textContent = t('loadingSub');
  document.getElementById('notice-title').textContent = t('noticeTitle');
  document.getElementById('notice-body').textContent = t('noticeBody');
  document.getElementById('retry-btn').textContent = t('retry');

  const frame = document.getElementById('app-frame');
  const loading = document.getElementById('loading');
  const notice = document.getElementById('notice');
  const retryBtn = document.getElementById('retry-btn');
  const launchUrl = {json.dumps(launch_url)};
  let settled = false;
  let timer = null;


  function stopTimer(){{
    if (timer) {{
      clearTimeout(timer);
      timer = null;
    }}
  }}

  function showFallback(){{
    if (settled) return;
    loading.classList.add('hidden');
    notice.classList.add('show');
  }}

  function armFallback(){{
    settled = false;
    loading.classList.remove('hidden');
    notice.classList.remove('show');
    stopTimer();
    timer = window.setTimeout(showFallback, 6000);
  }}

  frame.addEventListener('load', function(){{
    settled = true;
    stopTimer();
    loading.classList.add('hidden');
    notice.classList.remove('show');
  }});

  frame.addEventListener('error', showFallback);
  retryBtn.addEventListener('click', function(){{
    armFallback();
    frame.src = launchUrl + (launchUrl.indexOf('?') === -1 ? '?' : '&') + '_reload=' + Date.now();
  }});

  armFallback();
}})();
</script>
</body></html>"""
        (app_dir / "pwa.html").write_text(pwa_html)

    # ===== Windows — .bat + .ico =====
    def _build_windows(self, dl: Path, r: dict, icon_png, launch_url):
        win_flags = self._desktop_window_flags(r.get("options") or {}, "windows")
        bat = f"""@echo off
title {r['name']}
set "URL={launch_url}"
(where msedge >nul 2>&1) && (start "" msedge --app="%URL%" --new-window{win_flags} & exit /b)
(where chrome >nul 2>&1) && (start "" chrome --app="%URL%" --new-window{win_flags} & exit /b)
start "" "%URL%"
"""
        # VBS shortcut creator — auto-creates a desktop shortcut with the app icon
        vbs = f"""Set ws = CreateObject("WScript.Shell")
Set sc = ws.CreateShortcut(ws.SpecialFolders("Desktop") & "\\{r['name']}.lnk")
sc.TargetPath = ws.CurrentDirectory & "\\{r['name']}.bat"
sc.WorkingDirectory = ws.CurrentDirectory
sc.IconLocation = ws.CurrentDirectory & "\\icon.ico"
sc.Description = "{r['name']} - WebToApp"
sc.Save
WScript.Echo "桌面快捷方式已创建！"
"""
        with zipfile.ZipFile(dl / "windows.zip", 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{r['name']}/{r['name']}.bat", bat)
            z.writestr(f"{r['name']}/创建桌面快捷方式.vbs", vbs)
            if icon_png:
                z.writestr(f"{r['name']}/icon.ico", self._png_to_ico(icon_png))
                z.writestr(f"{r['name']}/icon.png", icon_png)

    # ===== macOS — .app bundle + .icns =====
    def _build_macos(self, dl: Path, r: dict, icon_png, launch_url):
        n = r['name']
        # Launch chain, best experience first:
        #   1. The compiled WKWebView helper (Contents/MacOS/wta_webview) — a
        #      native window running inside our bundle identity, so the menu
        #      bar shows the app name and the bundle's ATS exceptions apply.
        #      A non-zero exit (bad target, missing frameworks on old macOS)
        #      falls through to the paths below.
        #   2. A WKWebView window driven by the system osascript (JXA) — works
        #      with zero dependencies but runs under osascript's identity.
        #      app.js refuses plain-http targets (ATS blocks them inside
        #      WKWebView) and exits non-zero so the shell falls through.
        #   3. A Chromium-family browser's "app mode" (--app=URL).
        #   4. The default browser as a last resort.
        launcher = f"""#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
RES="$DIR/../Resources"
export WTA_URL={shlex.quote(launch_url)}
export WTA_NAME={shlex.quote(n)}
export WTA_ICON="$RES/AppIcon.icns"
HELPER="$DIR/{_MACOS_HELPER_NAME}"
if [ -x "$HELPER" ]; then
    "$HELPER"
    [ $? -eq 0 ] && exit 0
fi
if /usr/bin/osascript -l JavaScript "$RES/app.js"; then
    exit 0
fi
CHROMIUM_APPS=(
    "Google Chrome"
    "Google Chrome Canary"
    "Chromium"
    "Microsoft Edge"
    "Brave Browser"
    "Vivaldi"
    "Arc"
    "Opera"
)
for app in "${{CHROMIUM_APPS[@]}}"; do
    if [ -d "/Applications/$app.app" ] || [ -d "$HOME/Applications/$app.app" ]; then
        open -na "$app" --args --app="$WTA_URL"
        exit 0
    fi
done
open "$WTA_URL"
"""
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>launcher</string>
<key>CFBundleName</key><string>{n}</string>
<key>CFBundleIdentifier</key><string>com.webtoapp.{r['id']}</string>
<key>CFBundleVersion</key><string>1.0</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsArbitraryLoads</key><true/></dict>
</dict></plist>"""
        helper_path = _MACOS_TEMPLATE_DIR / _MACOS_HELPER_NAME
        with zipfile.ZipFile(dl / "macos.zip", 'w', zipfile.ZIP_DEFLATED) as z:
            info = zipfile.ZipInfo(f"{n}.app/Contents/MacOS/launcher")
            info.external_attr = 0o755 << 16
            z.writestr(info, launcher)
            if helper_path.exists():
                helper_info = zipfile.ZipInfo(f"{n}.app/Contents/MacOS/{_MACOS_HELPER_NAME}")
                helper_info.external_attr = 0o755 << 16
                z.writestr(helper_info, helper_path.read_bytes())
            z.writestr(f"{n}.app/Contents/Resources/app.js", _MACOS_WEBVIEW_APP_JS)
            z.writestr(f"{n}.app/Contents/Info.plist", plist)
            if icon_png:
                z.writestr(f"{n}.app/Contents/Resources/AppIcon.icns", self._png_to_icns(icon_png))

    # ===== Linux — .desktop + icon.png =====
    def _build_linux(self, dl: Path, r: dict, icon_png, launch_url):
        n = r['name']
        linux_flags = self._desktop_window_flags(r.get("options") or {}, "linux")
        # Icon path: relative to install location
        icon_line = f"Icon=$HOME/.local/share/icons/{n}.png" if icon_png else "Icon=web-browser"
        desktop = f"""[Desktop Entry]
Type=Application
Name={n}
Comment=Distilled by WebToApp
Exec=bash -c 'URL="{launch_url}"; for b in google-chrome chromium-browser microsoft-edge firefox; do command -v "$b" >/dev/null && exec "$b" --app="$URL"{linux_flags}; done; xdg-open "$URL"'
{icon_line}
Terminal=false
Categories=Network;WebBrowser;
"""
        # Install script — copies icon to standard location
        install_sh = f"""#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/share/icons"
mkdir -p "$HOME/.local/share/applications"
cp "$DIR/icon.png" "$HOME/.local/share/icons/{n}.png" 2>/dev/null
cp "$DIR/{n}.desktop" "$HOME/.local/share/applications/"
echo "✓ {n} 已安装到应用菜单"
"""
        tar_path = dl / "linux.tar.gz"
        with tarfile.open(tar_path, 'w:gz') as t:
            self._tar_add(t, f"{n}/{n}.desktop", desktop, 0o755)
            self._tar_add(t, f"{n}/install.sh", install_sh, 0o755)
            if icon_png:
                info = tarfile.TarInfo(name=f"{n}/icon.png")
                info.size = len(icon_png)
                info.mode = 0o644
                t.addfile(info, io.BytesIO(icon_png))

    # ===== iOS — .mobileconfig (optionally CMS-signed "苹果免签") =====
    def _build_ios(self, dl: Path, r: dict, icon_png, base_url: Optional[str] = None):
        """Emit a Web Clip profile.

        If `base_url` is provided, the Web Clip points at `{base_url}/a/{id}/launch`
        so the server can later swap the target URL without re-installing.
        Otherwise the Web Clip points directly at the recipe's URL.

        If a signing cert is configured, the final file is a DER-encoded CMS
        signature — iOS will show the signer's domain in place of the red
        "未签名" warning. Otherwise a plain XML profile is written (still installs
        fine on every iOS version; just shows "未签名").
        """
        uid1 = str(uuid.uuid5(uuid.NAMESPACE_URL, r['url'] + '.clip'))
        uid2 = str(uuid.uuid5(uuid.NAMESPACE_URL, r['url'] + '.profile'))

        # iOS stays on the lightweight launch route so the server only handles
        # the initial open and target hot-swap, not the full browsing session.
        web_clip_url = f"{base_url}/a/{r['id']}/launch" if base_url else r['url']
        # A home-screen Web Clip must be FullScreen to launch as a standalone
        # app (no Safari chrome / address bar). Without it, iOS treats the clip
        # as a plain Safari bookmark and taps open the browser — exactly the
        # "opens in the browser instead of a standalone window" bug users hit.
        # Standalone is the baseline "app" behavior, so it is always on here and
        # is NOT tied to the Android-only "immersive" toggle.
        full_screen_tag = "<true/>"

        icon_tag = ""
        if icon_png:
            b64 = base64.b64encode(icon_png).decode()
            icon_tag = f"<key>Icon</key><data>{b64}</data>"

        mobileconfig = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>PayloadContent</key><array><dict>
<key>FullScreen</key>{full_screen_tag}
<key>IgnoreManifestScope</key><true/>
<key>IsRemovable</key><true/>
<key>Label</key><string>{r['name']}</string>
{icon_tag}
<key>PayloadDisplayName</key><string>{r['name']}</string>
<key>PayloadIdentifier</key><string>com.webtoapp.{r['id']}.clip</string>
<key>PayloadType</key><string>com.apple.webClip.managed</string>
<key>PayloadUUID</key><string>{uid1}</string>
<key>PayloadVersion</key><integer>1</integer>
<key>URL</key><string>{web_clip_url}</string>
</dict></array>
<key>PayloadDisplayName</key><string>{r['name']}</string>
<key>PayloadIdentifier</key><string>com.webtoapp.{r['id']}</string>
<key>PayloadRemovalDisallowed</key><false/>
<key>PayloadType</key><string>Configuration</string>
<key>PayloadUUID</key><string>{uid2}</string>
<key>PayloadVersion</key><integer>1</integer>
</dict></plist>""".encode("utf-8")

        signed_bytes, was_signed = mobileconfig_signer.sign_or_passthrough(mobileconfig)
        (dl / "ios.mobileconfig").write_bytes(signed_bytes)
        return {
            "signed": was_signed,
            "dynamic_url": bool(base_url),
            "web_clip_url": web_clip_url,
        }

    # ===== Android — APK or PWA package =====
    def _build_android(self, dl: Path, r: dict, icon_png, shell_url: str):
        builder = ApkBuilder()
        prefix = r.get("android_package_prefix") or config.android_package_prefix()
        pkg = f"{prefix}.a{r['id']}"
        apk_path = dl / "android.apk"
        zip_path = dl / "android.zip"

        if builder.build_apk(
            str(apk_path),
            shell_url,
            r['name'],
            pkg,
            icon_png,
            version_code=r.get("android_version_code", 1),
            version_name=r.get("android_version_name", "1.0"),
            feature_options=r.get("options") or {},
            app_id=r['id'],
        ):
            if zip_path.exists():
                zip_path.unlink()
            return {"apk": True, "fallback": False}

        builder.build_fallback(str(zip_path), shell_url, r['name'], icon_png, r['color'])
        return {"apk": False, "fallback": True}

    def _normalized_display_mode(self, display):
        raw = str(display or "").strip().lower()
        if raw in {"fullscreen", "standalone", "minimal-ui", "browser"}:
            return raw
        return "fullscreen"

    # ===== Helpers =====
    def _tar_add(self, t, name, content, mode=0o644):
        data = content.encode()
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = mode
        t.addfile(info, io.BytesIO(data))
