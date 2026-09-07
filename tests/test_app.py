import asyncio
import csv
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from parser_app.__main__ import instance_lock, private_json
from parser_app.domain import AppError, bounded_int, export_rows, job_spec, matches, source_key
from parser_app.engine import Engine
from parser_app.server import Application, LocalServer
from parser_app.store import Store
from parser_app.telegram import TelegramGateway, friendly_error
from tests.support import FakeGateway, FakeRuntime


def options(**kwargs):
    return job_spec({"sources": ["@example_channel"], **kwargs})[1]


class DomainTests(unittest.TestCase):
    def test_source_formats(self):
        for value in ("@Example_Channel", "https://t.me/example_channel", "t.me/s/example_channel/123"):
            self.assertEqual(source_key(value), "@example_channel")
        self.assertEqual(source_key("-1001234567890"), "-1001234567890")

    def test_reject_unsafe_sources(self):
        for value in ("https://t.me.evil.test/channel", "https://t.me/+invite", "https://t.me/proxy", "http://t.me/channel", "file:///secret", "https://[broken", "https://t.me/channel?start=x", 123, ""):
            with self.subTest(value=value), self.assertRaises(AppError):
                source_key(value)

    def test_validation(self):
        for value in (True, 1.2, -1, 50001, "9" * 5000, "١٢"):
            with self.subTest(value=value), self.assertRaises(AppError):
                bounded_int(value, "Лимит", 1, 50000)
        for payload in ({"date_from": "2026-09-08", "date_to": "2026-09-07"}, {"date_from": "0001-01-01"}, {"date_to": "2026-02-30"}, {"media": "executable"}, {"incremental": 1}, {"include": [1]}):
            with self.subTest(payload=payload), self.assertRaises(AppError):
                options(**payload)

    def test_normalize_deduplicate(self):
        sources, spec = job_spec({"sources": "\n@Example_Channel\nhttps://t.me/example_channel\n", "include": "ДИЗАЙН, дизайн"})
        self.assertEqual(sources, ["@example_channel"])
        self.assertEqual(spec["include"], ["дизайн"])

    def test_filter_semantics(self):
        message = FakeGateway.message(1)
        self.assertTrue(matches(message, options(include=["ДИЗАЙН"], date_from="2026-09-07", date_to="2026-09-07")))
        self.assertFalse(matches(message, options(exclude=["системы"])))
        self.assertFalse(matches(message, options(media="photo")))
        self.assertFalse(matches(message, options(date_from="2026-09-08")))
        self.assertFalse(matches({**message, "service": True}, options()))
        self.assertTrue(matches({**message, "text": "", "media": "photo"}, options()))

    def test_export_roundtrip_and_formula_safety(self):
        row = {**FakeGateway.message(1), "source_id": -1001, "source_title": "Учебный канал", "text": '=HYPERLINK("bad")\nТекст, с запятой'}
        raw = b"".join(export_rows(iter([row]), "csv")).decode("utf-8-sig")
        restored = list(csv.DictReader(io.StringIO(raw)))[0]
        self.assertTrue(restored["text"].startswith("'="))
        for fmt in ("json", "jsonl"):
            parsed = json.loads(b"".join(export_rows(iter([row]), fmt)))
            self.assertEqual((parsed[0] if fmt == "json" else parsed)["text"], row["text"])
        self.assertEqual(b"".join(export_rows(iter([]), "json")), b"[]\n")

    def test_csv_whitespace_prefixes(self):
        from parser_app.domain import safe_csv
        for text in (" +SUM(1)", "\t=1", "\ufeff@formula", "-2+3", "\ntext"):
            self.assertTrue(safe_csv(text).startswith("'"))
        self.assertEqual(safe_csv("Обычный текст"), "Обычный текст")

    def test_errors_do_not_leak_exception_data(self):
        self.assertNotIn("PRIVATE_VALUE", str(friendly_error(RuntimeError("PRIVATE_VALUE"))))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "test.sqlite3")
        self.source = {"id": -1001234567890, "title": "Учебный канал", "username": "example_channel"}

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self, **kwargs):
        job_id = self.store.enqueue(["@example_channel"], options(**kwargs))[0]
        self.store.claim()
        return self.store.prepare(job_id, self.source, 120)

    def test_deduplication_and_unicode_search(self):
        job = self.prepared()
        first = {**FakeGateway.message(1), "text": "ДИЗАЙН 100%_готов"}
        self.store.checkpoint(job["id"], self.source["id"], 1, 1, [first])
        self.store.checkpoint(job["id"], self.source["id"], 1, 0, [{**first, "views": 12}])
        self.assertEqual(self.store.stats()["messages"], 1)
        self.assertEqual(self.store.messages(query="дизайн 100%_")["total"], 1)
        self.assertEqual(self.store.messages(query="%wrong_")["total"], 0)
        self.assertEqual(self.store.messages()["items"][0]["views"], 12)

    def test_atomic_checkpoint(self):
        job = self.prepared()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.checkpoint(job["id"], self.source["id"], 2, 2, [FakeGateway.message(1), {**FakeGateway.message(2), "text": None}])
        self.assertEqual(self.store.stats()["messages"], 0)
        self.assertEqual(self.store.job(job["id"])["cursor"], 0)
        self.assertEqual(self.store.sources()[0]["last_id"], 0)

    def test_batch_enqueue_is_atomic(self):
        self.store.enqueue(["@example_channel"], options())
        with self.assertRaises(AppError):
            self.store.enqueue(["@another_channel", "@example_channel"], options())
        self.assertEqual(len(self.store.jobs()), 1)

    def test_resume_and_history_modes(self):
        job = self.prepared()
        self.store.checkpoint(job["id"], self.source["id"], 5, 5, [FakeGateway.message(5)])
        self.store.state(job["id"], "completed")
        new = self.prepared()
        self.assertEqual(new["cursor"], 5)
        self.store.state(new["id"], "completed")
        self.assertEqual(self.prepared(incremental=False)["cursor"], 0)

    def test_pause_resume_cancel(self):
        job = self.prepared()
        self.store.control(job["id"], "pause")
        self.assertEqual(self.store.job(job["id"])["stop_request"], "pause")
        self.store.state(job["id"], "paused")
        self.store.control(job["id"], "resume")
        self.assertEqual(self.store.job(job["id"])["state"], "queued")
        self.store.control(job["id"], "cancel")
        self.assertEqual(self.store.job(job["id"])["state"], "cancelled")

    def test_recovery_preserves_rate_limit(self):
        job = self.prepared()
        self.store.state(job["id"], "waiting")
        self.store.set_cooldown(30)
        self.store.recover()
        self.assertEqual(self.store.job(job["id"])["state"], "paused")
        self.assertGreater(Store(self.store.path).cooldown(), time.time())

    def test_concurrent_claim_is_unique(self):
        self.store.enqueue(["@channel_" + str(n) for n in range(8)], options())
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual(len({job["id"] for job in jobs}), 8)

    def test_clear_requires_idle_queue(self):
        job = self.prepared()
        with self.assertRaises(AppError):
            self.store.clear()
        self.store.state(job["id"], "paused")
        self.store.clear()
        self.assertEqual(self.store.jobs(), [])

    def test_instance_lock_releases(self):
        with instance_lock(self.directory):
            with self.assertRaises(AppError):
                with instance_lock(self.directory):
                    pass
        with instance_lock(self.directory):
            pass

    def test_private_file_permissions(self):
        path = self.directory / "private.json"
        private_json(path, {"example": True})
        self.assertEqual(json.loads(path.read_text()), {"example": True})
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.gateway = FakeGateway(total=120)
        self.engine = Engine(self.store, self.gateway)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def queued(self, **kwargs):
        job_id = self.store.enqueue(["@example_channel"], options(**kwargs))[0]
        return job_id, self.store.claim()

    async def test_bounded_scan(self):
        job_id, job = self.queued(limit=50)
        await self.engine.run_job(job)
        result = self.store.job(job_id)
        self.assertEqual((result["state"], result["scanned"], result["completion"]), ("completed", 50, "limit"))
        self.assertEqual(self.store.stats()["messages"], 50)

    async def test_failure_resume_without_duplicates(self):
        self.gateway.fail_at = 78
        job_id, job = self.queued()
        await self.engine.run_job(job)
        self.assertEqual(self.store.job(job_id)["state"], "failed")
        self.assertEqual(self.store.job(job_id)["cursor"], 77)
        self.store.control(job_id, "resume")
        await self.engine.run_job(self.store.claim())
        self.assertEqual(self.store.job(job_id)["state"], "completed")
        self.assertEqual(self.store.stats()["messages"], 120)
        self.assertEqual(self.store.job(job_id)["scanned"], 120)

    async def test_pause_resume_checkpoint(self):
        job_id, job = self.queued()
        async def hook(index):
            if index == 10:
                self.store.control(job_id, "pause")
                await asyncio.sleep(0.3)
        self.gateway.hook = hook
        await self.engine.run_job(job)
        self.assertEqual(self.store.job(job_id)["state"], "paused")
        self.assertGreater(self.store.job(job_id)["cursor"], 0)
        self.gateway.hook = None
        self.store.control(job_id, "resume")
        await self.engine.run_job(self.store.claim())
        self.assertEqual(self.store.stats()["messages"], 120)

    async def test_flood_wait_can_be_cancelled(self):
        self.gateway.rate_at = 2
        job_id, job = self.queued()
        task = asyncio.create_task(self.engine.run_job(job))
        for _ in range(100):
            if self.store.job(job_id)["state"] == "waiting":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.store.job(job_id)["state"], "waiting")
        self.store.control(job_id, "cancel")
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.store.job(job_id)["state"], "cancelled")
        self.assertGreater(self.store.cooldown(), time.time())

    async def test_config_never_returned_to_browser(self):
        gateway = TelegramGateway(Path(self.temp.name))
        result = await gateway.auth("configure", {"api_id": 123, "api_hash": "a" * 32})
        self.assertTrue(result["configured"])
        self.assertNotIn("api_hash", result)
        self.assertNotIn("api_id", result)
        await gateway.close()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.gateway = FakeGateway()
        app = Application(self.store, FakeRuntime(self.gateway), "local-test-token", self.temp.name)
        self.server = LocalServer(("127.0.0.1", 0), app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        values = {"Authorization": "Bearer local-test-token", **(headers or {})}
        raw = None if data is None else json.dumps(data).encode()
        if raw is not None:
            values.setdefault("Content-Type", "application/json")
        req = Request(self.origin + path, data=raw, headers=values, method=method)
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_capability_required(self):
        status, _, body = self.request("/api/status", headers={"Authorization": ""})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["code"], "session")
        self.assertEqual(self.request("/api/status")[0], 200)

    def test_host_and_origin_protection(self):
        for headers in ({"Origin": "https://malicious.test"}, {"Host": "malicious.test"}, {"Origin": "null"}):
            self.assertEqual(self.request("/api/status", headers=headers)[0], 403)

    def test_static_security_headers(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"view-guide", body)
        self.assertNotIn(b'id="statMessages"', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request("/static/../config.json")[0], 404)

    def test_write_validation(self):
        self.assertEqual(self.request("/api/jobs", "POST", {"sources": []})[0], 400)
        self.assertEqual(self.request("/api/jobs", "POST", {"sources": ["@example_channel"]}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/jobs", "POST", {"sources": ["@example_channel"]})[0], 200)
        self.assertEqual(self.request("/api/auth/logout", "POST", {})[0], 409)

    def test_empty_export_is_valid(self):
        status, headers, body = self.request("/api/export?format=json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.request("/api/export?format=exe")[0], 400)

    def test_clear_and_shutdown_require_confirmation(self):
        self.assertEqual(self.request("/api/history", "DELETE", {})[0], 400)
        self.assertEqual(self.request("/api/shutdown", "POST", {})[0], 400)
        self.assertEqual(self.request("/api/history", "DELETE", {"confirm": "delete-local-history"})[0], 200)


if __name__ == "__main__":
    unittest.main()
