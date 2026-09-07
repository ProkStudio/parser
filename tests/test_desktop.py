"""Offline security and regression tests; all keys are synthetic random test bytes."""
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from parser_app.accounts import AccountGateway, Vault, read_session, write_session, validate_tdata_folder
from parser_app.domain import AppError, job_spec
from parser_app.engine import Engine
from parser_app.native import NativeBridge
from parser_app.proxies import add_proxy, account_proxy, delete_proxy, list_proxies, validate_proxy
from parser_app.store import Store
from parser_app.workspace import DesktopApplication
from tests.support import FakeGateway, FakeRuntime

CONFIG = {"api_id": 12345, "api_hash": "a" * 32}

class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.vault=Vault(self.root/"app");self.key=os.urandom(256)
        self.source=self.root/"synthetic.session";write_session(self.source,2,self.key)
    def tearDown(self):self.temp.cleanup()
    def test_read_and_copy_without_source_mutation(self):
        before=self.source.read_bytes();items=self.vault.import_sessions([self.source],CONFIG)["items"]
        self.assertTrue(items[0]["ok"]);dest=self.vault.account_dir(items[0]["id"])/"telegram.session"
        self.assertEqual(read_session(dest),(2,self.key));self.assertEqual(self.source.read_bytes(),before)
        self.assertNotIn(str(self.source),self.vault.path.read_text())
        if os.name!="nt":self.assertEqual(dest.stat().st_mode&0o777,0o600)
    def test_duplicates_and_mixed_import(self):
        bad=self.root/"bad.session";bad.write_bytes(b"not SQLite"*100)
        result=self.vault.import_sessions([self.source,bad,self.source],CONFIG)
        self.assertEqual([r["ok"] for r in result["items"]],[True,False,False]);self.assertEqual(len(self.vault.entries()),1)
    def test_import_does_not_copy_contact_cache_or_injected_endpoint(self):
        with sqlite3.connect(self.source) as db:
            db.execute("INSERT INTO entities VALUES(1,2,'private_username',42,'private_name',1)")
            db.execute("UPDATE sessions SET server_address='attacker.invalid',port=1234")
        item=self.vault.import_sessions([self.source],CONFIG)["items"][0]
        with sqlite3.connect(self.vault.account_dir(item["id"])/"telegram.session") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entities").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT server_address,port FROM sessions").fetchone(),("149.154.167.51",443))
    def test_reject_pyrogram_and_empty_auth(self):
        bad=self.root/"pyrogram.session"
        with sqlite3.connect(bad) as db:db.execute("CREATE TABLE sessions(dc_id INTEGER,auth_key BLOB,api_id INTEGER)")
        with self.assertRaisesRegex(AppError,"не формат Telethon"):read_session(bad)
        with sqlite3.connect(self.source) as db:db.execute("UPDATE sessions SET auth_key=?",(b"",))
        with self.assertRaises(AppError):read_session(self.source)
    def test_active_session_and_bad_paths(self):
        side=Path(str(self.source)+"-wal");side.write_bytes(b"synthetic")
        with self.assertRaisesRegex(AppError,"Закройте"):read_session(self.source)
        for ident in ("../secrets",None,"",123):
            with self.assertRaises(AppError):self.vault.entry(ident)
    def test_tdata_validation_and_import_adapter(self):
        folder=self.root/"tdata";folder.mkdir()
        with self.assertRaisesRegex(AppError,"key_data"):validate_tdata_folder(folder)
        (folder/"key_datas").write_bytes(b"synthetic marker")
        self.assertEqual(validate_tdata_folder(folder),folder.resolve())
        with patch("parser_app.accounts.read_tdata",return_value=[((2,self.key),CONFIG)]):
            result=self.vault.import_tdata(folder,"local passcode")
        self.assertTrue(result["items"][0]["ok"]);self.assertEqual(self.vault.entries()[0]["kind"],"tdata")
        self.assertNotIn("local passcode",self.vault.path.read_text())
    def test_import_persist_failure_is_rolled_back(self):
        with patch.object(self.vault,"save",side_effect=OSError("disk full")):
            with self.assertRaises(OSError):self.vault.add("Test","session",CONFIG,(2,self.key))
        self.assertEqual(self.vault.entries(),[])
        self.assertEqual(list((self.vault.directory/"accounts").iterdir()),[])
    def test_local_removal_preserves_original(self):
        account=self.vault.import_sessions([self.source],CONFIG)["items"][0]["id"]
        self.vault.remove(account);self.assertTrue(self.source.exists());self.assertEqual(self.vault.entries(),[])
    def test_legacy_migration_preserves_files_and_survives_restart(self):
        root=self.root/"legacy-app";root.mkdir();write_session(root/"telegram.session",2,self.key)
        before=(root/"telegram.session").read_bytes();v=Vault(root)
        self.assertEqual(v.entries()[0]["id"],"legacy");self.assertEqual((root/"telegram.session").read_bytes(),before)
        self.assertEqual(len(Vault(root).entries()),1)
        v.remove("legacy");self.assertEqual(Vault(root).entries(),[])
    def test_public_account_snapshot_has_no_keys(self):
        self.vault.add("Test","session",CONFIG,(2,self.key));manager=AccountGateway(self.vault)
        public=json.dumps(manager.accounts());self.assertNotIn(CONFIG["api_hash"],public)
        for field in ("fingerprint","api_hash","auth_key"):self.assertNotIn(field,public)
    def test_proxy_validation_and_redaction(self):
        p=add_proxy(self.vault,{"host":"127.0.0.1","port":1080,"username":"private-user","password":"private-pass"})
        public=json.dumps(list_proxies(self.vault));self.assertNotIn("private-user",public);self.assertNotIn("private-pass",public)
        account=self.vault.add("Test");self.vault.update(account,proxy_id=p["id"])
        self.assertEqual(account_proxy(self.vault,p["id"])["password"],"private-pass")
        with self.assertRaises(AppError):delete_proxy(self.vault,p["id"])
        self.vault.update(account,proxy_id="");delete_proxy(self.vault,p["id"])
        with self.assertRaises(AppError):account_proxy(self.vault,p["id"])
    def test_invalid_proxies(self):
        for data in ({"host":"http://host","port":1},{"host":"x","port":70000},{"host":"x","port":1,"type":"mtproto"},{"host":"x","port":1,"username":"user"},{"host":"x","port":1,"username":"u","password":"\r\n"}):
            with self.subTest(data=data),self.assertRaises(AppError):validate_proxy(data)
        self.assertEqual(validate_proxy({"host":"[::1]","port":1080})["host"],"::1")
    def test_clean_session_is_accepted_by_real_telethon_if_installed(self):
        try:from telethon.sessions import SQLiteSession
        except ImportError:self.skipTest("Real Telethon is exercised in dependency-enabled CI")
        session=SQLiteSession(str(self.source))
        try:self.assertEqual(session.auth_key.key,self.key);self.assertEqual(session.dc_id,2)
        finally:session.close()

