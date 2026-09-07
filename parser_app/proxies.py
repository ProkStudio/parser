"""Explicit SOCKS5/HTTP CONNECT configuration; no rotation or direct fallback."""
from __future__ import annotations

import copy
import ipaddress
import re
import time
import uuid

from .domain import AppError, bounded_int


def validate_proxy(data):
    if not isinstance(data, dict):
        raise AppError("Некорректные настройки прокси.")
    kind = data.get("type", "socks5")
    if kind not in ("socks5", "http"):
        raise AppError("Поддерживаются SOCKS5 и HTTP CONNECT. MTProto-прокси не поддерживаются.")
    host = data.get("host", "")
    if not isinstance(host, str) or not 1 <= len(host.strip()) <= 253:
        raise AppError("Укажите IP-адрес или имя сервера прокси, без схемы и порта.")
    host = host.strip().strip("[]")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise AppError("Некорректное имя сервера прокси.") from None
        if not re.fullmatch(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-") for part in host.split(".")):
            raise AppError("Введите только адрес сервера; логин, пароль и порт указываются отдельно.")
    port = bounded_int(data.get("port", ""), "Порт", 1, 65535)
    values = {"type": kind, "host": host, "port": port}
    for name in ("username", "password"):
        value = data.get(name, "")
        if not isinstance(value, str) or len(value.encode("utf-8")) > 255 or any(c in value for c in "\r\n\0"):
            raise AppError("Логин и пароль прокси: до 255 байт, без переносов строк.")
        values[name] = value
    if bool(values["username"]) != bool(values["password"]):
        raise AppError("Укажите и логин, и пароль либо оставьте оба поля пустыми.")
    label = data.get("label") or f"{host}:{port}"
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
        raise AppError("Название прокси: от 1 до 80 символов.")
    return {**values, "label": label.strip()}


def proxy_entry(vault, proxy_id):
    with vault.lock:
        for proxy in vault.data["proxies"]:
            if proxy["id"] == proxy_id:
                return copy.deepcopy(proxy)
    raise AppError("Прокси не найден. Соединение напрямую не выполнялось.", 404)


def list_proxies(vault):
    with vault.lock:
        return [{key: p[key] for key in ("id", "label", "type", "host", "port")} | {"has_auth": bool(p["username"])} for p in vault.data["proxies"]]


def add_proxy(vault, data):
    proxy = {**validate_proxy(data), "id": uuid.uuid4().hex}
    with vault.lock:
        for item in vault.data["proxies"]:
            if all(item[key] == proxy[key] for key in ("type", "host", "port", "username", "password")):
                raise AppError("Такой прокси уже добавлен.", 409)
        previous = copy.deepcopy(vault.data)
        try:
            vault.data["proxies"].append(proxy)
            vault.save()
        except BaseException:
            vault.data = previous
            raise
    return {"id": proxy["id"]}


def delete_proxy(vault, proxy_id):
    with vault.lock:
        proxy_entry(vault, proxy_id)
        if any(e["proxy_id"] == proxy_id for e in vault.data["accounts"]):
            raise AppError("Сначала отключите этот прокси у аккаунтов, которым он назначен.", 409)
        previous = copy.deepcopy(vault.data)
        try:
            vault.data["proxies"] = [p for p in vault.data["proxies"] if p["id"] != proxy_id]
            vault.save()
        except BaseException:
            vault.data = previous
            raise
    return {"ok": True}


def connection_args(proxy):
    try:
        import socks
    except ImportError:
        raise AppError("Не установлен PySocks. Переустановите зависимости приложения.", 503, "dependency") from None
    return {"proxy_type": socks.SOCKS5 if proxy["type"] == "socks5" else socks.HTTP,
            "addr": proxy["host"], "port": proxy["port"], "rdns": True,
            "username": proxy["username"] or None, "password": proxy["password"] or None}


def account_proxy(vault, proxy_id):
    # Do not import PySocks merely to list accounts; Telethon accepts type strings.
    if not proxy_id:
        return None
    proxy = proxy_entry(vault, proxy_id)
    return {"proxy_type": proxy["type"], "addr": proxy["host"], "port": proxy["port"],
            "rdns": True, "username": proxy["username"] or None, "password": proxy["password"] or None}


def check_proxy(vault, proxy_id):
    proxy = proxy_entry(vault, proxy_id)
    options = connection_args(proxy)
    import socks
    started = time.monotonic()
    try:
        with socks.socksocket() as connection:
            connection.set_proxy(**options)
            connection.settimeout(8)
            # Fixed Telegram endpoint, not an arbitrary URL sent by the front end.
            connection.connect(("149.154.167.51", 443))
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000),
                "message": "Туннель до Telegram доступен. Это не проверка авторизации аккаунта."}
    except OSError:
        raise AppError("Прокси не установил туннель до Telegram. Проверьте адрес, порт и учётные данные. Прямое подключение не использовалось.", 503, "proxy") from None
