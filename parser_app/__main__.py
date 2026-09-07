"""Desktop launcher, single-instance handoff and packaged application smoke test."""
from __future__ import annotations

import argparse
import json
import logging
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

from .domain import VERSION, AppError
from .engine import Runtime
from .server import ASSETS, Application, LocalServer
from .store import Store
from .telegram import TelegramGateway


def default_directory():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Parser"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "parser"


def private_json(path, data):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


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


def reopen(directory):
    try:
        url = json.loads((directory / "launch.json").read_text(encoding="utf-8"))["url"]
        if re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/#key=[A-Za-z0-9_-]{40,64}", url):
            webbrowser.open(url)
            return True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return False


def report_error(message):
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    elif sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Parser", 0x10)


def smoke_test(server, directory, token, result):
    """Executed against the installed EXE by Windows CI; never calls Telegram."""
    try:
        import telethon  # Proves the frozen dependency is actually included.
        origin = "http://127.0.0.1:" + str(server.server_port)
        for path in ASSETS:
            with urlopen(origin + path, timeout=5) as response:
                if response.status != 200 or not response.read(16):
                    raise RuntimeError("Missing packaged asset")
        request = Request(origin + "/api/status", headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=5) as response:
            data = json.load(response)
        if data["version"] != VERSION or data["telegram"]["authorized"]:
            raise RuntimeError("Unexpected clean-start state")
        result.update(ok=True, version=VERSION, assets=len(ASSETS), telegram_version=telethon.__version__)
    except Exception as exc:
        result.update(ok=False, error=type(exc).__name__)
    finally:
        private_json(directory / "smoke-test.json", result)
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Parser — локальный сбор сообщений Telegram")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--data-dir", type=Path, default=default_directory())
    parser.add_argument("--port", type=int, default=0, help="Локальный порт; 0 — выбрать свободный")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
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
            runtime = None
            try:
                store = Store(directory / "parser.sqlite3")
                store.recover()
                token = secrets.token_urlsafe(32)
                runtime = Runtime(store, TelegramGateway(directory))
                server = LocalServer(("127.0.0.1", args.port), Application(store, runtime, token, directory))
                url = "http://127.0.0.1:" + str(server.server_port) + "/#key=" + token
                private_json(directory / "launch.json", {"url": url})
                if sys.stdout is not None and not args.smoke_test:
                    print(f"Parser {VERSION}\nЛокальные данные: {directory}\nЛичная ссылка (не передавайте её другим):\n{url}\nДля выхода: Ctrl+C", flush=True)
                if args.smoke_test:
                    timer = threading.Timer(0.3, smoke_test, args=(server, directory, token, result))
                    timer.daemon = True
                    timer.start()
                elif not args.no_browser:
                    webbrowser.open(url)
                try:
                    server.serve_forever(poll_interval=0.25)
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
            finally:
                if runtime is not None:
                    runtime.close()
                (directory / "launch.json").unlink(missing_ok=True)
                logger.removeHandler(handler)
                handler.close()
    except AppError as exc:
        if exc.code == "already_running" and not args.no_browser and not args.smoke_test and reopen(directory):
            return 0
        report_error(str(exc))
        return 1
    except OSError:
        report_error("Не удалось запустить Parser. Проверьте доступ к папке данных и свободный порт.")
        return 1
    return 0 if not args.smoke_test or result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
