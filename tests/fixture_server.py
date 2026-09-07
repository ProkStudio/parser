"""Offline tutorial fixture. Launched only by tests/browser.mjs, never by Parser."""
import asyncio
import json
import secrets
import tempfile
from pathlib import Path

from parser_app.domain import job_spec
from parser_app.server import Application, LocalServer
from parser_app.store import Store
from tests.support import FakeGateway, FakeRuntime


def main():
    with tempfile.TemporaryDirectory(prefix="parser-tutorial-") as directory:
        store = Store(Path(directory) / "fixture.sqlite3")
        gateway = FakeGateway(total=35, authorized=False)
        _, options = job_spec({"sources": ["@example_channel"], "limit": 1000})
        source = asyncio.run(gateway.resolve("@example_channel"))
        first = store.enqueue(["@example_channel"], options)[0]
        store.claim()
        store.prepare(first, source, 35)
        messages = [gateway.message(n) for n in range(1, 36)]
        messages[-1]["text"] = "Учебный пример. Вакансия: дизайнер интерфейсов. Удалённая работа, продуктовая команда. Это синтетическое сообщение для инструкции, не публикация из реального канала."
        messages[-2]["text"] = "Учебный пример. Новый материал о дизайн-системах: компоненты, типографика и доступность. Сохраните выборку в CSV или JSON, чтобы работать с ней дальше."
        messages[-3]["text"] = '<script>window.parserInjected=true</script> Учебный тест безопасного отображения текста.'
        store.checkpoint(first, source["id"], 35, 35, messages)
        store.state(first, "completed", completion="end")
        paused = store.enqueue(["@example_archive"], {**options, "include": ["дизайн"]})[0]
        store.state(paused, "paused")
        queued = store.enqueue(["@example_news"], options)[0]
        store.state(queued, "failed", error="Учебный пример: соединение было прервано. Проверьте сеть и повторите сбор.")
        token = secrets.token_urlsafe(32)
        app = Application(store, FakeRuntime(gateway), token, "Учебный пример · данные хранятся локально")
        server = LocalServer(("127.0.0.1", 0), app)
        print(json.dumps({"port": server.server_port, "key": token}), flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