class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.vault=Vault(self.root)
        self.a=self.vault.add("Account A",config=CONFIG);self.b=self.vault.add("Account B",config=CONFIG)
        self.manager=AccountGateway(self.vault);self.ga=FakeGateway(5);self.gb=FakeGateway(9)
        self.manager.clients={self.a:self.ga,self.b:self.gb};self.store=Store(self.root/"parser.sqlite3")
    async def asyncTearDown(self):await self.manager.close();self.temp.cleanup()
    async def test_job_account_does_not_follow_ui_selection(self):
        self.manager.selected=self.b
        options=job_spec({"sources":"@example_channel","incremental":False})[1]|{"account_id":self.a}
        job_id=self.store.enqueue(["@example_channel"],options)[0]
        await Engine(self.store,self.manager).run_job(self.store.claim())
        self.assertEqual(self.store.job(job_id)["scanned"],5);self.assertIs(self.manager.worker,self.ga)
    async def test_missing_account_never_uses_selected_account(self):
        await self.manager.reset(self.a);self.vault.remove(self.a);self.manager.selected=self.b
        options=job_spec({"sources":"@example_channel"})[1]|{"account_id":self.a}
        job_id=self.store.enqueue(["@example_channel"],options)[0]
        await Engine(self.store,self.manager).run_job(self.store.claim())
        self.assertEqual(self.store.job(job_id)["state"],"failed");self.assertEqual(self.store.stats()["messages"],0)
    async def test_imported_logout_is_blocked(self):
        self.vault.update(self.a,kind="tdata")
        with self.assertRaisesRegex(AppError,"исходную сессию"):await self.manager.auth("logout",{"account_id":self.a})
    async def test_incremental_cursor_is_per_account(self):
        base=job_spec({"sources":"@example_channel"})[1];source=await self.ga.resolve("")
        first=self.store.enqueue(["@example_channel"],base|{"account_id":self.a})[0];self.store.claim();self.store.prepare(first,source,100)
        self.store.checkpoint(first,source["id"],30,30,[]);self.store.state(first,"completed")
        second=self.store.enqueue(["@example_channel"],base|{"account_id":self.b})[0];self.store.claim()
        self.assertEqual(self.store.prepare(second,source,100)["cursor"],0)
        self.store.state(second,"completed");third=self.store.enqueue(["@example_channel"],base|{"account_id":self.a})[0];self.store.claim()
        self.assertEqual(self.store.prepare(third,source,100)["cursor"],30)

