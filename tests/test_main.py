import ipaddress
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi.testclient import TestClient

from server import main


class MainHelpersTests(unittest.TestCase):
    def make_request(self, *, client_host: str, headers: Optional[dict] = None, scheme: str = "https", netloc: str = "service.test"):
        return SimpleNamespace(
            headers=headers or {},
            client=SimpleNamespace(host=client_host),
            url=SimpleNamespace(scheme=scheme, netloc=netloc),
        )

    def test_client_ip_ignores_forwarded_headers_from_untrusted_clients(self):
        original = main.TRUSTED_PROXY_NETWORKS
        main.TRUSTED_PROXY_NETWORKS = (ipaddress.ip_network("127.0.0.1/32"),)
        try:
            request = self.make_request(
                client_host="198.51.100.9",
                headers={"x-forwarded-for": "203.0.113.5", "x-real-ip": "203.0.113.6"},
            )
            self.assertEqual(main._client_ip(request), "198.51.100.9")
        finally:
            main.TRUSTED_PROXY_NETWORKS = original

    def test_client_ip_accepts_forwarded_headers_from_trusted_proxy(self):
        original = main.TRUSTED_PROXY_NETWORKS
        main.TRUSTED_PROXY_NETWORKS = (ipaddress.ip_network("127.0.0.1/32"),)
        try:
            request = self.make_request(
                client_host="127.0.0.1",
                headers={"x-forwarded-for": "203.0.113.5", "x-real-ip": "203.0.113.6"},
            )
            self.assertEqual(main._client_ip(request), "203.0.113.5")
        finally:
            main.TRUSTED_PROXY_NETWORKS = original

    def test_resolve_base_url_ignores_forwarded_host_without_trusted_proxy(self):
        original = main.TRUSTED_PROXY_NETWORKS
        main.TRUSTED_PROXY_NETWORKS = (ipaddress.ip_network("127.0.0.1/32"),)
        try:
            request = self.make_request(
                client_host="198.51.100.9",
                headers={"x-forwarded-proto": "http", "x-forwarded-host": "evil.test", "host": "service.test"},
                scheme="https",
                netloc="service.test",
            )
            self.assertEqual(main._resolve_base_url(request), "https://service.test")
        finally:
            main.TRUSTED_PROXY_NETWORKS = original

    def test_load_recipe_evicts_least_recently_used_entries(self):
        original_apps_dir = main.APPS_DIR
        original_cache_size = main.RECIPE_CACHE_SIZE
        with tempfile.TemporaryDirectory() as tmpdir:
            apps_dir = Path(tmpdir)
            for app_id in ("a1", "b2", "c3"):
                path = apps_dir / app_id
                path.mkdir(parents=True)
                (path / "recipe.json").write_text(json.dumps({"id": app_id}))
            main.APPS_DIR = apps_dir
            main.RECIPE_CACHE_SIZE = 2
            with main._recipe_cache_lock:
                main._recipe_cache.clear()
            try:
                main._load_recipe("a1")
                main._load_recipe("b2")
                main._load_recipe("a1")
                main._load_recipe("c3")
                with main._recipe_cache_lock:
                    self.assertEqual(list(main._recipe_cache.keys()), ["a1", "c3"])
            finally:
                main.APPS_DIR = original_apps_dir
                main.RECIPE_CACHE_SIZE = original_cache_size
                with main._recipe_cache_lock:
                    main._recipe_cache.clear()


class EntryCacheHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_root_html_must_revalidate(self):
        # Stale entry HTML + fresh ?v= assets inside it = users stranded on
        # old UI talking to a new API (all-zero stats). no-cache forces a
        # cheap 304 revalidation instead of heuristic caching.
        for path in ("/", "/index.html"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("cache-control"), "no-cache")

    def test_versioned_assets_stay_long_cached(self):
        resp = self.client.get("/css/style.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("max-age=86400", resp.headers.get("cache-control", ""))


class HistoryBulkDeleteTests(unittest.TestCase):
    """POST /api/history/delete-bulk — one request replaces the per-id DELETE
    fan-out that froze the server (issue #59), and the old /api/history/recover
    endpoint (which attached every app on the server to the caller) is gone."""

    FP = "test-device-fp"

    def setUp(self):
        self.client = TestClient(main.app)
        self._tmp = tempfile.TemporaryDirectory()
        self.apps_dir = Path(self._tmp.name)
        self.original_apps_dir = main.APPS_DIR
        self.original_store = main.history_store
        main.APPS_DIR = self.apps_dir
        main.history_store = __import__("server.history_store", fromlist=["HistoryStore"]).HistoryStore(
            self.apps_dir / "_history.json"
        )

    def tearDown(self):
        main.APPS_DIR = self.original_apps_dir
        main.history_store = self.original_store
        self._tmp.cleanup()

    def _record(self, app_id):
        (self.apps_dir / app_id).mkdir(parents=True, exist_ok=True)
        main.history_store.record_build(self.FP, {"id": app_id, "name": app_id, "url": f"https://{app_id}.test"}, f"/a/{app_id}", None)

    def _cookies(self):
        return {"webtoapp_device_fingerprint": self.FP}

    def test_bulk_delete_removes_only_requested_ids(self):
        for app_id in ("a1", "b2", "c3"):
            self._record(app_id)
        resp = self.client.post(
            "/api/history/delete-bulk",
            json={"app_ids": ["a1", "c3", "missing"]},
            cookies=self._cookies(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["removed"], ["a1", "c3"])
        remaining = [item["app_id"] for item in resp.json()["history"]["items"]]
        self.assertEqual(remaining, ["b2"])

    def test_bulk_delete_requires_device_fingerprint(self):
        resp = self.client.post("/api/history/delete-bulk", json={"app_ids": ["a1"]})
        self.assertEqual(resp.status_code, 400)

    def test_bulk_delete_rejects_oversized_payload(self):
        app_ids = [f"app{i}" for i in range(main.HISTORY_BULK_DELETE_MAX + 1)]
        resp = self.client.post(
            "/api/history/delete-bulk",
            json={"app_ids": app_ids},
            cookies=self._cookies(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_bulk_delete_empty_list_is_a_noop(self):
        resp = self.client.post(
            "/api/history/delete-bulk",
            json={"app_ids": []},
            cookies=self._cookies(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["removed"], [])

    def test_recover_endpoint_is_removed(self):
        # 405 (matches the DELETE /{app_id} route as "recover") proves no POST
        # recover handler exists anymore.
        resp = self.client.post("/api/history/recover", cookies=self._cookies())
        self.assertIn(resp.status_code, (404, 405))
