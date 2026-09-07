"""Account-aware extension of the existing secured loopback API."""
from __future__ import annotations

import re

from .accounts import AccountGateway
from .domain import AppError, job_spec
from .proxies import add_proxy, check_proxy, delete_proxy, list_proxies, proxy_entry
from .server import Application


class DesktopApplication(Application):
    def __init__(self, *args):
        super().__init__(*args)
        self.native = False
        self.focus_window = None

    def require_idle(self):
        if self.store.busy():
            raise AppError("Сначала поставьте активные задачи на паузу или отмените их. Изменения аккаунтов и прокси не применяются посреди сбора.", 409)

    def dispatch(self, method, path, query, data):
        manager = self.runtime.gateway
        if not isinstance(manager, AccountGateway):
            return super().dispatch(method, path, query, data)
        vault = manager.vault
        if method == "GET":
            if path == "/api/status":
                return {**super().dispatch(method, path, query, data), "desktop": self.native}
            if path == "/api/accounts":
                return {"items": manager.accounts(), "selected": manager.selected}
            if path == "/api/proxies":
                return {"items": list_proxies(vault)}
        with self.mutations:
            if method == "POST" and path == "/api/window/focus":
                if self.focus_window:
                    self.focus_window()
                return {"ok": bool(self.focus_window)}
            if method == "POST" and path == "/api/accounts":
                self.require_idle()
                account_id = vault.add(data.get("label") or "Новый аккаунт")
                manager.selected = account_id
                return manager.snapshot()
            match = re.fullmatch(r"/api/accounts/([a-f0-9]{32}|legacy)/(select|check|remove|proxy)", path)
            if method == "POST" and match:
                account_id, action = match[1], match[2]
                entry = vault.entry(account_id)
                if action == "select":
                    manager.selected = account_id
                    return manager.snapshot()
                self.require_idle()
                if action == "check":
                    try:
                        return self.runtime.call(manager.auth("refresh", {"account_id": account_id}))
                    except AppError as exc:
                        client = manager.get(account_id)
                        client.state.update(authorized=False, error=str(exc))
                        if exc.code == "rate_limit" and exc.retry_after:
                            self.store.set_cooldown(exc.retry_after)
                        raise
                if action == "remove":
                    if data.get("confirm") != "remove-local-account":
                        raise AppError("Необходимо подтверждение локального удаления аккаунта.")
                    self.runtime.call(manager.reset(account_id))
                    vault.remove(account_id)
                    if manager.selected == account_id:
                        manager.selected = next((e["id"] for e in vault.entries()), "")
                    return {"ok": True}
                if action == "proxy":
                    proxy_id = data.get("proxy_id", "")
                    if not isinstance(proxy_id, str):
                        raise AppError("Некорректный прокси.")
                    if proxy_id:
                        proxy_entry(vault, proxy_id)
                    self.runtime.call(manager.reset(account_id))
                    vault.update(account_id, proxy_id=proxy_id)
                    return {"ok": True}
            if method == "POST" and path == "/api/proxies":
                self.require_idle()
                return add_proxy(vault, data)
            match = re.fullmatch(r"/api/proxies/([a-f0-9]{32})/(check|remove)", path)
            if method == "POST" and match:
                self.require_idle()
                if match[2] == "check":
                    return check_proxy(vault, match[1])
                if data.get("confirm") != "remove-proxy":
                    raise AppError("Необходимо подтверждение удаления прокси.")
                return delete_proxy(vault, match[1])
            if method == "POST" and path == "/api/jobs":
                account_id = data.get("account_id", "")
                entry = vault.entry(account_id)
                client = manager.get(account_id)
                if not client.config:
                    raise AppError("Настройте API-параметры этого аккаунта во вкладке «Аккаунты».", 409)
                if not client.state["authorized"]:
                    raise AppError("Нажмите «Проверить» у этого аккаунта или завершите вход по номеру, затем создайте задачу.", 409)
                sources, options = job_spec(data)
                options.update(account_id=account_id, account_label=entry["label"])
                return {"ids": self.store.enqueue(sources, options)}
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/resume", path)
            if method == "POST" and match:
                job = self.store.job(match[1])
                account_id = job["options"].get("account_id", "legacy")
                vault.entry(account_id)
                return self.store.control(match[1], "resume")
            if method == "POST" and path.startswith("/api/auth/"):
                self.require_idle()
                action = path.rsplit("/", 1)[1]
                if action not in ("configure", "send_code", "verify_code", "verify_password", "refresh", "logout"):
                    raise AppError("Действие не найдено.", 404)
                try:
                    return self.runtime.call(manager.auth(action, data))
                except AppError as exc:
                    if exc.code == "rate_limit" and exc.retry_after:
                        self.store.set_cooldown(exc.retry_after)
                    raise
        # Parent retains the shared write lock, token and strict origin checks.
        return super().dispatch(method, path, query, data)
