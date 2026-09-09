"""
WebToApp — Distillation Engine Server
FastAPI backend: site analysis, content distillation, app generation & download.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Optional

from server import config
from server import html_site
from server.engine.analyzer import SiteAnalyzer
from server.engine.distiller import Distiller
from server.engine.recipe import RecipeStore
from server.engine import mobileconfig_signer
from server.engine.cache import analysis_cache, html_cache, icon_cache
from server.engine.storage import r2_storage
from server.history_store import HistoryStore
from server.logging_util import log_event, setup_logging
from server.task_store import TaskStore

app = FastAPI(title="WebToApp Distillation Engine", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


_LONG_CACHE_PREFIXES = ("/css/", "/js/", "/assets/")
_LONG_CACHE_SUFFIXES = (".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".woff", ".woff2")

# Paths that must never be reachable through the catch-all StaticFiles mount,
# which is rooted at the whole project dir. Signing keystores, server source
# and cert material live under these — serving them would leak private keys.
# ``/generated`` holds per-app recipe.json files (which carry the secret
# edit_token); they must only be reached via the curated /a/{id}/... routes.
_BLOCKED_STATIC_PREFIXES = ("/certs", "/server", "/.git", "/deploy", "/generated")
# ``.env`` covers the deployed webtoapp.env (R2 credentials); ``.json`` is
# deliberately NOT listed — /a/{id}/manifest.json is a legitimate route, and
# sensitive .json files all live under blocked prefixes anyway.
_BLOCKED_STATIC_SUFFIXES = (".keystore", ".jks", ".pem", ".key", ".p12", ".pfx", ".env", ".bak", ".sqlite3", ".log")


def _normalize_request_path(raw_path: str) -> str:
    """Collapse duplicate slashes, strip trailing ones, lower-case.

    StaticFiles resolves '//server/x' on disk just like '/server/x', so the
    blocklist must judge the same normalized form — matching the raw string
    let doubled slashes evade every prefix rule (issue #18).
    """
    return re.sub(r"/{2,}", "/", str(raw_path or "").rstrip("/")).lower()


def _is_sensitive_path(raw_path: str) -> bool:
    normalized = _normalize_request_path(raw_path)
    return (
        any(normalized == p or normalized.startswith(p + "/") for p in _BLOCKED_STATIC_PREFIXES)
        or normalized.endswith(_BLOCKED_STATIC_SUFFIXES)
    )


@app.middleware("http")
async def block_sensitive_paths(request: Request, call_next):
    """Hard 404 for sensitive paths before they hit the static file mount.

    The frontend is served via ``StaticFiles(directory=ROOT)``, which would
    otherwise expose ``certs/`` (signing keystores + private keys) and the
    server source to anyone who guesses the path.

    We return a Response directly rather than raising HTTPException: user
    middleware runs outside Starlette's ExceptionMiddleware, so a raised
    HTTPException here would surface as a 500 instead of a 404.
    """
    if _is_sensitive_path(request.url.path):
        return PlainTextResponse("Not found", status_code=404)
    return await call_next(request)


@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    """Tag static-ish responses so Cloudflare (or any CDN) will cache them.

    We never override an upstream-supplied Cache-Control; this only fills in
    defaults for static assets and per-app icons.
    """
    response = await call_next(request)
    path = request.url.path
    existing = response.headers.get("cache-control")
    if existing:
        return response
    if path in ("/", "/index.html"):
        # Entry HTML must revalidate on every load. All frontend upgrades
        # ride on ?v= cache-busters *inside* this file, so a heuristically
        # cached copy strands users on stale UI talking to a new API
        # (all-zero stats, missing panels). StaticFiles emits ETag and
        # Last-Modified, so this costs a cheap 304, not a full download.
        response.headers["Cache-Control"] = "no-cache"
    elif path.startswith(_LONG_CACHE_PREFIXES) or path.endswith(_LONG_CACHE_SUFFIXES):
        # 1 day fresh, 7 days stale-while-revalidate. Hashed query strings
        # (?v=...) used in index.html keep these effectively immutable.
        response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    elif path.endswith("/icon.png") or path.endswith("/manifest.json"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response

ROOT = Path(__file__).parent.parent
APPS_DIR = ROOT / "generated"
APPS_DIR.mkdir(exist_ok=True)
history_store = HistoryStore(APPS_DIR / "_history.json")
task_store = TaskStore(APPS_DIR / "_tasks.sqlite3")
setup_logging()
_analyze_stats = {"total": 0, "cache_hits": 0, "total_ms": 0}
_analyze_stats_lock = threading.Lock()
_android_build_stats = {"apk_total": 0, "fallback_total": 0, "total_ms": 0, "count": 0}
_android_build_stats_lock = threading.Lock()
_recipe_cache = OrderedDict()
_recipe_cache_lock = threading.RLock()

analyzer = SiteAnalyzer()
distiller = Distiller()
recipes = RecipeStore()
# Shared async HTTP client used for outbound calls (Cloudflare cache purge etc).
http_client = httpx.AsyncClient(
    follow_redirects=False,
    timeout=30.0,
    headers={"User-Agent": "WebToApp/1.0 (+https://github.com/)"},
)
DISTILL_WORKER_COUNT = config.distill_worker_count()
RECIPE_CACHE_SIZE = config.recipe_cache_size()
DISTILL_TASK_TTL_SECONDS = 6 * 60 * 60
DISTILL_TASK_MAX_FINISHED = 256
APP_RETENTION_DAYS = 30
APP_RETENTION_SWEEP_INTERVAL_SECONDS = 60 * 60
RATE_LIMIT_BUCKET_TTL = 5 * 60  # forget IPs idle for this long


def _trusted_proxy_networks():
    networks = []
    for raw in config.trusted_proxy_cidrs():
        try:
            networks.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    return tuple(networks)


TRUSTED_PROXY_NETWORKS = _trusted_proxy_networks()

def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _retention_cutoff_iso() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=APP_RETENTION_DAYS)
    return cutoff.isoformat().replace("+00:00", "Z")


class IPRateLimiter:
    """In-memory sliding-window rate limiter, keyed by client IP.

    Cheap enough for a single-process FastAPI deploy. Buckets idle past
    `idle_ttl` are evicted on the fly so memory stays bounded even if the
    proxy is hammered by a botnet rotating IPs.
    """

    def __init__(self, max_requests: int, window_seconds: int, idle_ttl: int = 300):
        self.max_requests = int(max_requests or 0)
        self.window_seconds = int(window_seconds or 60)
        self.idle_ttl = int(idle_ttl or 300)
        self._buckets: Dict[str, list] = {}
        self._lock = asyncio.Lock()
        self._last_sweep = 0.0

    async def allow(self, key: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.time()
        cutoff = now - self.window_seconds
        async with self._lock:
            self._sweep_locked(now)
            timestamps = [t for t in self._buckets.get(key, []) if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self._buckets[key] = timestamps
                return False
            timestamps.append(now)
            self._buckets[key] = timestamps
            return True

    def _sweep_locked(self, now: float) -> None:
        if now - self._last_sweep < 30:
            return
        self._last_sweep = now
        idle_cutoff = now - self.idle_ttl
        expired = [key for key, ts in self._buckets.items() if not ts or ts[-1] < idle_cutoff]
        for key in expired:
            self._buckets.pop(key, None)


class DistillTaskQueue:
    def __init__(self, worker_count: int = 1, store: Optional[TaskStore] = None):
        self.worker_count = max(1, int(worker_count or 1))
        self._queue = asyncio.Queue()
        self._tasks: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._workers = []
        self._store = store
        self.completed_total = 0
        self.failed_total = 0

    def _persist(self, task: dict) -> None:
        if not self._store:
            return
        try:
            self._store.upsert(task)
        except Exception as exc:
            log_event("task_persist_failed", task_id=task.get("task_id"), error=str(exc))

    async def start(self) -> None:
        if self._workers:
            return
        if self._store:
            for task in self._store.list_resumable():
                task = deepcopy(task)
                task["status"] = "pending"
                task["stage"] = "queued"
                task["updated_at"] = _utc_now_iso()
                self._tasks[task["task_id"]] = task
                self._persist(task)
                await self._queue.put(task["task_id"])
                log_event("task_resumed", task_id=task["task_id"], app_id=task.get("app_id"))
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"distill-worker-{index}")
            for index in range(self.worker_count)
        ]

    async def stop(self) -> None:
        if not self._workers:
            return
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def submit(self, payload: dict) -> dict:
        now = _utc_now_iso()
        task_id = uuid.uuid4().hex
        # HTML uploads pass an explicit app_id (derived from content hash);
        # URL flows keep deriving it from url:name.
        app_id = payload.get("app_id") or hashlib.md5(
            f"{payload['url']}:{payload.get('name', '')}".encode()
        ).hexdigest()[:8]
        task = {
            "task_id": task_id,
            "app_id": app_id,
            "status": "pending",
            "stage": "queued",
            "stage_detail": {},
            "created_at": now,
            "updated_at": now,
            "payload": deepcopy(payload),
            "result": None,
            "error": None,
            "finished_at": None,
        }
        async with self._lock:
            self._prune_locked()
            self._tasks[task_id] = task
            self._persist(task)
        await self._queue.put(task_id)
        return {"task_id": task_id, "app_id": app_id, "status": "pending"}

    async def get(self, task_id: str) -> Optional[dict]:
        async with self._lock:
            self._prune_locked()
            task = self._tasks.get(task_id)
            if task:
                return deepcopy(task)
        if self._store:
            stored = self._store.get(task_id)
            if stored:
                return stored
        return None

    async def set_progress(self, task_id: str, stage: str, detail: Optional[dict] = None) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.get("status") not in {"pending", "running"}:
                return
            task["status"] = "running"
            task["stage"] = stage
            task["stage_detail"] = detail or {}
            task["updated_at"] = _utc_now_iso()
            # Progress is transient UI state: the in-memory task (what the
            # polling endpoint reads) is always updated, but SQLite only
            # sees stage transitions. Persisting the per-platform "done"
            # ticks too produced 5+ fsyncs per build that bought nothing —
            # crash recovery only needs the task back in "pending".
            if stage.startswith("platform_") and stage != "platform_error":
                return
            self._persist(task)

    async def _worker_loop(self, _index: int) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                if task_id is None:
                    return
                payload = None
                async with self._lock:
                    task = self._tasks.get(task_id)
                    if not task:
                        continue
                    task["status"] = "running"
                    task["stage"] = "starting"
                    task["updated_at"] = _utc_now_iso()
                    self._persist(task)
                    payload = deepcopy(task["payload"])
                payload = dict(payload or {})
                payload["_task_id"] = task_id
                loop = asyncio.get_running_loop()
                try:
                    result = await asyncio.to_thread(_run_distill_job, payload, self, loop)
                except Exception as exc:
                    log_event("distill_failed", task_id=task_id, error=str(exc))
                    async with self._lock:
                        task = self._tasks.get(task_id)
                        if task:
                            task["status"] = "error"
                            task["stage"] = "error"
                            task["error"] = str(exc)
                            task["updated_at"] = _utc_now_iso()
                            task["finished_at"] = time.time()
                            self.failed_total += 1
                            self._persist(task)
                else:
                    async with self._lock:
                        task = self._tasks.get(task_id)
                        if task:
                            task["status"] = "done"
                            task["stage"] = "done"
                            task["result"] = result
                            task["updated_at"] = _utc_now_iso()
                            task["finished_at"] = time.time()
                            self.completed_total += 1
                            self._persist(task)
                    log_event("distill_done", task_id=task_id, app_id=(result or {}).get("app_id"))
            finally:
                self._queue.task_done()

    def _prune_locked(self) -> None:
        now = time.time()
        expired = []
        finished = []
        for task_id, task in self._tasks.items():
            finished_at = task.get("finished_at")
            if finished_at:
                finished.append((finished_at, task_id))
                if now - float(finished_at) > DISTILL_TASK_TTL_SECONDS:
                    expired.append(task_id)
        for task_id in expired:
            self._tasks.pop(task_id, None)
            if self._store:
                try:
                    self._store.delete(task_id)
                except Exception:
                    pass
        if self._store:
            try:
                self._store.prune(DISTILL_TASK_TTL_SECONDS, DISTILL_TASK_MAX_FINISHED)
            except Exception:
                pass
        if len(finished) <= DISTILL_TASK_MAX_FINISHED:
            return
        finished.sort()
        for _finished_at, task_id in finished[:len(finished) - DISTILL_TASK_MAX_FINISHED]:
            self._tasks.pop(task_id, None)
            if self._store:
                try:
                    self._store.delete(task_id)
                except Exception:
                    pass


# --- Models ---
class AnalyzeRequest(BaseModel):
    url: str

class DistillRequest(BaseModel):
    url: str
    name: str = ""
    color: str = "#7c3aed"
    display: str = "fullscreen"
    orientation: str = "any"
    options: dict = {}


class HistoryImportPayload(BaseModel):
    version: int = 1
    items: List[dict] = []


class HistoryBulkDeletePayload(BaseModel):
    app_ids: List[str] = []


# --- API Routes ---
@app.post("/api/analyze")
async def analyze_url(req: AnalyzeRequest):
    try:
        result = await analyzer.analyze(str(req.url))
        with _analyze_stats_lock:
            _analyze_stats["total"] += 1
            if result.get("cacheHit"):
                _analyze_stats["cache_hits"] += 1
            _analyze_stats["total_ms"] += int(result.get("durationMs") or 0)
        log_event(
            "analyze_done",
            url=str(req.url),
            cache_hit=bool(result.get("cacheHit")),
            duration_ms=result.get("durationMs"),
            host=result.get("host"),
        )
        return result
    except Exception as e:
        log_event("analyze_failed", url=str(req.url), error=str(e))
        raise HTTPException(status_code=422, detail=str(e))


def _resolve_base_url(request: Request) -> str:
    configured = config.public_base_url()
    if configured:
        return configured
    if _request_is_from_trusted_proxy(request):
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    else:
        scheme = request.url.scheme
        host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


def _public_recipe(recipe: dict) -> dict:
    safe = dict(recipe)
    safe.pop("_custom_icon_data_url", None)
    safe.pop("edit_token", None)
    return safe


def _device_fingerprint(request: Request) -> Optional[str]:
    raw = str(request.cookies.get("webtoapp_device_fingerprint", "") or "").strip()
    if not raw:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._:-]", "", raw)[:160]
    return cleaned or None


def _history_payload(request: Request) -> dict:
    items = history_store.list_history(_device_fingerprint(request), APPS_DIR)
    return {"items": items}


def _html_site_public_base(base_url: str) -> str:
    """Origin that uploaded-site content should live on.

    SITE_PUBLIC_BASE_URL wins over the request-derived origin when configured,
    so uploaded pages execute on an isolated host instead of the API origin
    (issue #20). Empty config keeps the legacy main-origin behaviour.
    """
    return config.site_public_base_url() or str(base_url or "").rstrip("/")


def _import_recipe_from_payload(item: dict, base_url: str = "") -> dict:
    snapshot = deepcopy(item.get("snapshot") or {})
    recipe = deepcopy(item.get("recipe") or snapshot.get("recipe") or {})
    app_id = str(item.get("app_id") or snapshot.get("app_id") or recipe.get("id") or "").strip()
    source_type = recipe.get("source_type") or snapshot.get("source_type") or "url"
    if source_type == "html":
        # The stored URL embeds the exporting server's base URL; re-derive it
        # from the importing server so artifacts point at the right host.
        target_url = f"{_html_site_public_base(base_url)}/a/{app_id}/site/{html_site.INDEX_NAME}"
    else:
        target_url = str(recipe.get("url") or snapshot.get("target_url") or "").strip()
    if not app_id or not target_url:
        raise ValueError("invalid history item")
    normalized = {
        "id": app_id,
        "url": target_url,
        "source_type": source_type,
        "name": recipe.get("name") or snapshot.get("name") or app_id,
        "color": recipe.get("color") or snapshot.get("color") or "#7c3aed",
        "display": recipe.get("display") or snapshot.get("display") or "fullscreen",
        "orientation": recipe.get("orientation") or snapshot.get("orientation") or "any",
        "android_version_code": recipe.get("android_version_code") or snapshot.get("android_version_code") or 1,
        "android_version_name": recipe.get("android_version_name") or snapshot.get("android_version_name") or "1.0.0",
        "android_package_prefix": recipe.get("android_package_prefix") or snapshot.get("android_package_prefix") or config.android_package_prefix(),
        "custom_icon_uploaded": bool(item.get("icon_data_url") or recipe.get("custom_icon_uploaded") or snapshot.get("custom_icon_uploaded")),
        "options": recipe.get("options") or {},
    }
    if source_type == "html":
        normalized["content_hash"] = recipe.get("content_hash") or snapshot.get("content_hash") or ""
    icon_data_url = str(item.get("icon_data_url") or "").strip()
    if icon_data_url:
        normalized["_custom_icon_data_url"] = icon_data_url
    return normalized


def _build_distill_response(payload: dict, progress_cb=None) -> dict:
    source_type = payload.get("source_type") or "url"
    app_id = payload.get("app_id") or hashlib.md5(
        f"{payload['url']}:{payload.get('name', '')}".encode()
    ).hexdigest()[:8]
    if source_type == "html":
        # The "site" is hosted by this server; the recipe URL points at the
        # staged content so every platform artifact reuses the URL pipeline.
        base_url = _html_site_public_base(payload.get("base_url"))
        site_index = payload.get("site_index") or html_site.INDEX_NAME
        target_url = f"{base_url}/a/{app_id}/site/{site_index}"
    else:
        target_url = str(payload["url"])
    recipe = distiller.create_recipe(
        app_id=app_id,
        url=target_url,
        name=payload.get("name") or "",
        color=payload.get("color") or "#7c3aed",
        display=payload.get("display") or "fullscreen",
        orientation=payload.get("orientation") or "any",
        options=payload.get("options") or {},
        source_type=source_type,
        content_hash=payload.get("content_hash"),
    )
    app_dir = APPS_DIR / app_id
    base_url = payload.get("base_url") or ""
    build_meta = distiller.write_app_files(
        app_dir,
        recipe,
        base_url=base_url,
        progress_cb=progress_cb,
    )
    history_store.record_build(
        payload.get("device_fingerprint"),
        recipe,
        f"/a/{app_id}",
        build_meta.get("runtime_url"),
    )
    return {
        "app_id": app_id,
        "url": f"/a/{app_id}",
        "recipe": _public_recipe(recipe),
        "edit_token": recipe.get("edit_token"),
        "ios": build_meta.get("ios", {}),
        "android": build_meta.get("android", {}),
        "runtime_url": build_meta.get("runtime_url"),
        "platform_errors": build_meta.get("platform_errors") or {},
        "signing_available": mobileconfig_signer.can_sign(),
    }


def _run_distill_job(payload: dict, queue: Optional["DistillTaskQueue"] = None, loop=None) -> dict:
    task_id = payload.get("_task_id")
    started = time.perf_counter()

    def progress_cb(stage: str, detail=None):
        if queue is None or not task_id or loop is None:
            return
        # Fire-and-forget: the build thread must never wait on the event loop
        # (set_progress persists to SQLite behind an asyncio.Lock). The old
        # fut.result(timeout=2) handoff added a context switch per progress
        # tick and, under lock contention, stalled real build work for up to
        # 2 s per tick. Ordering loss is fine — every tick overwrites the
        # same task row and the frontend polls the latest state anyway.
        try:
            asyncio.run_coroutine_threadsafe(queue.set_progress(task_id, stage, detail), loop)
        except Exception:
            pass

    result = _build_distill_response(payload, progress_cb=progress_cb)
    duration_ms = int((time.perf_counter() - started) * 1000)
    result["duration_ms"] = duration_ms
    android_meta = result.get("android") or {}
    with _android_build_stats_lock:
        _android_build_stats["count"] += 1
        _android_build_stats["total_ms"] += duration_ms
        if android_meta.get("apk"):
            _android_build_stats["apk_total"] += 1
        elif android_meta.get("fallback"):
            _android_build_stats["fallback_total"] += 1
    log_event(
        "distill_timing",
        task_id=task_id,
        app_id=result.get("app_id"),
        duration_ms=duration_ms,
        android=android_meta,
    )
    return result


distill_queue = DistillTaskQueue(worker_count=DISTILL_WORKER_COUNT, store=task_store)
# Cheap anti-abuse safety net for the build endpoint: ~10 submissions / minute
# per source IP. The real quota lives on device fingerprint (see config.daily_build_quota_per_device).
distill_rate_limiter = IPRateLimiter(max_requests=10, window_seconds=60, idle_ttl=RATE_LIMIT_BUCKET_TTL)
retention_task = None


def _client_ip(request: Request) -> str:
    if _request_is_from_trusted_proxy(request):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = _normalized_ip(forwarded.split(",", 1)[0].strip())
            if first:
                return first
        real_ip = _normalized_ip(request.headers.get("x-real-ip", "").strip())
        if real_ip:
            return real_ip
    client = request.client
    direct_ip = _normalized_ip(client.host if client else "")
    return direct_ip or "unknown"


def _normalized_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return ""


def _request_is_from_trusted_proxy(request: Request) -> bool:
    client = request.client
    client_ip = _normalized_ip(client.host if client else "")
    if not client_ip:
        return False
    parsed = ipaddress.ip_address(client_ip)
    return any(parsed in network for network in TRUSTED_PROXY_NETWORKS)



def _keystore_dir() -> Path:
    return Path(config.android_keystore_dir())


def _keystore_stats() -> dict:
    root = _keystore_dir()
    if not root.exists():
        return {"dir": str(root), "count": 0, "bytes": 0}
    count = 0
    total = 0
    for path in root.glob("*.keystore"):
        count += 1
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return {"dir": str(root), "count": count, "bytes": total}


def _purge_keystores_for_apps(app_ids) -> int:
    root = _keystore_dir()
    if not root.exists():
        return 0
    removed = 0
    for app_id in app_ids:
        safe_id = re.sub(r"[^a-z0-9_]", "", str(app_id or "").lower()) or "app"
        for path in (root / f"{safe_id}.keystore", root / f"{safe_id}.json"):
            try:
                if path.exists():
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def _purge_orphan_keystores() -> int:
    root = _keystore_dir()
    if not root.exists():
        return 0
    live_ids = set()
    for recipe_path in APPS_DIR.glob("*/recipe.json"):
        live_ids.add(recipe_path.parent.name)
    removed = 0
    for keystore in root.glob("*.keystore"):
        app_id = keystore.stem
        if app_id in live_ids:
            continue
        meta = root / f"{app_id}.json"
        try:
            keystore.unlink()
            removed += 1
        except OSError:
            pass
        try:
            if meta.exists():
                meta.unlink()
                removed += 1
        except OSError:
            pass
    return removed

def _purge_expired_generated_apps() -> int:
    expired = history_store.list_expired_apps(_retention_cutoff_iso())
    if not expired:
        return 0
    removed_ids = []
    for item in expired:
        app_id = item["app_id"]
        app_dir = APPS_DIR / app_id
        try:
            if app_dir.exists():
                shutil.rmtree(app_dir)
            removed_ids.append(app_id)
        except Exception:
            continue
    if not removed_ids:
        return 0
    history_store.purge_apps(removed_ids)
    with _recipe_cache_lock:
        for app_id in removed_ids:
            _recipe_cache.pop(app_id, None)
    keystore_removed = _purge_keystores_for_apps(removed_ids)
    orphan_removed = _purge_orphan_keystores()
    if r2_storage.configured:
        for app_id in removed_ids:
            try:
                r2_storage.delete_app(app_id)
            except Exception as exc:  # noqa: BLE001
                log_event("retention_r2_failed", app_id=app_id, error=str(exc))
    log_event(
        "retention_purged",
        count=len(removed_ids),
        app_ids=removed_ids[:10],
        keystores_removed=keystore_removed,
        orphan_keystores_removed=orphan_removed,
    )
    return len(removed_ids)


async def _retention_loop() -> None:
    while True:
        try:
            await asyncio.sleep(APP_RETENTION_SWEEP_INTERVAL_SECONDS)
            await asyncio.to_thread(_purge_expired_generated_apps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event("retention_sweep_failed", error=str(exc))


@app.on_event("startup")
async def _startup_services():
    global retention_task
    await asyncio.to_thread(_purge_expired_generated_apps)
    await distill_queue.start()
    retention_task = asyncio.create_task(_retention_loop(), name="app-retention-sweeper")


def _enforce_daily_quota(device_fingerprint: Optional[str]) -> None:
    quota = config.daily_build_quota_per_device()
    if quota <= 0 or not device_fingerprint:
        return
    since_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    used = history_store.count_recent_builds(device_fingerprint, since_iso)
    if used >= quota:
        raise HTTPException(
            status_code=429,
            detail=f"已达每日生成上限（{used}/{quota}），24 小时后自动恢复。",
        )


@app.post("/api/distill")
async def distill_app(req: DistillRequest, request: Request):
    if not await distill_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="提交太频繁，请稍后再试。")
    device_fingerprint = _device_fingerprint(request)
    _enforce_daily_quota(device_fingerprint)
    task = await distill_queue.submit(
        {
            "url": str(req.url),
            "name": req.name,
            "color": req.color,
            "display": req.display,
            "orientation": req.orientation,
            "options": req.options or {},
            "base_url": _resolve_base_url(request),
            "device_fingerprint": device_fingerprint,
        }
    )
    log_event(
        "distill_submitted",
        task_id=task.get("task_id"),
        app_id=task.get("app_id"),
        url=str(req.url),
        client_ip=_client_ip(request),
    )
    return JSONResponse(task, status_code=202)


@app.get("/api/distill/{task_id}")
async def distill_task_status(task_id: str):
    task = await distill_queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] == "done":
        return dict(task["result"] or {})
    if task["status"] == "error":
        raise HTTPException(500, task.get("error") or "生成失败")
    detail = task.get("stage_detail") or {}
    platforms_done = detail.get("done")
    platforms_total = detail.get("total")
    payload = {
        "task_id": task["task_id"],
        "app_id": task["app_id"],
        "status": task["status"],
        "stage": task.get("stage") or task["status"],
        "stage_detail": detail,
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }
    if platforms_total:
        payload["progress"] = {
            "done": int(platforms_done or 0),
            "total": int(platforms_total or 0),
            "platform": detail.get("platform"),
            "platform_status": detail.get("platform_status") or {},
        }
    return payload


class UpdateUrlRequest(BaseModel):
    url: str
    edit_token: Optional[str] = None


# --- HTML-to-App uploads ---
async def _read_upload_capped(file: UploadFile) -> bytes:
    cap = config.html_upload_max_bytes()
    data = await file.read(cap + 1)
    if len(data) > cap:
        raise HTTPException(
            status_code=413,
            detail={"code": "htmlUpload.tooLarge", "message": "Uploaded file is too large"},
        )
    if not data:
        raise HTTPException(
            status_code=400,
            detail={"code": "htmlUpload.invalid", "message": "Uploaded file is empty"},
        )
    return data


def _analyze_html_bytes(data: bytes, filename: str) -> dict:
    """Local-only analysis of an uploaded HTML app: title, theme color and a
    favicon preview. Stages into a throwaway temp dir; nothing persists."""
    with tempfile.TemporaryDirectory() as tmp:
        staged_dir = Path(tmp) / "site"
        staged = html_site.validate_and_extract(data, filename, staged_dir)
        meta = html_site.extract_site_meta(staged_dir)
    icon_data_url = None
    if meta.get("icon_png"):
        icon_data_url = "data:image/png;base64," + base64.b64encode(meta["icon_png"]).decode("ascii")
    return {
        "sourceType": "html",
        "name": meta.get("title") or "",
        "color": meta.get("theme_color") or "",
        "iconDataUrl": icon_data_url,
        "fileCount": staged["file_count"],
        "totalBytes": staged["total_bytes"],
    }


@app.post("/api/analyze/html")
async def analyze_html_upload(request: Request, file: UploadFile = File(...)):
    if not await distill_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="提交太频繁，请稍后再试。")
    data = await _read_upload_capped(file)
    try:
        result = await asyncio.to_thread(_analyze_html_bytes, data, file.filename)
    except html_site.HtmlUploadError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "htmlUpload.invalid", "message": str(exc)},
        )
    log_event(
        "analyze_html_done",
        filename=str(file.filename or ""),
        name=result.get("name"),
        file_count=result.get("fileCount"),
    )
    return result


@app.post("/api/distill/html")
async def distill_html_app(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(""),
    color: str = Form("#7c3aed"),
    display: str = Form("fullscreen"),
    orientation: str = Form("any"),
    options: str = Form(""),
):
    """Build an app from uploaded HTML content (single .html or .zip bundle).

    Mirrors /api/distill: same rate limits and per-device daily quota, same
    202 + task polling contract. The content is staged under
    ``generated/<app_id>/site/`` before the task is enqueued."""
    if not await distill_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="提交太频繁，请稍后再试。")
    device_fingerprint = _device_fingerprint(request)
    _enforce_daily_quota(device_fingerprint)
    try:
        parsed_options = json.loads(options) if str(options).strip() else {}
        if not isinstance(parsed_options, dict):
            raise ValueError("options must be a JSON object")
    except ValueError as exc:
        raise HTTPException(422, "options must be a JSON object") from exc
    data = await _read_upload_capped(file)
    try:
        staged = await asyncio.to_thread(html_site.stage_html_app, data, file.filename, name, APPS_DIR)
    except html_site.HtmlUploadError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "htmlUpload.invalid", "message": str(exc)},
        )
    task = await distill_queue.submit(
        {
            "source_type": "html",
            "app_id": staged["app_id"],
            "content_hash": staged["content_hash"],
            "site_index": staged["index_name"],
            "name": staged["name"],
            "color": color,
            "display": display,
            "orientation": orientation,
            "options": parsed_options,
            "base_url": _resolve_base_url(request),
            "device_fingerprint": device_fingerprint,
        }
    )
    log_event(
        "distill_html_submitted",
        task_id=task.get("task_id"),
        app_id=task.get("app_id"),
        filename=str(file.filename or ""),
        client_ip=_client_ip(request),
    )
    return JSONResponse(task, status_code=202)


@app.patch("/api/app/{app_id}/url")
async def update_app_url(app_id: str, body: UpdateUrlRequest, request: Request):
    """Hot-swap the target URL of an already-installed Web Clip.
    Users don't need to reinstall — their Web Clip still points at our /launch
    endpoint, which now redirects to the new URL.

    Authorization: the caller must present the app's ``edit_token`` (returned
    only to the creator in the /api/distill response). Without it, anyone who
    guessed the 8-char app_id could redirect a victim's installed app to a
    phishing site, so a missing/incorrect token is rejected."""
    recipe_path = APPS_DIR / app_id / "recipe.json"
    if not recipe_path.exists():
        raise HTTPException(404, "App not found")
    recipe = json.loads(recipe_path.read_text())

    if recipe.get("source_type") == "html":
        # HTML apps bundle their content on this server; repointing the URL
        # would orphan the hosted site bundle.
        raise HTTPException(409, "URL hot-swap is not supported for HTML apps; rebuild with new content instead")

    expected = str(recipe.get("edit_token") or "")
    supplied = str(body.edit_token or request.headers.get("x-edit-token", "") or "")
    # Constant-time compare; reject if the app has no token or the token is wrong.
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(403, "Invalid or missing edit token")

    new_url = str(body.url or "").strip()
    parsed = urlparse(new_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(422, "URL must be an absolute http(s) URL")

    old_url = recipe.get("url")
    recipe["url"] = new_url
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2))
    history_store.update_recipe(recipe, public_path=f"/a/{app_id}", runtime_url=recipe.get("url"))
    distiller._write_download_page(APPS_DIR / app_id, recipe)
    purge_result = await _purge_launch_cache(app_id)
    return {
        "app_id": app_id,
        "old_url": old_url,
        "new_url": new_url,
        "cache_purged": purge_result,
    }


async def _purge_launch_cache(app_id: str) -> Optional[bool]:
    """Eagerly evict the ``/launch`` redirect from the Cloudflare edge cache.

    Returns ``True`` on success, ``False`` on failure, ``None`` when no
    Cloudflare API credentials are configured (the caller can rely on the
    short TTL instead).
    """
    if not config.cloudflare_purge_available():
        return None
    base = config.public_base_url()
    if not base:
        return None
    target = f"{base.rstrip('/')}/a/{app_id}/launch"
    try:
        resp = await http_client.post(
            f"https://api.cloudflare.com/client/v4/zones/{config.cloudflare_zone_id()}/purge_cache",
            headers={
                "Authorization": f"Bearer {config.cloudflare_api_token()}",
                "Content-Type": "application/json",
            },
            json={"files": [target]},
            timeout=8.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[CF Purge] {app_id}: {exc}")
        return False
    if resp.status_code >= 400:
        print(f"[CF Purge] {app_id}: HTTP {resp.status_code} {resp.text[:200]}")
        return False
    return True



@app.get("/healthz")
async def healthz():
    return {"ok": True, "status": "healthy"}


@app.get("/readyz")
async def readyz():
    apps_writable = False
    try:
        APPS_DIR.mkdir(exist_ok=True)
        probe = APPS_DIR / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        apps_writable = True
        try:
            probe.unlink()
        except Exception:
            pass
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "status": "not_ready", "error": str(exc)},
            status_code=503,
        )
    return {
        "ok": True,
        "status": "ready",
        "apps_writable": apps_writable,
        "workers": DISTILL_WORKER_COUNT,
        "build_parallelism": config.build_parallelism(),
    }


@app.get("/api/metrics")
async def metrics(request: Request):
    """Ops metrics. Not public: gated by METRICS_TOKEN when configured,
    otherwise restricted to loopback callers (ops curl on the host itself).
    The response discloses filesystem paths, queue depths and keystore
    stats — fine for the operator, nothing a stranger needs."""
    token = config.metrics_token()
    if token:
        supplied = (
            request.headers.get("x-metrics-token", "").strip()
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(403, "Forbidden")
    elif _client_ip(request) not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Forbidden")
    apk_builder_ready = False
    try:
        from server.engine.apk_builder import ApkBuilder
        apk_builder_ready = bool(ApkBuilder().can_build_apk)
    except Exception:
        apk_builder_ready = False
    return {
        "distill": {
            "workers": DISTILL_WORKER_COUNT,
            "completed_total": distill_queue.completed_total,
            "failed_total": distill_queue.failed_total,
            "queue_size": distill_queue._queue.qsize(),
            "in_memory_tasks": len(distill_queue._tasks),
        },
        "caches": {
            "html": html_cache.stats(),
            "icon": icon_cache.stats(),
            "analysis": analysis_cache.stats(),
            "recipe": {"size": len(_recipe_cache), "max_size": RECIPE_CACHE_SIZE},
        },
        "history": history_store.stats() if hasattr(history_store, "stats") else {},
        "tasks": task_store.stats() if hasattr(task_store, "stats") else {},
        "features": {
            "ios_signing": mobileconfig_signer.can_sign(),
            "android_apk": apk_builder_ready,
            "r2": r2_storage.configured,
        },
        "build_parallelism": config.build_parallelism(),
        "analyze": {
            "total": _analyze_stats["total"],
            "cache_hits": _analyze_stats["cache_hits"],
            "avg_ms": int(_analyze_stats["total_ms"] / _analyze_stats["total"]) if _analyze_stats["total"] else 0,
        },
        "android_builds": {
            "apk_total": _android_build_stats["apk_total"],
            "fallback_total": _android_build_stats["fallback_total"],
            "avg_ms": int(_android_build_stats["total_ms"] / _android_build_stats["count"]) if _android_build_stats["count"] else 0,
            "count": _android_build_stats["count"],
        },
        "keystores": _keystore_stats(),
    }


@app.get("/api/recipes/popular")
async def popular_recipes():
    return recipes.get_popular()


@app.get("/api/stats")
async def homepage_stats():
    app_count = sum(1 for _ in APPS_DIR.glob("*/recipe.json"))
    traffic = history_store.traffic_totals()
    return {
        "generatedApps": app_count,
        "downloads": traffic["downloads"],
        "views": traffic["views"],
    }


@app.get("/api/history")
async def history_index(request: Request):
    return _history_payload(request)


@app.post("/api/history/attach/{app_id}")
async def attach_history_item(app_id: str, request: Request):
    recipe_path = APPS_DIR / app_id / "recipe.json"
    if not recipe_path.exists():
        raise HTTPException(404, "App not found")
    device_fingerprint = _device_fingerprint(request)
    if not device_fingerprint:
        return {"attached": False, "reason": "missing_device_fingerprint"}
    history_store.attach_app(device_fingerprint, app_id)
    return {"attached": True, "app_id": app_id, "history": _history_payload(request)}


def _decode_site_files(item: dict):
    """Decode the base64 site bundle embedded in an exported history item."""
    raw_files = (item.get("site_files") or [])
    files = []
    for entry in raw_files:
        try:
            files.append({"name": str(entry.get("name") or ""), "data": base64.b64decode(entry.get("data") or "")})
        except Exception:
            return None
    return files or None


@app.get("/api/history/export")
async def export_history(request: Request):
    payload = history_store.export_history(_device_fingerprint(request), APPS_DIR)
    export_cap = config.html_export_max_bytes()
    for item in payload.get("items") or []:
        recipe = item.get("recipe") or {}
        if recipe.get("source_type") != "html":
            continue
        site_dir = APPS_DIR / str(item.get("app_id") or "") / "site"
        if not site_dir.is_dir():
            item["site_files_skipped"] = True
            continue
        files = html_site.iter_site_files(site_dir)
        total = sum(len(f["data"]) for f in files)
        if total > export_cap:
            item["site_files_skipped"] = True
            continue
        item["site_files"] = [
            {"name": f["name"], "data": base64.b64encode(f["data"]).decode("ascii")} for f in files
        ]
    return payload


@app.post("/api/history/import")
async def import_history(payload: HistoryImportPayload, request: Request):
    device_fingerprint = _device_fingerprint(request)
    if not device_fingerprint:
        raise HTTPException(400, "Missing device fingerprint")
    base_url = _resolve_base_url(request)
    imported = 0
    restored = 0
    skipped = 0
    errors = []
    for item in payload.items:
        try:
            recipe = _import_recipe_from_payload(item, base_url)
            app_id = recipe["id"]
            app_dir = APPS_DIR / app_id
            recipe_path = app_dir / "recipe.json"
            effective_recipe = recipe
            should_restore = not recipe_path.exists()
            if recipe_path.exists():
                # The app already exists on the server: its recipe.json is the
                # source of truth (and holds the secret edit_token). Never let
                # an imported snapshot overwrite it — otherwise anyone could
                # repoint someone else's installed app by importing a crafted
                # snapshot. Import only re-links the app to this device.
                try:
                    effective_recipe = json.loads(recipe_path.read_text())
                except Exception:
                    effective_recipe = recipe
            if should_restore:
                if recipe.get("source_type") == "html":
                    site_files = _decode_site_files(item)
                    if not site_files:
                        raise ValueError("HTML app snapshot carries no site bundle (skipped at export or corrupt)")
                    await asyncio.to_thread(html_site.restore_site_files, app_dir / "site", site_files)
                distiller.write_app_files(app_dir, recipe, base_url=base_url)
                effective_recipe = _load_recipe(app_id)
                restored += 1
            history_store.import_snapshot(
                device_fingerprint,
                item.get("snapshot") or {},
                effective_recipe,
                public_path=f"/a/{app_id}",
                runtime_url=effective_recipe.get("url"),
            )
            imported += 1
        except Exception as exc:
            skipped += 1
            errors.append(str(exc))
    return {
        "imported": imported,
        "restored": restored,
        "skipped": skipped,
        "errors": errors[:5],
        "history": _history_payload(request),
    }


@app.delete("/api/history/{app_id}")
async def delete_history_item(app_id: str, request: Request):
    device_fingerprint = _device_fingerprint(request)
    if not device_fingerprint:
        raise HTTPException(400, "Missing device fingerprint")
    removed = history_store.remove_from_device(device_fingerprint, app_id)
    if not removed:
        raise HTTPException(404, "History item not found")
    return {"removed": True, "app_id": app_id, "history": _history_payload(request)}


HISTORY_BULK_DELETE_MAX = 500


@app.post("/api/history/delete-bulk")
def delete_history_bulk(payload: HistoryBulkDeletePayload, request: Request):
    # Sync endpoint on purpose: FastAPI runs it in the threadpool, so the
    # blocking SQLite work stays off the event loop while a large clean-up
    # removes every entry in one lock acquisition and one payload rebuild.
    device_fingerprint = _device_fingerprint(request)
    if not device_fingerprint:
        raise HTTPException(400, "Missing device fingerprint")
    app_ids = [app_id for app_id in payload.app_ids if app_id]
    if not app_ids:
        return {"removed": [], "history": _history_payload(request)}
    if len(app_ids) > HISTORY_BULK_DELETE_MAX:
        raise HTTPException(400, "Too many app ids")
    removed = history_store.remove_many_from_device(device_fingerprint, app_ids)
    return {"removed": removed, "history": _history_payload(request)}


@app.on_event("shutdown")
async def _shutdown_clients():
    global retention_task
    if retention_task is not None:
        retention_task.cancel()
        await asyncio.gather(retention_task, return_exceptions=True)
        retention_task = None
    await asyncio.to_thread(_purge_expired_generated_apps)
    await distill_queue.stop()
    history_store.flush()
    await http_client.aclose()


def _load_recipe(app_id: str) -> dict:
    recipe_path = APPS_DIR / app_id / "recipe.json"
    if not recipe_path.exists():
        raise HTTPException(404, "App not found")
    stat = recipe_path.stat()
    cache_key = (str(recipe_path), stat.st_mtime_ns, stat.st_size)
    with _recipe_cache_lock:
        cached = _recipe_cache.get(app_id)
        if cached and cached.get("key") == cache_key:
            _recipe_cache.move_to_end(app_id)
            return deepcopy(cached["value"])
    value = json.loads(recipe_path.read_text())
    with _recipe_cache_lock:
        _recipe_cache[app_id] = {"key": cache_key, "value": value}
        _recipe_cache.move_to_end(app_id)
        while len(_recipe_cache) > RECIPE_CACHE_SIZE:
            _recipe_cache.popitem(last=False)
    return deepcopy(value)


# --- App Serving ---
DOWNLOAD_TYPES = {
    "windows": ("windows.zip", "application/zip"),
    "macos": ("macos.zip", "application/zip"),
    "linux": ("linux.tar.gz", "application/gzip"),
    "android": ("android.apk", "application/vnd.android.package-archive"),
    "android_fallback": ("android.zip", "application/zip"),
    "ios": ("ios.mobileconfig", "application/x-apple-aspen-config"),
}


@app.get("/a/{app_id}")
async def serve_download_page(app_id: str, request: Request):
    """Serve the download landing page."""
    app_dir = APPS_DIR / app_id
    recipe_path = app_dir / "recipe.json"
    if not recipe_path.exists():
        raise HTTPException(404, "App not found")
    device_fingerprint = _device_fingerprint(request)
    if device_fingerprint:
        try:
            history_store.attach_app(device_fingerprint, app_id)
        except Exception:
            pass
    history_store.record_visit(app_id, "landing")
    page_path = app_dir / "page.html"
    if page_path.exists():
        try:
            page_ok = (
                page_path.stat().st_mtime_ns >= recipe_path.stat().st_mtime_ns
                and Distiller.DOWNLOAD_PAGE_MARKER in page_path.read_text(errors="ignore")[:256]
            )
        except Exception:
            page_ok = False
        if page_ok:
            return FileResponse(page_path, media_type="text/html; charset=utf-8")
    recipe = _load_recipe(app_id)
    distiller._write_download_page(app_dir, recipe)
    if page_path.exists():
        return FileResponse(page_path, media_type="text/html; charset=utf-8")
    return HTMLResponse(distiller.render_download_page(app_dir, recipe))


@app.get("/a/{app_id}/download/{platform}")
async def serve_download(app_id: str, platform: str):
    """Download a platform-specific app package.

    Preference order:
      1. Public CDN URL recorded in ``recipe.downloads_cdn`` (302 redirect).
      2. Local file in ``generated/<app_id>/downloads/`` (FileResponse).
    """
    if platform not in DOWNLOAD_TYPES:
        raise HTTPException(400, f"Unsupported platform: {platform}")

    filename, media_type = DOWNLOAD_TYPES[platform]
    filepath = APPS_DIR / app_id / "downloads" / filename

    # Android: try APK first, fall back to ZIP
    if platform == "android" and not filepath.exists():
        filename, media_type = DOWNLOAD_TYPES["android_fallback"]
        filepath = APPS_DIR / app_id / "downloads" / filename

    recipe_path = APPS_DIR / app_id / "recipe.json"
    recipe = {}
    if recipe_path.exists():
        try:
            recipe = json.loads(recipe_path.read_text())
        except Exception:
            recipe = {}

    cdn_url = (recipe.get("downloads_cdn") or {}).get(filename)
    if cdn_url:
        history_store.record_visit(app_id, f"download:{platform}")
        # 302 keeps the URL hot-swappable if we later re-upload under the same key.
        return RedirectResponse(cdn_url, status_code=302)

    if not filepath.exists():
        raise HTTPException(404, "Download not found")
    history_store.record_visit(app_id, f"download:{platform}")
    name = recipe.get("name") or app_id
    dl_name = f"{name}-{platform}.{filename.split('.', 1)[1]}"
    return FileResponse(filepath, media_type=media_type, filename=dl_name)


@app.get("/a/{app_id}/pwa")
async def serve_pwa(app_id: str):
    """Serve the PWA version (for Android install)."""
    pwa = APPS_DIR / app_id / "pwa.html"
    if not pwa.exists():
        raise HTTPException(404)
    html = pwa.read_text()
    history_store.record_visit(app_id, "pwa")
    return HTMLResponse(html)


@app.get("/a/{app_id}/manifest.json")
async def serve_manifest(app_id: str):
    manifest = APPS_DIR / app_id / "manifest.json"
    if not manifest.exists():
        raise HTTPException(404)
    return FileResponse(manifest, media_type="application/manifest+json")


@app.get("/a/{app_id}/site/{file_path:path}")
@app.get("/a/{app_id}/site")
async def serve_app_site(app_id: str, request: Request, file_path: str = ""):
    """Serve the hosted content of an HTML app.

    Path traversal is contained by resolve_site_file (resolved paths must stay
    inside ``generated/<app_id>/site``); directory requests fall back to
    index.html. Each hit records a visit so the 30-day retention sweep keeps
    actively-used apps alive.

    When SITE_PUBLIC_BASE_URL is configured, content is served from that
    isolated origin only: requests arriving on any other host (the API
    origin) are permanently redirected there — except calls from the site
    gateway itself, which proves itself with X-Site-Origin-Key. Uploaded
    pages must never execute same-origin with the API — hostile JS could
    otherwise invoke /api/* with the visitor's cookies attached (issue #20)."""
    site_base = config.site_public_base_url()
    if site_base:
        supplied_key = request.headers.get("x-site-origin-key", "")
        expected_key = config.site_origin_key()
        from_gateway = (
            bool(expected_key)
            and bool(supplied_key)
            and secrets.compare_digest(supplied_key, expected_key)
        )
        req_host = (request.headers.get("host") or "").lower()
        if not from_gateway and req_host and req_host != urlparse(site_base).netloc.lower():
            target = f"{site_base}/a/{app_id}/site" + (f"/{file_path}" if file_path else "")
            query = str(request.url.query)
            if query:
                target = f"{target}?{query}"
            return RedirectResponse(target, status_code=301)
    site_dir = APPS_DIR / app_id / "site"
    if not site_dir.is_dir():
        raise HTTPException(404, "Site not found")
    path = html_site.resolve_site_file(site_dir, file_path)
    if path is None:
        raise HTTPException(404, "Site file not found")
    history_store.record_visit(app_id, "site")
    return FileResponse(
        path,
        media_type=html_site.mime_for(path),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/a/{app_id}/sw.js")
async def serve_sw(app_id: str):
    sw = APPS_DIR / app_id / "sw.js"
    if not sw.exists():
        raise HTTPException(404)
    return FileResponse(sw, media_type="application/javascript")


@app.get("/a/{app_id}/icon.png")
async def serve_icon(app_id: str):
    """Serve the app's high-resolution icon (used by download page, PWA manifest, etc.)."""
    icon = APPS_DIR / app_id / "icon.png"
    if not icon.exists():
        raise HTTPException(404)
    return FileResponse(icon, media_type="image/png")


@app.api_route("/a/{app_id}/proxy", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@app.api_route("/a/{app_id}/proxy/{proxied_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy_app(app_id: str, proxied_path: str = ""):
    """Legacy reverse-proxy endpoint, fully removed.

    Earlier PWA shells iframed this route, which forced every target-site
    resource to transit our origin server. New builds load the target URL
    directly. Kept as a stable 410 so any cached/QR-shared link gets a
    deterministic response instead of a 404 chain or accidental revival.

    The 410 itself ships with a long Cache-Control so CDNs (Cloudflare etc.)
    will absorb scanner / botnet traffic at the edge without ever touching
    the origin. 24h is plenty — the route will never come back.
    """
    return JSONResponse(
        {"detail": "Proxy route permanently removed."},
        status_code=410,
        headers={
            "Cache-Control": "public, max-age=86400, s-maxage=86400",
            "CDN-Cache-Control": "public, max-age=86400",
        },
    )


@app.api_route("/a/{app_id}/launch", methods=["GET", "HEAD"])
async def launch_web_clip(app_id: str):
    """Dynamic launcher for installed iOS Web Clips.
    The mobileconfig's WebClip URL points here, so when the user taps the home
    screen icon, iOS opens this endpoint which 302-redirects to the recipe's
    current target. Update the target via PATCH /api/app/{id}/url and every
    subsequent tap loads the new URL — no re-install needed.

    Cached at the edge for ``LAUNCH_CACHE_MAX_AGE`` seconds (default 60s) so a
    swarm of taps doesn't all hit the origin. Hot-swaps via PATCH eagerly
    purge the cache when CLOUDFLARE_API_TOKEN is set; otherwise users see the
    new URL once the TTL expires.
    """
    recipe = _load_recipe(app_id)
    target = recipe.get("url")
    if not target:
        raise HTTPException(500, "Recipe has no target URL")
    history_store.record_visit(app_id, "launch")
    max_age = config.launch_cache_max_age()
    headers = {}
    if max_age > 0:
        # CDN-Cache-Control is honoured by Cloudflare/Fastly even when the
        # client-side Cache-Control would otherwise inhibit caching.
        headers["Cache-Control"] = f"public, max-age={max_age}, s-maxage={max_age}"
        headers["CDN-Cache-Control"] = f"public, max-age={max_age}"
    return RedirectResponse(target, status_code=302, headers=headers)


@app.api_route("/a/{app_id}/install", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_ios_install(app_id: str):
    """Legacy compatibility route.

    iOS install guidance now lives on the main download page, so keep this
    route only as a stable entrypoint for old shared links and QR codes.
    """
    recipe_path = APPS_DIR / app_id / "recipe.json"
    if not recipe_path.exists():
        raise HTTPException(404, "App not found")
    history_store.record_visit(app_id, "install")
    return RedirectResponse(url=f"/a/{app_id}", status_code=307)


# --- Static frontend ---
app.mount("/", StaticFiles(directory=str(ROOT), html=True), name="static")
