"""Synthetic adapters: these tests never connect to Telegram."""
import asyncio
from parser_app.telegram import TelegramGateway

class FakeGateway:
    def __init__(self, total=12, authorized=True):
        self.total, self.fail_at, self.rate_at, self.hook = total, None, None, None
        self.state = {"configured": authorized, "authorized": authorized, "step": "ready" if authorized else "settings", "account": "Учебный аккаунт" if authorized else "", "error": None}
        self.config = {"api_id": 12345, "api_hash": "a" * 32} if authorized else None
        self.closed = False
    def snapshot(self): return dict(self.state)
    async def initialize(self): pass
    async def close(self): self.closed = True
    async def auth(self, action, data):
        if action == "configure":
            self.config = TelegramGateway._validate_config(data)
            self.state.update(configured=True, step="phone")
        elif action == "send_code": self.state["step"] = "code"
        elif action == "verify_code" and data.get("code") == "22222": self.state["step"] = "password"
        elif action in ("verify_code", "verify_password", "refresh"):
            self.state.update(authorized=True, step="ready", account="Учебный аккаунт")
        elif action == "logout": self.state.update(authorized=False, step="phone", account="")
        return self.snapshot()
    async def resolve(self, reference):
        return {"id": -1001234567890, "title": "Учебный канал", "username": "example_channel"}
    async def latest_id(self, source): return self.total
    @staticmethod
    def message(index):
        return {"message_id": index, "sent_at": f"2026-09-07T12:{index % 60:02d}:00+00:00", "text": "Дизайн системы. Учебное сообщение " + str(index), "media": "text", "views": 0, "link": "https://t.me/example_channel/" + str(index), "service": False}
    async def messages(self, source, cursor, upper_id, limit, since=""):
        for index in range(cursor + 1, min(upper_id, cursor + limit) + 1):
            if self.fail_at == index:
                self.fail_at = None
                raise OSError("Synthetic network failure; no real account")
            if self.rate_at == index:
                from parser_app.domain import RateLimited
                self.rate_at = None
                raise RateLimited(1)
            if self.hook: await self.hook(index)
            yield self.message(index)

class FakeRuntime:
    def __init__(self, gateway): self.gateway = gateway
    def call(self, coroutine): return asyncio.run(coroutine)
