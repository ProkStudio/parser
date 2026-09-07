"""Read-only Telethon adapter. Credentials and sessions never leave the data directory."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import VERSION, AppError, RateLimited, bounded_int


ERRORS = {
    "ApiIdInvalidError": "Telegram отклонил API ID / API Hash. Проверьте их на my.telegram.org.",
    "PhoneNumberInvalidError": "Проверьте номер телефона: нужен международный формат, например +79991234567.",
    "PhoneCodeInvalidError": "Неверный код Telegram. Проверьте код и попробуйте ещё раз.",
    "PhoneCodeExpiredError": "Код истёк. Запросите новый код.",
    "PasswordHashInvalidError": "Неверный пароль двухэтапной аутентификации.",
    "PhoneNumberBannedError": "Telegram ограничил этот номер. Обратитесь в поддержку Telegram.",
    "ChannelPrivateError": "Нет доступа к каналу или группе. Приложение не обходит ограничения доступа.",
    "ChatAdminRequiredError": "Telegram не разрешает читать историю с текущими правами.",
    "UsernameInvalidError": "Некорректный username источника.",
    "UsernameNotOccupiedError": "Канал или группа с таким username не найдены.",
    "AuthKeyUnregisteredError": "Сессия Telegram недействительна. Подключите аккаунт заново.",
    "SessionRevokedError": "Сессия Telegram отозвана. Подключите аккаунт заново.",
    "UserDeactivatedBanError": "Аккаунт ограничен Telegram. Сбор остановлен.",
}


def friendly_error(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        return exc
    if type(exc).__name__ in ERRORS:
        return AppError(ERRORS[type(exc).__name__], 400, "telegram")
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return AppError("Не удалось связаться с Telegram. Проверьте интернет и повторите попытку.", 503, "network")
    if isinstance(exc, ValueError):
        return AppError("Источник не найден в доступных диалогах. Проверьте username или ID.", 400, "telegram")
    return AppError("Telegram не выполнил запрос. Повторите позже; технический тип ошибки записан в локальный журнал.", 502, "telegram")


class TelegramGateway:
    def __init__(self, directory: Path):
        self.directory = directory
        self.client = None
        self.proxy = None
        self._auth_lock = asyncio.Lock()
        self._phone = self._code_hash = None
        self._code_at = self._next_code_at = 0.0
        self.config = None
        self.state = {"configured": False, "authorized": False, "step": "settings", "account": "", "error": None}
        try:
            path = directory / "config.json"
            if path.exists():
                config = json.loads(path.read_text(encoding="utf-8"))
                self.config = self._validate_config(config)
                self.state.update(configured=True, step="phone")
        except (AppError, ValueError, OSError, TypeError):
            self.state["error"] = "Не удалось прочитать настройки. Сохраните API ID и API Hash заново."

    @staticmethod
    def _validate_config(data):
        if not isinstance(data, dict):
            raise AppError("Некорректные настройки Telegram.")
        api_id = bounded_int(data.get("api_id", ""), "API ID", 1, 2147483647)
        api_hash = data.get("api_hash", "")
        if not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash.strip()):
            raise AppError("API Hash должен содержать 32 шестнадцатеричных символа.")
        return {"api_id": api_id, "api_hash": api_hash.strip().lower()}

    def snapshot(self):
        return dict(self.state)

    async def _open(self):
        if not self.config:
            raise AppError("Сначала сохраните API ID и API Hash.", 409)
        if self.client is None:
            try:
                from telethon import TelegramClient, types, utils
            except ImportError:
                raise AppError("Не установлен Telethon. Выполните установку зависимостей по README и перезапустите Parser.", 503, "dependency") from None
            self.types, self.utils = types, utils
            self.client = TelegramClient(str(self.directory / "telegram"), self.config["api_id"], self.config["api_hash"],
                                         proxy=self.proxy, receive_updates=False,
                                         timeout=15, connection_retries=2, request_retries=0,
                                         flood_sleep_threshold=0, device_model="Parser Desktop", app_version=VERSION)
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def _refresh(self):
        client = await self._open()
        authorized = await client.is_user_authorized()
        account = ""
        if authorized:
            me = await client.get_me()
            account = " ".join(filter(None, (me.first_name, me.last_name))) or me.username or "Аккаунт Telegram"
        self.state.update(authorized=authorized, account=account, step="ready" if authorized else "phone", error=None)
        return self.snapshot()

    async def initialize(self):
        if self.config:
            try:
                await self._refresh()
            except Exception as exc:
                self.state["error"] = str(friendly_error(exc))

    async def auth(self, action: str, data: dict):
        async with self._auth_lock:
            try:
                return await self._auth(action, data)
            except Exception as exc:
                if type(exc).__name__ == "FloodWaitError":
                    seconds = max(1, int(exc.seconds))
                    self._next_code_at = time.monotonic() + seconds
                    raise AppError(f"Telegram просит подождать {seconds} сек. Не запрашивайте код повторно до окончания ожидания.", 429, "rate_limit", seconds) from None
                if type(exc).__name__ == "PhoneCodeExpiredError":
                    self._code_hash = None
                    self.state["step"] = "phone"
                if type(exc).__name__ in ("AuthKeyUnregisteredError", "SessionRevokedError"):
                    self.state.update(authorized=False, step="phone", account="")
                raise friendly_error(exc) from None

    async def _auth(self, action, data):
        if action == "configure":
            if self.state["authorized"]:
                raise AppError("Перед изменением API-параметров отключите аккаунт.", 409)
            config = self._validate_config(data)
            await self.close()
            path = self.directory / "config.json"
            temp = path.with_suffix(".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(config, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            self.config = config
            self._code_hash = self._phone = None
            self.state.update(configured=True, authorized=False, step="phone", error=None)
            return self.snapshot()
        if action == "refresh":
            return await self._refresh()
        client = await self._open()
        if action == "send_code":
            if self.state["authorized"]:
                raise AppError("Аккаунт уже подключён.", 409)
            phone = data.get("phone", "")
            if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
                raise AppError("Нужен номер в международном формате: +79991234567.")
            if time.monotonic() < self._next_code_at:
                wait = int(self._next_code_at - time.monotonic()) + 1
                raise AppError(f"Повторный запрос кода будет доступен через {wait} сек.", 429, "rate_limit", wait)
            self._next_code_at = time.monotonic() + 60
            sent = await client.send_code_request(phone)
            self._phone, self._code_hash, self._code_at = phone, sent.phone_code_hash, time.monotonic()
            self.state.update(step="code", error=None)
        elif action == "verify_code":
            code = data.get("code", "")
            if not isinstance(code, str) or not re.fullmatch(r"\d{4,10}", code):
                raise AppError("Введите цифровой код из Telegram.")
            if not self._code_hash or time.monotonic() - self._code_at > 600:
                self.state["step"] = "phone"
                raise AppError("Запросите новый код: предыдущий запрос истёк.")
            try:
                await client.sign_in(phone=self._phone, code=code, phone_code_hash=self._code_hash)
            except Exception as exc:
                if type(exc).__name__ != "SessionPasswordNeededError":
                    raise
                self._code_hash = None
                self.state["step"] = "password"
                return self.snapshot()
            self._code_hash = self._phone = None
            return await self._refresh()
        elif action == "verify_password":
            password = data.get("password", "")
            if self.state["step"] != "password":
                raise AppError("Сначала подтвердите код Telegram.", 409)
            if not isinstance(password, str) or not 1 <= len(password) <= 256:
                raise AppError("Введите пароль двухэтапной аутентификации.")
            await client.sign_in(password=password)
            self._phone = None
            return await self._refresh()
        elif action == "logout":
            await client.log_out()  # Explicit user action; revokes only this app's session.
            await self.close()
            self._phone = self._code_hash = None
            self.state.update(authorized=False, account="", step="phone", error=None)
        else:
            raise AppError("Неизвестное действие подключения.", 404)
        return self.snapshot()

    async def resolve(self, reference: str):
        try:
            client = await self._open()
            target = int(reference) if reference.startswith("-100") else reference
            try:
                entity = await client.get_entity(target)
            except ValueError:
                if not isinstance(target, int):
                    raise
                # Imported sessions intentionally have no entity/access-hash cache.
                # Resolve accessible private groups from the user's own dialogs.
                entity = None
                async for dialog in client.iter_dialogs(limit=500):
                    if self.utils.get_peer_id(dialog.entity) == target:
                        entity = dialog.entity
                        break
                if entity is None:
                    raise AppError("Группа не найдена среди 500 доступных диалогов. Проверьте ID и участие аккаунта в группе.")
            if not isinstance(entity, (self.types.Channel, self.types.Chat)):
                raise AppError("Поддерживаются только каналы и группы, не личные переписки.")
            return {"id": self.utils.get_peer_id(entity), "title": entity.title, "username": getattr(entity, "username", None) or "", "entity": entity}
        except Exception as exc:
            self._raise_read_error(exc)

    async def latest_id(self, source):
        try:
            messages = await self.client.get_messages(source["entity"], limit=1)
            return messages[0].id if messages else 0
        except Exception as exc:
            self._raise_read_error(exc)

    async def messages(self, source, cursor, upper_id, limit, since=""):
        offset = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) - timedelta(seconds=1) if since and cursor == 0 else None
        try:
            async for message in self.client.iter_messages(source["entity"], min_id=cursor, max_id=upper_id+1, reverse=True, limit=limit, offset_date=offset):
                media = "text"
                for name in ("photo", "video", "audio", "document"):
                    if getattr(message, name, None):
                        media = name
                        break
                if media == "text" and getattr(message, "media", None) and not isinstance(message.media, self.types.MessageMediaWebPage):
                    media = "other"
                if getattr(message, "voice", None):
                    media = "audio"
                if getattr(message, "video_note", None):
                    media = "video"
                timestamp = getattr(message, "date", None)
                username = source["username"]
                private_id = str(source["id"])[4:] if str(source["id"]).startswith("-100") else ""
                link = "https://" + "t.me/" + username + "/" + str(message.id) if username else ("https://" + "t.me/c/" + private_id + "/" + str(message.id) if private_id else "")
                yield {"message_id": message.id, "sent_at": (timestamp.astimezone(timezone.utc) if timestamp else datetime(1970, 1, 1, tzinfo=timezone.utc)).isoformat(timespec="seconds"),
                       "text": getattr(message, "message", None) or "", "media": media, "views": getattr(message, "views", None) or 0, "link": link,
                       "service": timestamp is None or bool(getattr(message, "action", None))}
        except Exception as exc:
            self._raise_read_error(exc)

    def _raise_read_error(self, exc):
        if type(exc).__name__ == "FloodWaitError":
            raise RateLimited(exc.seconds) from None
        if type(exc).__name__ in ("AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedBanError"):
            self.state.update(authorized=False, step="phone", account="")
        raise friendly_error(exc) from None

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
