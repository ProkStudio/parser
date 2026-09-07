"""Local account vault and offline, explicit imports of the user's own sessions.

No network calls during import. Only a sanitized authorization record is copied;
entity/contact caches, arbitrary SQLite schema and source file paths are not kept.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import multiprocessing
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from .domain import AppError, RateLimited
from .telegram import TelegramGateway

# Production Telegram IPv4 DCs. Never trust a server address embedded in an import.
DC_ADDRESSES = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100",
                4: "149.154.167.91", 5: "91.108.56.130"}
MAX_SESSION_BYTES = 32 * 1024 * 1024
EMPTY_STATE = {"configured": False, "authorized": False, "step": "settings", "account": "", "error": None}


def private_json(path, value):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validate_key(dc_id, key):
    if type(dc_id) is not int or dc_id not in DC_ADDRESSES:
        raise AppError("Поддерживаются только обычные, не тестовые сессии Telegram.")
    if not isinstance(key, bytes) or len(key) != 256 or not any(key):
        raise AppError("Файл не содержит действующего ключа сессии. Войдите по номеру телефона.")
    return dc_id, key


def read_session(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".session":
        raise AppError("Выберите обычный файл .session, созданный Telethon.")
    if not 100 <= path.stat().st_size <= MAX_SESSION_BYTES:
        raise AppError("Размер .session должен быть от 100 байт до 32 МБ.")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise AppError("Сессия используется другой программой. Закройте её перед импортом; не удаляйте служебные файлы вручную.")
    try:
        # immutable=1 avoids writing journal/lock files next to the original.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
            db.set_progress_handler(lambda: 1, 100000)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
            if "sessions" not in tables or not {"server_address", "dc_id", "auth_key", "port"} <= columns:
                raise AppError("Это не формат Telethon .session. Pyrogram, StringSession и переименованные ZIP не поддерживаются; используйте вход по номеру.")
            rows = db.execute("SELECT dc_id,auth_key FROM sessions LIMIT 2").fetchall()
            if len(rows) != 1:
                raise AppError("В файле нет единственной сессии Telethon.")
            return validate_key(*rows[0])
    except sqlite3.Error:
        raise AppError("Не удалось прочитать .session: повреждённый или неподдерживаемый SQLite-файл.") from None


def write_session(path, dc_id, key):
    """Write a clean Telethon 1.40 SQLiteSession v7, without copying SQL objects."""
    validate_key(dc_id, key)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE version(version INTEGER PRIMARY KEY);
            INSERT INTO version VALUES(7);
            CREATE TABLE sessions(dc_id INTEGER PRIMARY KEY, server_address TEXT,
                                  port INTEGER, auth_key BLOB, takeout_id INTEGER);
            CREATE TABLE entities(id INTEGER PRIMARY KEY, hash INTEGER NOT NULL,
                                  username TEXT, phone INTEGER, name TEXT, date INTEGER);
            CREATE TABLE sent_files(md5_digest BLOB, file_size INTEGER, type INTEGER,
                                    id INTEGER, hash INTEGER, PRIMARY KEY(md5_digest,file_size,type));
            CREATE TABLE update_state(id INTEGER PRIMARY KEY, pts INTEGER, qts INTEGER,
                                      date INTEGER, seq INTEGER);
        """)
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,NULL)", (dc_id, DC_ADDRESSES[dc_id], 443, key))
        db.commit()


def validate_tdata_folder(folder):
    folder = Path(folder).expanduser()
    if folder.is_symlink() or not folder.is_dir():
        raise AppError("Выберите папку tdata, не архив и не ярлык.")
    entries = list(folder.iterdir())
    keys = [p for p in entries if re.fullmatch(r"key_data[s01]?", p.name)]
    if not keys:
        raise AppError("В выбранной папке нет key_data. Выберите саму папку tdata, а не папку Telegram Desktop.")
    relevant = keys + [p for p in entries if re.fullmatch(r"[A-Fa-f0-9]{16}[s01]?", p.name)]
    for path in relevant:
        if path.is_symlink():
            raise AppError("Для импорта tdata нужны обычные файлы и папки, не символические ссылки.")
        if path.is_file() and path.stat().st_size > 8 * 1024 * 1024:
            raise AppError("Служебный файл tdata слишком большой или повреждён.")
        if path.is_dir():
            for item in path.glob("map*"):
                if item.is_symlink() or (item.is_file() and item.stat().st_size > 8 * 1024 * 1024):
                    raise AppError("Некорректный служебный файл tdata.")
    return folder.resolve()


