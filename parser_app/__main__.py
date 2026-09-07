"""Desktop by default. Browser/headless modes require an explicit command-line flag."""
from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import re
import secrets
import sys
import threading
import webbrowser
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.request import Request, urlopen

from .accounts import AccountGateway, Vault, private_json
from .domain import VERSION, AppError
from .engine import Runtime
from .server import ASSETS, LocalServer
from .store import Store
from .workspace import DesktopApplication


def default_directory():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Parser"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Parser"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "parser"


@contextmanager
def instance_lock(directory):
    stream = (directory / "instance.lock").open("a+b")
    acquired = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise AppError("Parser уже запущен с этой папкой данных.", 409, "already_running") from None
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def reopen(directory, browser=False):
    try:
        url = json.loads((directory / "launch.json").read_text(encoding="utf-8"))["url"]
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/#key=[A-Za-z0-9_-]{40,64}", url):
            return False
        if browser:
            webbrowser.open(url)
            return True
        origin, token = url.split("/#key=")
        request = Request(origin + "/api/window/focus", data=b"{}", method="POST",
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return bool(json.load(response).get("ok"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def report_error(message):
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Parser", 0x10)


def smoke_test(server, directory, token, result):
    """Installed EXE smoke check without Telegram accounts or network requests."""
    try:
        import telethon
        import webview
        import socks
        # Check tdata dependencies without monkey-patching the runtime's Telethon.
        from importlib.util import find_spec
        if find_spec("opentele") is None:
            raise RuntimeError("Missing tdata dependency")
        origin = "http://127.0.0.1:" + str(server.server_port)
        for path in ASSETS:
            with urlopen(origin + path, timeout=5) as response:
                if response.status != 200 or not response.read(16):
                    raise RuntimeError("Missing packaged asset")
        for path in ("/api/status", "/api/accounts", "/api/proxies"):
            request = Request(origin + path, headers={"Authorization": "Bearer " + token})
            with urlopen(request, timeout=5) as response:
                data = json.load(response)
            if path == "/api/status" and data["version"] != VERSION:
                raise RuntimeError("Unexpected version")
        result.update(ok=True, version=VERSION, assets=len(ASSETS), telegram_version=telethon.__version__)
    except Exception as exc:
        result.update(ok=False, error=type(exc).__name__)
    finally:
        private_json(directory / "smoke-test.json", result)
        server.shutdown()


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Parser — локальное приложение Telegram")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--data-dir", type=Path, default=default_directory())
    parser.add_argument("--port", type=int, default=0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true", help="Явно открыть браузер вместо окна; импорт файлов недоступен")
    mode.add_argument("--no-browser", action="store_true", help="Сервер без окна, для разработки")
    mode.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--window-smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Порт должен быть в диапазоне 0–65535")
    directory = args.data_dir.expanduser().resolve()
    result = {}
    try:
        if os.name != "nt":
            os.umask(0o077)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with instance_lock(directory):
            handler = RotatingFileHandler(directory / "parser.log", maxBytes=1000000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger = logging.getLogger("parser")
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            runtime = server = None
            try:
                store = Store(directory / "parser.sqlite3")
                store.recover()
                token = secrets.token_urlsafe(32)
                runtime = Runtime(store, AccountGateway(Vault(directory)))
                app = DesktopApplication(store, runtime, token, directory)
                server = LocalServer(("127.0.0.1", args.port), app)
                url = "http://127.0.0.1:" + str(server.server_port) + "/#key=" + token
                private_json(directory / "launch.json", {"url": url})
                if args.browser or args.no_browser:
                    if sys.stdout is not None:
                        print(f"Parser {VERSION}\nЛичная локальная ссылка (не передавайте): {url}", flush=True)
                    if args.browser:
                        webbrowser.open(url)
                if not (args.browser or args.no_browser or args.smoke_test):
                    from .native import run_window
                    run_window(server, url, smoke=args.window_smoke)
                else:
                    if args.smoke_test:
                        timer = threading.Timer(0.3, smoke_test, args=(server, directory, token, result))
                        timer.daemon = True
                        timer.start()
                    server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
            finally:
                if server is not None:
                    server.server_close()
                if runtime is not None:
                    runtime.close()
                (directory / "launch.json").unlink(missing_ok=True)
                logger.removeHandler(handler)
                handler.close()
    except AppError as exc:
        if exc.code == "already_running" and not (args.no_browser or args.smoke_test or args.window_smoke) and reopen(directory, args.browser):
            return 0
        report_error(str(exc))
        return 1
    except OSError:
        report_error("Не удалось запустить Parser. Проверьте доступ к папке данных и свободный порт.")
        return 1
    if args.window_smoke:
        try:
            return 0 if json.loads((directory / "window-smoke.json").read_text())["ok"] else 1
        except (OSError, ValueError, KeyError):
            return 1
    return 0 if not args.smoke_test or result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
