"""Synthetic offline UI fixture only. Not used by the application launcher."""
import asyncio
import json
import secrets
import tempfile
from pathlib import Path

from parser_app.accounts import AccountGateway, Vault, private_json
from parser_app.domain import job_spec
from parser_app.server import LocalServer
from parser_app.store import Store
from parser_app.workspace import DesktopApplication
from tests.support import FakeGateway, FakeRuntime

class FixtureGateway(AccountGateway):
    def get(self, account_id):
        self.vault.entry(account_id)
        if account_id not in self.clients:
            self.clients[account_id]=FakeGateway(total=35,authorized=False)
            config=self.vault.account_dir(account_id)/'config.json'
            if config.exists():
                self.clients[account_id].config=json.loads(config.read_text())
                self.clients[account_id].state.update(configured=True,step='phone')
        return self.clients[account_id]
    async def auth(self, action, data):
        result=await super().auth(action,data)
        if action=='configure':
            account_id=data.get('account_id') or self.selected
            private_json(self.vault.account_dir(account_id)/'config.json',self.get(account_id).config)
        return result


def main():
    with tempfile.TemporaryDirectory(prefix="parser-ui-synthetic-") as directory:
        root=Path(directory);store=Store(root/"fixture.sqlite3")
        gateway=FixtureGateway(Vault(root));fake=FakeGateway(total=35)
        _,options=job_spec({"sources":["@example_channel"],"limit":1000})
        options["account_label"]="Учебный аккаунт"
        source=asyncio.run(fake.resolve("@example_channel"));first=store.enqueue(["@example_channel"],options)[0]
        store.claim();store.prepare(first,source,35)
        messages=[fake.message(n) for n in range(1,36)]
        messages[-1]["text"]="Учебный пример. Вакансия: дизайнер интерфейсов. Удалённая работа, продуктовая команда. Это синтетические данные, не публикация из реального канала."
        messages[-2]["text"]="Учебный пример. Новый материал о дизайн-системах: компоненты, типографика и доступность. Сохраните выборку в CSV или JSON."
        messages[-3]["text"]='<script>window.parserInjected=true</script> Учебный тест безопасного отображения.'
        store.checkpoint(first,source["id"],35,35,messages);store.state(first,"completed",completion="end")
        paused=store.enqueue(["@example_archive"],options)[0];store.state(paused,"paused")
        token=secrets.token_urlsafe(32)
        app=DesktopApplication(store,FakeRuntime(gateway),token,root)
        server=LocalServer(("127.0.0.1",0),app)
        print(json.dumps({"port":server.server_port,"key":token}),flush=True)
        try:server.serve_forever(poll_interval=.1)
        finally:server.server_close()

if __name__=="__main__":main()