def _tdata_worker(connection, folder, passcode):
    """Keep opentele's Telethon monkey-patching out of the application process."""
    try:
        from opentele.td import TDesktop
        from opentele.api import API
        desktop = TDesktop(folder, passcode=passcode)
        if not desktop.isLoaded() or not desktop.accounts:
            raise ValueError("No account")
        if len(desktop.accounts) > 10:
            raise ValueError("Unexpected number of accounts")
        config = {"api_id": API.TelegramDesktop.api_id, "api_hash": API.TelegramDesktop.api_hash}
        # Decode locally; no QR login, connect(), device randomization or network.
        rows = [(int(a.MainDcId), bytes(a.authKey.key), config) for a in desktop.accounts if a.isLoaded()]
        if not rows:
            raise ValueError("No loaded account")
        connection.send(("ok", rows))
    except ImportError:
        connection.send(("dependency", None))
    except Exception:
        # Exception strings may contain filesystem paths or decrypted information.
        connection.send(("format", None))
    finally:
        connection.close()


def read_tdata(folder, passcode=""):
    folder = validate_tdata_folder(folder)
    if not isinstance(passcode, str) or len(passcode) > 256:
        raise AppError("Локальный код-пароль tdata: не более 256 символов.")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_tdata_worker, args=(sender, str(folder), passcode), daemon=True)
    try:
        process.start()
        sender.close()
        if not receiver.poll(30):
            raise AppError("Чтение tdata заняло слишком много времени. Закройте Telegram Desktop и попробуйте ещё раз.")
        status, rows = receiver.recv()
        if status == "dependency":
            raise AppError("Модуль tdata не установлен. Для исходников: pip install .[tdata]. В полной Windows-сборке он включён.", 503, "dependency")
        if status != "ok":
            raise AppError("Не удалось прочитать tdata. Проверьте локальный код-пароль и целостность папки. Новые форматы Telegram Desktop могут быть несовместимы с opentele; запасной вариант — вход по номеру.")
        return [(validate_key(dc, key), TelegramGateway._validate_config(config)) for dc, key, config in rows]
    except (EOFError, OSError):
        raise AppError("Не удалось запустить локальный модуль tdata. Используйте вход по номеру телефона.") from None
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            process.join(0.5)
            if process.is_alive():
                process.terminate()
                process.join(2)
            process.close()


class Vault:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "accounts.json"
        self.lock = threading.RLock()
        self.data = {"version": 1, "accounts": [], "proxies": []}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if loaded["version"] != 1 or not isinstance(loaded["accounts"], list) or not isinstance(loaded["proxies"], list):
                    raise ValueError
                self.data = loaded
                for item in self.data["accounts"]:
                    self.account_dir(item["id"])
            except (ValueError, TypeError, KeyError):
                raise AppError("Не удалось прочитать список аккаунтов. Не удаляйте папку данных: восстановите accounts.json из резервной копии.", 500) from None
        elif (self.directory / "config.json").exists() or (self.directory / "telegram.session").exists():
            entry = {"id": "legacy", "label": "Аккаунт из прежней версии", "kind": "login", "proxy_id": "", "fingerprint": ""}
            if (self.directory / "telegram.session").exists():
                try:
                    _, key = read_session(self.directory / "telegram.session")
                    entry["fingerprint"] = hashlib.sha256(key).hexdigest()
                except AppError:
                    pass  # The normal connection flow can repair an old session.
            self.data["accounts"].append(entry)
            self.save()
        else:
            self.save()  # Do not remigrate a deliberately removed legacy account.

    def save(self):
        private_json(self.path, self.data)

    def account_dir(self, account_id):
        if account_id == "legacy":
            return self.directory
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{32}", account_id):
            raise AppError("Некорректный аккаунт.", 404)
        return self.directory / "accounts" / account_id

    def entry(self, account_id):
        with self.lock:
            self.account_dir(account_id)
            for item in self.data["accounts"]:
                if item["id"] == account_id:
                    return copy.deepcopy(item)
        raise AppError("Аккаунт удалён или не найден. Выберите существующий аккаунт.", 404, "account_missing")

    def entries(self):
        with self.lock:
            return copy.deepcopy(self.data["accounts"])

    def add(self, label, kind="login", config=None, session=None):
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise AppError("Название аккаунта: от 1 до 80 символов.")
        if config is not None:
            config = TelegramGateway._validate_config(config)
        if session is not None:
            validate_key(*session)
        fingerprint = hashlib.sha256(session[1]).hexdigest() if session else ""
        with self.lock:
            for entry in self.data["accounts"]:
                if fingerprint and entry.get("fingerprint") == fingerprint:
                    raise AppError("Эта сессия уже добавлена. Дубликат не создан.", 409, "duplicate")
            account_id = uuid.uuid4().hex
            directory = self.account_dir(account_id)
            directory.mkdir(parents=True, mode=0o700)
            entry = {"id": account_id, "label": label.strip(), "kind": kind, "proxy_id": "", "fingerprint": fingerprint}
            try:
                if config is not None:
                    private_json(directory / "config.json", config)
                if session is not None:
                    write_session(directory / "telegram.session", *session)
                self.data["accounts"].append(entry)
                self.save()
            except BaseException:
                self.data["accounts"] = [e for e in self.data["accounts"] if e["id"] != account_id]
                shutil.rmtree(directory, ignore_errors=True)
                raise
            return account_id

    def update(self, account_id, **fields):
        with self.lock:
            self.entry(account_id)
            previous = copy.deepcopy(self.data)
            try:
                for item in self.data["accounts"]:
                    if item["id"] == account_id:
                        item.update(fields)
                self.save()
            except BaseException:
                self.data = previous
                raise

    def remove(self, account_id):
        with self.lock:
            self.entry(account_id)
            previous = copy.deepcopy(self.data)
            self.data["accounts"] = [e for e in self.data["accounts"] if e["id"] != account_id]
            try:
                self.save()
            except BaseException:
                self.data = previous
                raise
            directory = self.account_dir(account_id)
            if account_id == "legacy":
                for name in ("telegram.session", "telegram.session-journal", "telegram.session-wal", "telegram.session-shm", "config.json"):
                    (directory / name).unlink(missing_ok=True)
            else:
                shutil.rmtree(directory)

    def import_sessions(self, paths, config):
        config = TelegramGateway._validate_config(config)
        if not 1 <= len(paths) <= 50:
            raise AppError("Выберите от 1 до 50 файлов .session за один раз.")
        results = []
        for raw in paths:
            path = Path(raw)
            try:
                session = read_session(path)
                account_id = self.add(path.stem[:80] or "Импорт session", "session", config, session)
                results.append({"name": path.name, "id": account_id, "ok": True})
            except (OSError, AppError) as exc:
                results.append({"name": path.name, "ok": False, "error": str(exc) if isinstance(exc, AppError) else "Не удалось прочитать или сохранить файл."})
        return {"items": results}

    def import_tdata(self, folder, passcode=""):
        records = read_tdata(folder, passcode)
        results = []
        for index, (session, config) in enumerate(records, 1):
            label = "Telegram Desktop · " + str(index)
            try:
                account_id = self.add(label, "tdata", config, session)
                results.append({"name": label, "id": account_id, "ok": True})
            except AppError as exc:
                results.append({"name": label, "ok": False, "error": str(exc)})
        return {"items": results}


