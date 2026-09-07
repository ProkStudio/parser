"""Validation and read-only parsing rules; no network dependencies."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

VERSION = "0.2.0"
ACTIVE = ("queued", "running", "waiting")
MEDIA = ("any", "text", "photo", "video", "document", "audio", "other")


class AppError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "validation", retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after


class RateLimited(Exception):
    def __init__(self, seconds: int):
        self.seconds = max(1, int(seconds))
        super().__init__("Telegram rate limit")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def source_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise AppError("Укажите @username или ссылку на канал / группу.")
    value = value.strip()
    if re.fullmatch(r"-100\d{5,16}", value):
        return value
    if value.startswith(("t.me/", "telegram.me/")):
        value = "https://" + value
    if "://" in value:
        try:
            parsed = urlparse(value)
        except ValueError:
            raise AppError("Некорректная ссылка Telegram.") from None
        if parsed.scheme != "https" or parsed.netloc.lower() not in ("t.me", "telegram.me", "www.t.me") or parsed.query or parsed.fragment:
            raise AppError("Поддерживаются только прямые HTTPS-ссылки Telegram.")
        parts = parsed.path.strip("/").split("/")
        if parts[0] == "s":
            parts = parts[1:]
        if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
            raise AppError("Нужна ссылка на канал, а не приглашение или служебная ссылка.")
        value = parts[0]
    value = value.removeprefix("@")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{3,31}", value) or value.lower() in ("joinchat", "share", "proxy", "socks", "login"):
        raise AppError("Некорректный username канала. Приватные группы: используйте их ID -100… при наличии доступа.")
    return "@" + value.lower()


def bounded_int(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or len(str(value)) > 20 or not re.fullmatch(r"[0-9]+", str(value)):
        raise AppError(f"{name}: требуется целое число.")
    number = int(value)
    if not low <= number <= high:
        raise AppError(f"{name}: допустимо от {low} до {high}.")
    return number


def job_spec(payload: dict) -> tuple[list[str], dict]:
    raw = payload.get("sources", [])
    if isinstance(raw, str):
        raw = [item.strip() for item in re.split(r"[,\n]", raw) if item.strip()]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise AppError("Добавьте от 1 до 20 источников, по одному на строку.")
    sources = list(dict.fromkeys(source_key(item) for item in raw))
    options = {"limit": bounded_int(payload.get("limit", 1000), "Лимит просмотра", 1, 50000)}
    for key in ("include", "exclude"):
        words = payload.get(key, [])
        if isinstance(words, str):
            words = re.split(r"[,\n]", words)
        if not isinstance(words, list) or len(words) > 30 or any(not isinstance(w, str) or len(w) > 100 for w in words):
            raise AppError("В каждом фильтре допустимо до 30 фраз длиной до 100 символов.")
        options[key] = list(dict.fromkeys(w.strip().casefold() for w in words if w.strip()))
    for key in ("date_from", "date_to"):
        raw_date = payload.get(key) or ""
        try:
            if raw_date and (not isinstance(raw_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date)):
                raise ValueError
            parsed_date = date.fromisoformat(raw_date) if raw_date else None
            if parsed_date and parsed_date.year < 1970:
                raise ValueError
            options[key] = parsed_date.isoformat() if parsed_date else ""
        except (ValueError, TypeError):
            raise AppError("Дата должна быть в формате ГГГГ-ММ-ДД, не раньше 1970 года.") from None
    if options["date_from"] and options["date_to"] and options["date_from"] > options["date_to"]:
        raise AppError("Начало периода не может быть позже окончания.")
    options["media"] = payload.get("media", "any")
    if options["media"] not in MEDIA:
        raise AppError("Неизвестный тип сообщения.")
    options["incremental"] = payload.get("incremental", True)
    if not isinstance(options["incremental"], bool):
        raise AppError("Параметр incremental должен быть логическим.")
    return sources, options


def matches(message: dict, options: dict) -> bool:
    if message.get("service") or not (message["text"] or message["media"] != "text"):
        return False
    day = message["sent_at"][:10]  # Gateway normalizes timestamps to UTC.
    if options["date_from"] and day < options["date_from"]:
        return False
    if options["date_to"] and day > options["date_to"]:
        return False
    if options["media"] != "any" and options["media"] != message["media"]:
        return False
    text = message["text"].casefold()
    return (not options["include"] or any(w in text for w in options["include"])) and not any(w in text for w in options["exclude"])


def safe_csv(value):
    if isinstance(value, str) and (value.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def export_rows(rows, fmt: str):
    columns = ("source_id", "source_title", "message_id", "sent_at", "text", "media", "views", "link")
    if fmt == "csv":
        yield b"\xef\xbb\xbf"  # UTF-8 BOM for Excel on Windows.
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield buffer.getvalue().encode("utf-8")
        for row in rows:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([safe_csv(row.get(key, "")) for key in columns])
            yield buffer.getvalue().encode("utf-8")
    elif fmt in ("json", "jsonl"):
        if fmt == "json":
            yield b"["
        first = True
        for row in rows:
            content = json.dumps({key: row.get(key) for key in columns}, ensure_ascii=False).encode("utf-8")
            yield ((b"" if first else b",") + content) if fmt == "json" else content + b"\n"
            first = False
        if fmt == "json":
            yield b"]\n"
    else:
        raise AppError("Формат экспорта: csv, json или jsonl.")
