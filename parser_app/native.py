"""Native pywebview shell. File contents never pass through JavaScript/HTTP."""
from __future__ import annotations

import hmac
import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .domain import AppError, export_rows
from .telegram import TelegramGateway


class NativeBridge:
    def __init__(self, app, origin):
        self._app = app
        self._origin = origin
        self._window = None
        self._dialog_lock = threading.Lock()
        self._smoke = False

    def _guard(self, token):
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self._app.token.encode()):
            raise AppError("Ключ окна устарел. Перезапустите Parser.", 403)
        if self._window is None:
            raise AppError("Окно приложения ещё не готово.", 409)
        url = self._window.get_current_url() or ""
        parts = urlsplit(url)
        if parts.scheme + "://" + parts.netloc != self._origin or parts.path != "/":
            raise AppError("Импорт разрешён только в локальном окне Parser.", 403)

    @staticmethod
    def _error(exc):
        if isinstance(exc, AppError):
            return {"error": str(exc)}
        logging.getLogger("parser").error("Native action failure: %s", type(exc).__name__)
        return {"error": "Не удалось выполнить локальное действие. Проверьте доступ к файлу и свободное место."}

    def window_ready(self, token, tabs):
        try:
            self._guard(token)
            if self._smoke:
                from .accounts import private_json
                private_json(Path(self._app.directory) / "window-smoke.json", {"ok": tabs == ["Аккаунты", "Прокси", "Задачи"]})
                threading.Timer(0.3, self._window.destroy).start()
            return {"ok": True}
        except Exception as exc:
            return self._error(exc)

    def import_accounts(self, token, kind, api_id="", api_hash="", passcode=""):
        try:
            self._guard(token)
            if kind not in ("session", "tdata"):
                raise AppError("Выберите .session или tdata.")
            config = TelegramGateway._validate_config({"api_id": api_id, "api_hash": api_hash}) if kind == "session" else None
            import webview
            if not self._dialog_lock.acquire(blocking=False):
                raise AppError("Закройте уже открытое окно выбора файла.", 409)
            try:
                if kind == "session":
                    paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                                           file_types=("Telethon session (*.session)",))
                else:
                    paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
                if not paths:
                    return {"cancelled": True}
                self._guard(token)
                with self._app.mutations:
                    self._app.require_idle()
                    vault = self._app.runtime.gateway.vault
                    return vault.import_sessions(paths, config) if kind == "session" else vault.import_tdata(paths[0], passcode)
            finally:
                self._dialog_lock.release()
        except Exception as exc:
            return self._error(exc)

    def export_messages(self, token, fmt, query="", source=""):
        try:
            self._guard(token)
            if fmt not in ("csv", "json", "jsonl"):
                raise AppError("Неизвестный формат экспорта.")
            self._app.store._where(query, source)
            import webview
            if not self._dialog_lock.acquire(blocking=False):
                raise AppError("Закройте уже открытое окно выбора файла.", 409)
            try:
                paths = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename="parser-messages." + fmt,
                                                       file_types=(f"{fmt.upper()} (*.{fmt})",))
                if not paths:
                    return {"cancelled": True}
                self._guard(token)
                destination = Path(paths[0] if not isinstance(paths, str) else paths)
                # A save dialog is explicit user approval, but never overwrite the vault itself.
                if destination.resolve().is_relative_to(Path(self._app.directory).resolve()):
                    raise AppError("Выберите папку за пределами служебной папки Parser, например «Документы».")
                if destination.suffix.lower() != "." + fmt:
                    destination = destination.with_name(destination.name + "." + fmt)
                    if destination.exists() and not self._window.create_confirmation_dialog("Заменить файл?", "Файл с выбранным расширением уже существует. Заменить его?"):
                        return {"cancelled": True}
                temp = destination.with_name(destination.name + ".parser-tmp")
                created = False
                try:
                    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    created = True
                    with os.fdopen(fd, "wb") as stream:
                        rows = self._app.store.iter_messages(query, source)
                        try:
                            for chunk in export_rows(rows, fmt):
                                stream.write(chunk)
                        finally:
                            rows.close()
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, destination)
                finally:
                    if created:
                        temp.unlink(missing_ok=True)
                return {"ok": True, "name": destination.name}
            finally:
                self._dialog_lock.release()
        except Exception as exc:
            return self._error(exc)


def run_window(server, url, smoke=False):
    try:
        import webview
    except ImportError:
        raise AppError("Не установлен оконный модуль. Выполните pip install .[desktop,tdata]. На Windows установите Microsoft Edge WebView2 Runtime. Браузер автоматически открываться не будет.", 503) from None
    import sys
    origin = url.split("/#", 1)[0]
    bridge = NativeBridge(server.app, origin)
    window = webview.create_window("Parser", url, js_api=bridge, width=1120, height=780,
                                   min_size=(760, 560), text_select=True, background_color="#141414",
                                   confirm_close=not smoke,
                                   localization={"global.quitConfirmation": "Завершить Parser? Текущая задача сохранит позицию и останется на паузе."})
    bridge._window = window
    bridge._smoke = smoke
    server.app.native = True

    def focus():
        window.restore()
        window.show()

    server.app.focus_window = focus
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
    worker.start()
    # API shutdown must close the native window, not merely stop its local server.
    server.app.request_shutdown = lambda: threading.Timer(0.3, window.destroy).start()

    def on_ready():
        if smoke:
            # Front-end calls window_ready over the real bridge. No eval/CSP bypass.
            import time
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if (Path(server.app.directory) / "window-smoke.json").exists():
                    return
                time.sleep(0.2)
            window.destroy()
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.start(on_ready, gui="edgechromium" if sys.platform == "win32" else None, debug=False, private_mode=True)
    except Exception:
        raise AppError("Не удалось открыть окно Parser. На Windows требуется Microsoft Edge WebView2 Runtime; на Linux — графическая сессия и зависимости pywebview[qt]. Браузер не запускался.", 503) from None
    finally:
        server.app.focus_window = None
        server.shutdown()
        worker.join(timeout=5)
