import base64
import json
import sqlite3
import threading
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(raw: Optional[str]) -> Optional[datetime]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class HistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._pending_visits = defaultdict(
            lambda: {
                "total": 0,
                "landing": 0,
                "install": 0,
                "pwa": 0,
                "launch": 0,
                "downloads": defaultdict(int),
                "last_visited_at": None,
            }
        )
        self._pending_limit = 64
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.suffix.lower() == ".json":
            self.db_path = self.path.with_suffix(".sqlite3")
            self.json_legacy_path = self.path
        else:
            self.db_path = self.path if self.path.suffix else Path(str(self.path) + ".sqlite3")
            self.json_legacy_path = self.path.with_suffix(".json") if self.path.suffix else Path(str(self.path) + ".json")
            if self.path.name == "_history":
                self.json_legacy_path = self.path.parent / "_history.json"
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self._migrate_json_if_needed()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS apps (
                    app_id TEXT PRIMARY KEY,
                    name TEXT,
                    target_url TEXT,
                    public_path TEXT,
                    runtime_url TEXT,
                    color TEXT,
                    recipe_json TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    app_ids_json TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS visits (
                    app_id TEXT PRIMARY KEY,
                    total INTEGER NOT NULL DEFAULT 0,
                    landing INTEGER NOT NULL DEFAULT 0,
                    install INTEGER NOT NULL DEFAULT 0,
                    pwa INTEGER NOT NULL DEFAULT 0,
                    launch INTEGER NOT NULL DEFAULT 0,
                    downloads_json TEXT NOT NULL DEFAULT '{}',
                    last_visited_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_apps_created_at ON apps(created_at);
                CREATE INDEX IF NOT EXISTS idx_visits_last_visited_at ON visits(last_visited_at);
                """
            )

    def _migrate_json_if_needed(self) -> None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", ("migrated_from_json",)).fetchone()
            if row:
                return
            legacy = self.json_legacy_path
            if not legacy.exists():
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ("migrated_from_json", _utc_now()),
                )
                return
            try:
                state = json.loads(legacy.read_text())
            except Exception:
                state = {}
            apps = state.get("apps") or {}
            devices = state.get("devices") or {}
            visits = state.get("visits") or {}
            for app_id, snapshot in apps.items():
                recipe = snapshot.get("recipe") or {}
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO apps(
                        app_id, name, target_url, public_path, runtime_url, color, recipe_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(app_id),
                        snapshot.get("name") or str(app_id),
                        snapshot.get("target_url") or "",
                        snapshot.get("public_path") or f"/a/{app_id}",
                        snapshot.get("runtime_url") or snapshot.get("target_url") or "",
                        snapshot.get("color") or "#7c3aed",
                        json.dumps(recipe, ensure_ascii=False),
                        snapshot.get("created_at") or _utc_now(),
                        snapshot.get("updated_at") or snapshot.get("created_at") or _utc_now(),
                    ),
                )
            for device_id, device in devices.items():
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO devices(device_id, app_ids_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(device_id),
                        json.dumps(list(device.get("app_ids") or []), ensure_ascii=False),
                        device.get("created_at") or _utc_now(),
                        device.get("updated_at") or _utc_now(),
                    ),
                )
            for app_id, stats in visits.items():
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO visits(
                        app_id, total, landing, install, pwa, launch, downloads_json, last_visited_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(app_id),
                        int(stats.get("total") or 0),
                        int(stats.get("landing") or 0),
                        int(stats.get("install") or 0),
                        int(stats.get("pwa") or 0),
                        int(stats.get("launch") or 0),
                        json.dumps(stats.get("downloads") or {}, ensure_ascii=False),
                        stats.get("last_visited_at"),
                    ),
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("migrated_from_json", _utc_now()),
            )
            try:
                backup = legacy.with_suffix(".json.bak")
                if backup.exists():
                    legacy.unlink(missing_ok=True)
                else:
                    legacy.replace(backup)
            except Exception:
                try:
                    legacy.unlink(missing_ok=True)
                except Exception:
                    pass

    def _visit_entry(self) -> dict:
        return {
            "total": 0,
            "landing": 0,
            "install": 0,
            "pwa": 0,
            "launch": 0,
            "downloads": {},
            "last_visited_at": None,
        }

    def _merge_visit_stats(self, existing: dict, imported: dict) -> dict:
        merged = self._visit_entry()
        merged["total"] = max(int(existing.get("total", 0)), int(imported.get("total", 0)))
        for key in ("landing", "install", "pwa", "launch"):
            merged[key] = max(int(existing.get(key, 0)), int(imported.get(key, 0)))
        existing_downloads = existing.get("downloads") or {}
        imported_downloads = imported.get("downloads") or {}
        downloads = {}
        for key in set(existing_downloads.keys()) | set(imported_downloads.keys()):
            downloads[key] = max(int(existing_downloads.get(key, 0)), int(imported_downloads.get(key, 0)))
        merged["downloads"] = downloads
        merged["last_visited_at"] = max(existing.get("last_visited_at") or "", imported.get("last_visited_at") or "") or None
        return merged

    def _snapshot_from_recipe(self, recipe: dict, public_path: str, runtime_url: Optional[str]) -> dict:
        safe_recipe = deepcopy(recipe)
        safe_recipe.pop("_custom_icon_data_url", None)
        safe_recipe.pop("edit_token", None)
        return {
            "app_id": recipe.get("id"),
            "name": recipe.get("name") or recipe.get("id"),
            "target_url": recipe.get("url") or "",
            "public_path": public_path,
            "runtime_url": runtime_url or recipe.get("url") or "",
            "color": recipe.get("color") or "#7c3aed",
            "recipe": safe_recipe,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }

    def _flush_visits_locked(self) -> None:
        if not self._pending_visits:
            return
        for app_id, delta in list(self._pending_visits.items()):
            row = self._conn.execute("SELECT * FROM visits WHERE app_id = ?", (app_id,)).fetchone()
            if row:
                downloads = json.loads(row["downloads_json"] or "{}")
                for platform, count in (delta.get("downloads") or {}).items():
                    downloads[platform] = int(downloads.get(platform, 0)) + int(count)
                self._conn.execute(
                    """
                    UPDATE visits SET
                        total = total + ?,
                        landing = landing + ?,
                        install = install + ?,
                        pwa = pwa + ?,
                        launch = launch + ?,
                        downloads_json = ?,
                        last_visited_at = COALESCE(?, last_visited_at)
                    WHERE app_id = ?
                    """,
                    (
                        int(delta.get("total", 0)),
                        int(delta.get("landing", 0)),
                        int(delta.get("install", 0)),
                        int(delta.get("pwa", 0)),
                        int(delta.get("launch", 0)),
                        json.dumps(downloads, ensure_ascii=False),
                        delta.get("last_visited_at"),
                        app_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO visits(app_id, total, landing, install, pwa, launch, downloads_json, last_visited_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        app_id,
                        int(delta.get("total", 0)),
                        int(delta.get("landing", 0)),
                        int(delta.get("install", 0)),
                        int(delta.get("pwa", 0)),
                        int(delta.get("launch", 0)),
                        json.dumps(dict(delta.get("downloads") or {}), ensure_ascii=False),
                        delta.get("last_visited_at"),
                    ),
                )
        self._pending_visits.clear()

    def flush(self) -> None:
        with self._lock:
            self._flush_visits_locked()

    def _get_device_app_ids_locked(self, device_id: str) -> List[str]:
        row = self._conn.execute("SELECT app_ids_json FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if not row:
            return []
        try:
            values = json.loads(row["app_ids_json"] or "[]")
        except Exception:
            values = []
        return [str(v) for v in values if str(v or "").strip()]

    def _set_device_app_ids_locked(self, device_id: str, app_ids: List[str], now: Optional[str] = None) -> None:
        now = now or _utc_now()
        row = self._conn.execute("SELECT created_at FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        created_at = row["created_at"] if row else now
        if not app_ids:
            self._conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO devices(device_id, app_ids_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (device_id, json.dumps(app_ids, ensure_ascii=False), created_at, now),
        )

    def _attach_app_locked(self, device_fingerprint: str, app_id: str, now: str) -> None:
        app_ids = self._get_device_app_ids_locked(device_fingerprint)
        if app_id in app_ids:
            app_ids = [app_id] + [x for x in app_ids if x != app_id]
        else:
            app_ids = [app_id] + app_ids
        self._set_device_app_ids_locked(device_fingerprint, app_ids, now)

    def record_build(self, device_fingerprint: Optional[str], recipe: dict, public_path: str, runtime_url: Optional[str]) -> None:
        snapshot = self._snapshot_from_recipe(recipe, public_path, runtime_url)
        app_id = str(snapshot.get("app_id") or "").strip()
        if not app_id:
            return
        now = _utc_now()
        with self._lock:
            self._flush_visits_locked()
            existing = self._conn.execute("SELECT created_at FROM apps WHERE app_id = ?", (app_id,)).fetchone()
            created_at = existing["created_at"] if existing else now
            self._conn.execute(
                """
                INSERT OR REPLACE INTO apps(
                    app_id, name, target_url, public_path, runtime_url, color, recipe_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    snapshot.get("name") or app_id,
                    snapshot.get("target_url") or "",
                    snapshot.get("public_path") or f"/a/{app_id}",
                    snapshot.get("runtime_url") or "",
                    snapshot.get("color") or "#7c3aed",
                    json.dumps(snapshot.get("recipe") or {}, ensure_ascii=False),
                    created_at,
                    now,
                ),
            )
            if device_fingerprint:
                self._attach_app_locked(device_fingerprint, app_id, now)

    def attach_app(self, device_fingerprint: Optional[str], app_id: str) -> None:
        if not device_fingerprint or not app_id:
            return
        with self._lock:
            self._flush_visits_locked()
            self._attach_app_locked(device_fingerprint, str(app_id), _utc_now())

    def update_recipe(self, recipe: dict, public_path: Optional[str] = None, runtime_url: Optional[str] = None) -> None:
        app_id = str((recipe or {}).get("id") or "").strip()
        if not app_id:
            return
        snapshot = self._snapshot_from_recipe(recipe, public_path or f"/a/{app_id}", runtime_url)
        now = _utc_now()
        with self._lock:
            self._flush_visits_locked()
            existing = self._conn.execute("SELECT created_at, public_path, runtime_url FROM apps WHERE app_id = ?", (app_id,)).fetchone()
            created_at = existing["created_at"] if existing else now
            pub = public_path or (existing["public_path"] if existing else f"/a/{app_id}")
            run = runtime_url if runtime_url is not None else (existing["runtime_url"] if existing else snapshot.get("runtime_url"))
            self._conn.execute(
                """
                INSERT OR REPLACE INTO apps(
                    app_id, name, target_url, public_path, runtime_url, color, recipe_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    snapshot.get("name") or app_id,
                    snapshot.get("target_url") or "",
                    pub,
                    run or "",
                    snapshot.get("color") or "#7c3aed",
                    json.dumps(snapshot.get("recipe") or {}, ensure_ascii=False),
                    created_at,
                    now,
                ),
            )

    def import_snapshot(
        self,
        device_fingerprint: Optional[str],
        snapshot: dict,
        recipe: dict,
        public_path: Optional[str] = None,
        runtime_url: Optional[str] = None,
    ) -> None:
        app_id = str((recipe or {}).get("id") or (snapshot or {}).get("app_id") or "").strip()
        if not app_id:
            return
        now = _utc_now()
        imported_visit_breakdown = (snapshot or {}).get("visit_breakdown") or {}
        imported_downloads = (snapshot or {}).get("download_breakdown") or {}
        imported_visits = {
            "total": int((snapshot or {}).get("total_activity_count", (snapshot or {}).get("visit_count", 0)) or 0),
            "landing": int(imported_visit_breakdown.get("landing", 0) or 0),
            "install": int(imported_visit_breakdown.get("install", 0) or 0),
            "pwa": int(imported_visit_breakdown.get("pwa", 0) or 0),
            "launch": int(imported_visit_breakdown.get("launch", 0) or 0),
            "downloads": {key: int(value or 0) for key, value in imported_downloads.items()},
            "last_visited_at": (snapshot or {}).get("last_visited_at"),
        }
        safe_recipe = deepcopy(recipe or {})
        safe_recipe.pop("_custom_icon_data_url", None)
        safe_recipe.pop("edit_token", None)
        with self._lock:
            self._flush_visits_locked()
            existing = self._conn.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
            created_at = (existing["created_at"] if existing else None) or (snapshot or {}).get("created_at") or now
            updated_at = max(
                (existing["updated_at"] if existing else "") or "",
                (snapshot or {}).get("updated_at") or "",
                now,
            )
            pub = public_path or (snapshot or {}).get("public_path") or (existing["public_path"] if existing else f"/a/{app_id}")
            run = (
                runtime_url
                if runtime_url is not None
                else ((snapshot or {}).get("runtime_url") or (existing["runtime_url"] if existing else "") or (recipe or {}).get("url") or "")
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO apps(
                    app_id, name, target_url, public_path, runtime_url, color, recipe_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    (snapshot or {}).get("name") or (recipe or {}).get("name") or (existing["name"] if existing else app_id),
                    (snapshot or {}).get("target_url") or (recipe or {}).get("url") or (existing["target_url"] if existing else ""),
                    pub,
                    run,
                    (snapshot or {}).get("color") or (recipe or {}).get("color") or (existing["color"] if existing else "#7c3aed"),
                    json.dumps(safe_recipe, ensure_ascii=False),
                    created_at,
                    updated_at,
                ),
            )
            row = self._conn.execute("SELECT * FROM visits WHERE app_id = ?", (app_id,)).fetchone()
            existing_stats = self._visit_row_to_stats(row)
            merged = self._merge_visit_stats(existing_stats, imported_visits)
            self._conn.execute(
                """
                INSERT OR REPLACE INTO visits(
                    app_id, total, landing, install, pwa, launch, downloads_json, last_visited_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    merged["total"],
                    merged["landing"],
                    merged["install"],
                    merged["pwa"],
                    merged["launch"],
                    json.dumps(merged["downloads"], ensure_ascii=False),
                    merged["last_visited_at"],
                ),
            )
            if device_fingerprint:
                self._attach_app_locked(device_fingerprint, app_id, now)

    def record_visit(self, app_id: str, channel: str) -> None:
        if not app_id:
            return
        channel = str(channel or "").strip().lower()
        with self._lock:
            delta = self._pending_visits[app_id]
            delta["total"] += 1
            if channel in {"landing", "install", "pwa", "launch"}:
                delta[channel] += 1
            elif channel.startswith("download:"):
                platform = channel.split(":", 1)[1] or "unknown"
                delta["downloads"][platform] += 1
            delta["last_visited_at"] = _utc_now()
            pending_total = sum(int(v.get("total", 0)) for v in self._pending_visits.values())
            if pending_total >= self._pending_limit:
                self._flush_visits_locked()

    def _app_row_to_snapshot(self, row) -> dict:
        try:
            recipe = json.loads(row["recipe_json"] or "{}")
        except Exception:
            recipe = {}
        return {
            "app_id": row["app_id"],
            "name": row["name"] or row["app_id"],
            "target_url": row["target_url"] or "",
            "public_path": row["public_path"] or f"/a/{row['app_id']}",
            "runtime_url": row["runtime_url"] or row["target_url"] or "",
            "color": row["color"] or "#7c3aed",
            "recipe": recipe,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _visit_row_to_stats(self, row) -> dict:
        if not row:
            return self._visit_entry()
        try:
            downloads = json.loads(row["downloads_json"] or "{}")
        except Exception:
            downloads = {}
        return {
            "total": int(row["total"] or 0),
            "landing": int(row["landing"] or 0),
            "install": int(row["install"] or 0),
            "pwa": int(row["pwa"] or 0),
            "launch": int(row["launch"] or 0),
            "downloads": downloads,
            "last_visited_at": row["last_visited_at"],
        }

    def list_history(self, device_fingerprint: Optional[str], apps_dir: Path) -> List[dict]:
        if not device_fingerprint:
            return []
        with self._lock:
            self._flush_visits_locked()
            app_ids = self._get_device_app_ids_locked(device_fingerprint)
            items = []
            for app_id in app_ids:
                row = self._conn.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
                if not row:
                    continue
                item = self._app_row_to_snapshot(row)
                visits = self._conn.execute("SELECT * FROM visits WHERE app_id = ?", (app_id,)).fetchone()
                stats = self._visit_row_to_stats(visits)
                item["visit_count"] = stats["total"]
                item["visit_breakdown"] = {
                    "landing": stats["landing"],
                    "install": stats["install"],
                    "pwa": stats["pwa"],
                    "launch": stats["launch"],
                }
                item["download_count"] = sum(int(v or 0) for v in (stats.get("downloads") or {}).values())
                item["download_breakdown"] = stats.get("downloads") or {}
                item["last_visited_at"] = stats.get("last_visited_at")
                icon_path = Path(apps_dir) / app_id / "icon.png"
                item["icon_url"] = f"/a/{app_id}/icon.png" if icon_path.exists() else None
                items.append(item)
            return items

    def export_history(self, device_fingerprint: Optional[str], apps_dir: Path) -> dict:
        items = self.list_history(device_fingerprint, apps_dir)
        export_items = []
        for item in items:
            app_id = item.get("app_id")
            icon_path = Path(apps_dir) / str(app_id) / "icon.png"
            icon_data_url = None
            if icon_path.exists():
                icon_data_url = "data:image/png;base64," + base64.b64encode(icon_path.read_bytes()).decode("ascii")
            export_items.append(
                {
                    "app_id": app_id,
                    "snapshot": item,
                    "recipe": deepcopy(item.get("recipe") or {}),
                    "icon_data_url": icon_data_url,
                }
            )
        return {
            "version": 1,
            "exported_at": _utc_now(),
            "items": export_items,
        }

    def count_recent_builds(self, device_fingerprint: Optional[str], since_iso: str) -> int:
        since = _parse_utc(since_iso)
        if since is None or not device_fingerprint:
            return 0
        with self._lock:
            self._flush_visits_locked()
            app_ids = self._get_device_app_ids_locked(device_fingerprint)
            count = 0
            for app_id in app_ids:
                row = self._conn.execute("SELECT created_at FROM apps WHERE app_id = ?", (app_id,)).fetchone()
                if not row:
                    continue
                created = _parse_utc(row["created_at"])
                if created and created >= since:
                    count += 1
            return count

    def remove_from_device(self, device_fingerprint: Optional[str], app_id: str) -> bool:
        if not device_fingerprint or not app_id:
            return False
        with self._lock:
            self._flush_visits_locked()
            previous = self._get_device_app_ids_locked(device_fingerprint)
            if app_id not in previous:
                return False
            filtered = [value for value in previous if value != app_id]
            self._set_device_app_ids_locked(device_fingerprint, filtered, _utc_now())
            return True

    def remove_many_from_device(self, device_fingerprint: Optional[str], app_ids: List[str]) -> List[str]:
        """Bulk variant of remove_from_device: one lock acquisition and one
        write instead of N, so a large clean-up does not hammer the store."""
        wanted = [app_id for app_id in (app_ids or []) if app_id]
        if not device_fingerprint or not wanted:
            return []
        with self._lock:
            self._flush_visits_locked()
            previous = self._get_device_app_ids_locked(device_fingerprint)
            previous_set = set(previous)
            removed_set = {app_id for app_id in wanted if app_id in previous_set}
            if not removed_set:
                return []
            filtered = [value for value in previous if value not in removed_set]
            self._set_device_app_ids_locked(device_fingerprint, filtered, _utc_now())
            return [app_id for app_id in wanted if app_id in removed_set]

    def list_expired_apps(self, cutoff_iso: str) -> List[dict]:
        cutoff = _parse_utc(cutoff_iso)
        if cutoff is None:
            return []
        with self._lock:
            self._flush_visits_locked()
            rows = self._conn.execute("SELECT * FROM apps").fetchall()
            expired = []
            for row in rows:
                app_id = row["app_id"]
                visit = self._conn.execute("SELECT * FROM visits WHERE app_id = ?", (app_id,)).fetchone()
                stats = self._visit_row_to_stats(visit)
                last_active_at = (
                    stats.get("last_visited_at")
                    or row["updated_at"]
                    or row["created_at"]
                )
                last_active_dt = _parse_utc(last_active_at)
                if last_active_dt is None or last_active_dt >= cutoff:
                    continue
                expired.append(
                    {
                        "app_id": app_id,
                        "name": row["name"] or app_id,
                        "last_active_at": last_active_at,
                    }
                )
            expired.sort(key=lambda item: item.get("last_active_at") or "")
            return expired

    def purge_apps(self, app_ids: List[str]) -> int:
        purge_ids = {str(app_id or "").strip() for app_id in app_ids if str(app_id or "").strip()}
        if not purge_ids:
            return 0
        removed = 0
        now = _utc_now()
        with self._lock:
            self._flush_visits_locked()
            for app_id in purge_ids:
                app_exists = self._conn.execute("SELECT 1 FROM apps WHERE app_id = ?", (app_id,)).fetchone()
                visit_exists = self._conn.execute("SELECT 1 FROM visits WHERE app_id = ?", (app_id,)).fetchone()
                if app_exists or visit_exists:
                    removed += 1
                self._conn.execute("DELETE FROM apps WHERE app_id = ?", (app_id,))
                self._conn.execute("DELETE FROM visits WHERE app_id = ?", (app_id,))
                self._pending_visits.pop(app_id, None)
            devices = self._conn.execute("SELECT device_id, app_ids_json FROM devices").fetchall()
            for device in devices:
                previous = []
                try:
                    previous = json.loads(device["app_ids_json"] or "[]")
                except Exception:
                    previous = []
                filtered = [value for value in previous if value not in purge_ids]
                if len(filtered) == len(previous):
                    continue
                self._set_device_app_ids_locked(device["device_id"], filtered, now)
            return removed

    def stats(self) -> dict:
        with self._lock:
            self._flush_visits_locked()
            apps = self._conn.execute("SELECT COUNT(*) AS c FROM apps").fetchone()["c"]
            devices = self._conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
            return {"backend": "sqlite", "apps": apps, "devices": devices, "db_path": str(self.db_path)}

    def traffic_totals(self) -> dict:
        """Homepage counters across all apps (flushes the pending buffer first).

        views: pure page views (landing + install + pwa + launch channels).
        downloads: per-platform download events summed from downloads_json.
        Both cover surviving apps only: the 30-day retention purge deletes an
        app's visits row along with the app itself.
        """
        with self._lock:
            self._flush_visits_locked()
            row = self._conn.execute(
                "SELECT COALESCE(SUM(landing + install + pwa + launch), 0) AS views FROM visits"
            ).fetchone()
            views = int(row["views"] or 0)
            downloads = 0
            for (blob,) in self._conn.execute("SELECT downloads_json FROM visits"):
                try:
                    data = json.loads(blob or "{}")
                except Exception:
                    continue
                if isinstance(data, dict):
                    downloads += sum(
                        int(value or 0)
                        for value in data.values()
                        if isinstance(value, (int, float))
                    )
            return {"views": views, "downloads": downloads}