class AccountGateway:
    """Distinct clients per account; the job account never follows UI selection."""
    def __init__(self, vault):
        self.vault = vault
        self.clients = {}
        self.selected = next((e["id"] for e in vault.entries()), "")
        self.worker = None
        self._manager_lock = asyncio.Lock()

    def get(self, account_id):
        entry = self.vault.entry(account_id)
        if account_id not in self.clients:
            from .proxies import account_proxy
            client = TelegramGateway(self.vault.account_dir(account_id))
            client.proxy = account_proxy(self.vault, entry["proxy_id"])
            self.clients[account_id] = client
        return self.clients[account_id]

    def snapshot(self):
        state = self.get(self.selected).snapshot() if self.selected else dict(EMPTY_STATE)
        return {**state, "account_id": self.selected}

    def accounts(self):
        return [{"id": e["id"], "label": e["label"], "kind": e["kind"], "proxy_id": e["proxy_id"],
                 **self.get(e["id"]).snapshot()} for e in self.vault.entries()]

    async def initialize(self):
        # No eager login for every imported account. User checks it or starts a job.
        pass

    async def auth(self, action, data):
        account_id = data.get("account_id") or self.selected
        async with self._manager_lock:
            gateway = self.get(account_id)
            entry = self.vault.entry(account_id)
            if action == "logout" and entry["kind"] in ("session", "tdata"):
                raise AppError("Импорт переиспользует исходную сессию. Для локального отключения нажмите «Удалить из Parser»; отзыв сессии в Telegram может отключить и исходное приложение.", 409)
            result = await gateway.auth(action, data)
            if result.get("authorized"):
                self.vault.update(account_id, label=result.get("account") or entry["label"])
                path = self.vault.account_dir(account_id) / "telegram.session"
                if path.exists():
                    try:
                        _, key = read_session(path)
                        self.vault.update(account_id, fingerprint=hashlib.sha256(key).hexdigest())
                    except AppError:
                        pass
            return {**result, "account_id": account_id}

    async def prepare_job(self, job):
        # Legacy jobs are pinned to legacy, never to whichever row is selected.
        account_id = job["options"].get("account_id", "legacy")
        gateway = self.get(account_id)
        self.worker = gateway
        try:
            state = await gateway.auth("refresh", {})
        except AppError as exc:
            if exc.code == "rate_limit" and exc.retry_after:
                raise RateLimited(exc.retry_after) from None
            raise
        if not state["authorized"]:
            raise AppError("Аккаунт этой задачи не подключён. Проверьте его во вкладке «Аккаунты».", 409)

    async def resolve(self, reference):
        return await self.worker.resolve(reference)

    async def latest_id(self, source):
        return await self.worker.latest_id(source)

    async def messages(self, *args):
        async for message in self.worker.messages(*args):
            yield message

    async def reset(self, account_id):
        if account_id in self.clients:
            await self.clients.pop(account_id).close()

    async def close(self):
        for client in list(self.clients.values()):
            await client.close()
        self.clients.clear()