class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.vault=Vault(self.root/"app");self.manager=AccountGateway(self.vault);self.store=Store(self.root/"app"/"parser.sqlite3")
        self.app=DesktopApplication(self.store,FakeRuntime(self.manager),"test-capability",self.root/"app")
        self.bridge=NativeBridge(self.app,"http://127.0.0.1:1234");self.window=Mock()
        self.window.get_current_url.return_value="http://127.0.0.1:1234/";self.bridge._window=self.window
        self.webview=types.SimpleNamespace(FileDialog=types.SimpleNamespace(OPEN=1,FOLDER=2,SAVE=3))
    def tearDown(self):self.temp.cleanup()
    def test_guard_denies_wrong_key_and_external_navigation(self):
        self.assertIn("error",self.bridge.import_accounts("wrong","session",**CONFIG));self.window.create_file_dialog.assert_not_called()
        self.window.get_current_url.return_value="https://untrusted.invalid/"
        self.assertIn("error",self.bridge.import_accounts("test-capability","session",**CONFIG));self.window.create_file_dialog.assert_not_called()
    def test_cancelled_file_dialog_has_no_mutation(self):
        self.window.create_file_dialog.return_value=None
        with patch.dict("sys.modules",{"webview":self.webview}):result=self.bridge.import_accounts("test-capability","session",**CONFIG)
        self.assertTrue(result["cancelled"]);self.assertEqual(self.vault.entries(),[])
    def test_session_picker_drives_real_local_import(self):
        source=self.root/"test.session";write_session(source,2,os.urandom(256));self.window.create_file_dialog.return_value=(str(source),)
        with patch.dict("sys.modules",{"webview":self.webview}):result=self.bridge.import_accounts("test-capability","session",**CONFIG)
        self.assertTrue(result["items"][0]["ok"]);self.assertEqual(len(self.vault.entries()),1)
    def test_tdata_picker_drives_converter_without_exposing_path(self):
        self.window.create_file_dialog.return_value=(str(self.root/"tdata"),)
        with patch.dict("sys.modules",{"webview":self.webview}),patch.object(self.vault,"import_tdata",return_value={"items":[]}) as converter:
            result=self.bridge.import_accounts("test-capability","tdata",passcode="synthetic")
        converter.assert_called_once_with(str(self.root/"tdata"),"synthetic");self.assertEqual(result,{"items":[]})
    def test_import_while_queue_busy_is_rejected(self):
        options=job_spec({"sources":"@example_channel"})[1];self.store.enqueue(["@example_channel"],options)
        self.window.create_file_dialog.return_value=("test.session",)
        with patch.dict("sys.modules",{"webview":self.webview}):result=self.bridge.import_accounts("test-capability","session",**CONFIG)
        self.assertIn("паузу",result["error"]);self.assertEqual(self.vault.entries(),[])
    def test_native_export_uses_save_dialog_and_retains_existing_temporary_file(self):
        destination=self.root/"export.json";self.window.create_file_dialog.return_value=(str(destination),)
        with patch.dict("sys.modules",{"webview":self.webview}):result=self.bridge.export_messages("test-capability","json")
        self.assertTrue(result["ok"]);self.assertEqual(json.loads(destination.read_text()),[])
        temp=destination.with_name(destination.name+".parser-tmp");temp.write_text("belongs to another export")
        with patch.dict("sys.modules",{"webview":self.webview}):result=self.bridge.export_messages("test-capability","json")
        self.assertIn("error",result);self.assertEqual(temp.read_text(),"belongs to another export")
    def test_export_cannot_overwrite_vault(self):
        self.window.create_file_dialog.return_value=(str(self.vault.path),)
        with patch.dict("sys.modules",{"webview":self.webview}):result=self.bridge.export_messages("test-capability","json")
        self.assertIn("error",result);self.assertEqual(json.loads(self.vault.path.read_text())["version"],1)

if __name__=="__main__":unittest.main()
