"""One cooperative queue per account: bounded scans, durable checkpoints, FloodWait."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from datetime import datetime, timezone

from .domain import AppError, RateLimited, matches
from .telegram import friendly_error

log = logging.getLogger("parser")


class Engine:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway
        self.task = None

    async def run(self):
        self.task = asyncio.current_task()
        await self.gateway.initialize()
        while True:
            try:
                if (hasattr(self.gateway, "prepare_job") or self.gateway.snapshot()["authorized"]) and self.store.cooldown() <= time.time():
                    job = self.store.claim()
                    if job:
                        await self.run_job(job)
                        continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Queue failure: %s", type(exc).__name__)
            await asyncio.sleep(0.5)

    async def run_job(self, job):
        job_id, source = job["id"], None
        buffer, scanned, cursor = [], 0, job["cursor"]

        def flush():
            nonlocal scanned, buffer
            if source is not None and scanned:
                self.store.checkpoint(job_id, source["id"], cursor, scanned, buffer)
                scanned, buffer = 0, []

        def stop():
            request = self.store.job(job_id)["stop_request"]
            if request:
                flush()
                self.store.state(job_id, "paused" if request == "pause" else "cancelled")
                return True
            return False

        try:
            while True:
                try:
                    if stop():
                        return
                    if hasattr(self.gateway, "prepare_job"):
                        await self.gateway.prepare_job(job)
                    source = await self.gateway.resolve(job["source_key"])
                    upper = job["upper_id"] if job["upper_id"] is not None else await self.gateway.latest_id(source)
                    job = self.store.prepare(job_id, source, upper)
                    cursor = job["cursor"]
                    remaining = job["options"]["limit"] - job["scanned"]
                    last_flush = last_poll = time.monotonic()
                    if upper > cursor and remaining > 0:
                        async for message in self.gateway.messages(source, cursor, upper, remaining, job["options"]["date_from"]):
                            clock = time.monotonic()
                            if clock - last_poll >= 0.25:
                                if stop():
                                    return
                                last_poll = clock
                            if message["message_id"] <= cursor or message["message_id"] > upper:
                                continue
                            cursor, scanned = message["message_id"], scanned + 1
                            if matches(message, job["options"]):
                                buffer.append(message)
                            if scanned >= 50 or clock - last_flush >= 1:
                                flush()
                                last_flush = clock
                    flush()
                    if stop():
                        return
                    result = self.store.job(job_id)
                    completion = "limit" if result["scanned"] >= job["options"]["limit"] and cursor < upper else "end"
                    self.store.state(job_id, "completed", completion=completion)
                    return
                except RateLimited as exc:
                    flush()
                    self.store.set_cooldown(exc.seconds)
                    until = self.store.cooldown()
                    self.store.state(job_id, "waiting", wait_until=datetime.fromtimestamp(until, timezone.utc).isoformat())
                    while time.time() < until:
                        if stop():
                            return
                        await asyncio.sleep(min(0.5, max(0, until - time.time())))
                    if stop():
                        return
                    self.store.state(job_id, "running")
                    job = self.store.job(job_id)
        except asyncio.CancelledError:
            flush()
            self.store.state(job_id, "paused", error="Приложение закрыто. Сбор можно продолжить.")
            raise
        except Exception as exc:
            flush()
            if not stop():
                self.store.state(job_id, "failed", error=str(friendly_error(exc)))
            log.error("Job failure: %s", type(exc).__name__)

    async def shutdown(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.gateway.close()


class Runtime:
    """The HTTP threads never operate on Telethon's asyncio client directly."""
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway
        self.engine = Engine(store, gateway)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, name="telegram-worker", daemon=True)
        self.thread.start()
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self.engine.run()))

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coroutine, timeout=40):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise AppError("Telegram не ответил вовремя. Проверьте подключение перед повторной попыткой.", 504, "timeout") from None

    def close(self):
        try:
            self.call(self.engine.shutdown(), timeout=20)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if not self.thread.is_alive():
                self.loop.close()
