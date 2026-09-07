"""Loopback-only HTTP API with a per-launch bearer capability and strict origin checks."""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .domain import VERSION, AppError, bounded_int, export_rows, job_spec

STATIC = Path(__file__).with_name("static")
ASSETS = {"/": ("index.html", "text/html"), "/static/app.js": ("app.js", "text/javascript"), "/static/styles.css": ("styles.css", "text/css")}

# Only the three current UI assets are served. Help is a compact built-in dialog.


class Export:
    def __init__(self, rows, fmt):
        self.rows, self.fmt = rows, fmt


class Application:
    def __init__(self, store, runtime, token, directory):
        self.store, self.runtime, self.token, self.directory = store, runtime, token, directory
        self.mutations = threading.Lock()
        self.request_shutdown = None

    def dispatch(self, method, path, query, data):
        def arg(key, default=""):
            values = query.get(key, [default])
            if len(values) != 1:
                raise AppError("Параметр запроса не должен повторяться.")
            return values[0]

        if method == "GET":
            if path == "/api/status":
                return {"version": VERSION, "telegram": self.runtime.gateway.snapshot(), "stats": self.store.stats(), "data_dir": str(self.directory), "cooldown_until": self.store.cooldown()}
            if path == "/api/sources":
                return {"items": self.store.sources()}
            if path == "/api/jobs":
                return {"items": self.store.jobs(bounded_int(arg("offset", "0"), "Смещение", 0, 1000000))}
            if path == "/api/messages":
                return self.store.messages(arg("q"), arg("source"), arg("page", "1"), arg("page_size", "30"))
            if path == "/api/export":
                fmt = arg("format", "csv")
                if fmt not in ("csv", "json", "jsonl"):
                    raise AppError("Формат экспорта: csv, json или jsonl.")
                self.store._where(arg("q"), arg("source"))  # Validate before sending headers.
                return Export(self.store.iter_messages(arg("q"), arg("source")), fmt)
        with self.mutations:
            if method == "POST" and path == "/api/shutdown":
                if data.get("confirm") != "quit" or self.request_shutdown is None:
                    raise AppError("Необходимо подтверждение завершения приложения.")
                self.request_shutdown()
                return {"ok": True}
            if method == "POST" and path == "/api/jobs":
                if not self.runtime.gateway.snapshot()["authorized"]:
                    raise AppError("Сначала подключите Telegram во вкладке «Подключение».", 409, "not_connected")
                sources, options = job_spec(data)
                return {"ids": self.store.enqueue(sources, options)}
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/(pause|cancel|resume)", path)
            if method == "POST" and match:
                if match[2] == "resume" and not self.runtime.gateway.snapshot()["authorized"]:
                    raise AppError("Сначала подключите Telegram.", 409)
                return self.store.control(match[1], match[2])
            if method == "POST" and path.startswith("/api/auth/"):
                action = path.rsplit("/", 1)[1]
                if action not in ("configure", "send_code", "verify_code", "verify_password", "refresh", "logout"):
                    raise AppError("Действие не найдено.", 404)
                if action in ("configure", "logout") and self.store.busy():
                    raise AppError("Сначала поставьте активные задачи на паузу или отмените их.", 409)
                try:
                    return self.runtime.call(self.runtime.gateway.auth(action, data))
                except AppError as exc:
                    if exc.code == "rate_limit" and exc.retry_after:
                        self.store.set_cooldown(exc.retry_after)
                    raise
            if method == "DELETE" and path == "/api/history":
                if data.get("confirm") != "delete-local-history":
                    raise AppError("Необходимо явное подтверждение удаления.")
                self.store.clear()
                return {"ok": True}
        raise AppError("Ресурс не найден.", 404, "not_found")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, app):
        if address[0] != "127.0.0.1":
            raise ValueError("Only IPv4 loopback is permitted")
        self.app = app
        super().__init__(address, Handler)
        self.app.request_shutdown = self.schedule_shutdown

    def schedule_shutdown(self):
        timer = threading.Timer(0.3, self.shutdown)
        timer.daemon = True
        timer.start()

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Parser"
    sys_version = ""

    def log_message(self, *args):
        pass  # Do not log URLs, message search text, login codes or tokens.

    def _headers(self, status, content_type, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or "json" in content_type else ""))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if length is not None:
            self.send_header("Content-Length", str(length))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status, payload, extra=None):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json", len(content), extra)
        self.wfile.write(content)

    def _guard(self, api):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if self.client_address[0] != "127.0.0.1" or host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise AppError("Недопустимый адрес запроса.", 403, "origin")
        if self.headers.get("Origin") not in (None, "http://" + host):
            raise AppError("Запрос из другого источника запрещён.", 403, "origin")
        if api:
            received = self.headers.get("Authorization", "").encode("utf-8")
            expected = ("Bearer " + self.server.app.token).encode("utf-8")
            if not hmac.compare_digest(received, expected):
                raise AppError("Откройте Parser по ссылке из приложения: ключ этой вкладки отсутствует или устарел.", 401, "session")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise AppError("Потоковое тело запроса не поддерживается.")
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isascii() or not raw_length.isdigit():
            raise AppError("Не задан корректный размер запроса.", 411)
        length = int(raw_length)
        if length > 65536:
            raise AppError("Слишком большой запрос.", 413)
        if self.headers.get_content_type() != "application/json":
            raise AppError("Ожидается application/json.", 415)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            raise AppError("Некорректное JSON-тело запроса.") from None

    def _handle(self):
        streaming = False
        try:
            parts = urlsplit(self.path)
            self._guard(parts.path.startswith("/api/"))
            if self.command == "GET" and parts.path in ASSETS:
                name, mime = ASSETS[parts.path]
                try:
                    content = (STATIC / name).read_bytes()
                except FileNotFoundError:
                    raise AppError("Файл этой сборки не найден.", 404, "not_found") from None
                self._headers(200, mime, len(content))
                self.wfile.write(content)
                return
            data = self._body() if self.command in ("POST", "DELETE") else {}
            try:
                query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)
            except ValueError:
                raise AppError("Слишком много параметров запроса.") from None
            result = self.server.app.dispatch(self.command, parts.path, query, data)
            if isinstance(result, Export):
                mime = {"csv": "text/csv", "json": "application/json", "jsonl": "application/x-ndjson"}[result.fmt]
                self._headers(200, mime, extra={"Content-Disposition": f'attachment; filename="parser-messages.{result.fmt}"'})
                streaming = True
                try:
                    for chunk in export_rows(result.rows, result.fmt):
                        self.wfile.write(chunk)
                finally:
                    result.rows.close()
            else:
                self._json(200, result)
        except AppError as exc:
            self.close_connection = True
            self._json(exc.status, {"error": str(exc), "code": exc.code, "retry_after": exc.retry_after}, {"Connection": "close", **({"Retry-After": str(exc.retry_after)} if exc.retry_after else {})})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            self.close_connection = True
            logging.getLogger("parser").error("HTTP failure: %s", type(exc).__name__)
            if not streaming:
                self._json(500, {"error": "Внутренняя ошибка. Попробуйте снова; подробности типа ошибки — в локальном журнале.", "code": "internal"}, {"Connection": "close"})

    do_GET = do_POST = do_DELETE = _handle
