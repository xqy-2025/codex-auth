import base64
import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import codex_auth as app


ROOT = Path(__file__).resolve().parents[1]


def jwt(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"fake.{body}.test-signature"


def cockpit(account="test-account", generation="old", subject="test-user"):
    return {"type": "codex", "id_token": jwt({"sub": subject, "email": "demo@example.invalid",
            app.AUTH_CLAIM: {"chatgpt_account_id": account}}),
            "access_token": "fake-access-" + generation, "refresh_token": "fake-refresh-" + generation,
            "account_id": account, "last_refresh": "2026-10-21T01:18:13.000Z",
            "email": "demo@example.invalid", "expired": "2099-01-01T00:00:00Z"}


class TemporaryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "codex"
        self.store = app.Store(self.home)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.tmp.cleanup()

    def save(self, name="甲", account="one", generation="old"):
        self.store.save(name, app.normalize(cockpit(account, generation)))

    def run_cli(self, *args, data=None):
        return subprocess.run([sys.executable, str(ROOT / "codex_auth.py"), "--home", str(self.home),
                               *args], input=data, capture_output=True, text=True)

    def test_exact_conversion_and_no_extra_metadata(self):
        source = cockpit()
        converted = app.normalize(source)
        self.assertEqual(set(converted), {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"})
        self.assertEqual(converted["tokens"], {key: source[key] for key in
                         ("id_token", "access_token", "refresh_token", "account_id")})
        self.assertEqual(converted["last_refresh"], source["last_refresh"])
        self.assertIsNone(converted["OPENAI_API_KEY"])

    def test_cli_array_import_and_switch(self):
        result = self.run_cli("add", "test", "--stdin", data=json.dumps([cockpit()]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.store.auth.exists())
        result = self.run_cli("test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(app.read_json(self.store.auth), app.normalize(cockpit()))
        self.assertEqual(self.store.active(), "test")
        listed = self.run_cli("list")
        self.assertIn("* test", listed.stdout)
        self.assertNotIn("fake-access", listed.stdout)

    def test_multi_account_requires_index_without_tty(self):
        raw = json.dumps([cockpit("one"), cockpit("two")])
        result = self.run_cli("add", "two", "--stdin", data=raw)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.store.names(), [])
        result = self.run_cli("add", "two", "--stdin", "--index", "2", data=raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.get("two")["tokens"]["account_id"], "two")

    def test_switch_preserves_rotated_tokens_and_backup(self):
        self.save("one", "one")
        self.save("two", "two")
        self.store.switch("one")
        rotated = app.normalize(cockpit("one", "rotated"))
        app.write_json(self.store.auth, rotated)
        self.store.switch("two")
        self.assertEqual(self.store.get("one"), rotated)
        backups = list((self.home / "auth-backups").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(app.read_json(backups[0]), rotated)
        self.store.switch("one")
        self.assertEqual(app.read_json(self.store.auth), rotated)

    def test_switch_to_same_account_does_not_restore_stale_token(self):
        self.save("one", "one")
        self.store.switch("one")
        rotated = app.normalize(cockpit("one", "rotated"))
        app.write_json(self.store.auth, rotated)
        self.store.switch("one")
        self.assertEqual(app.read_json(self.store.auth), rotated)

    def test_update_active_also_updates_auth(self):
        self.save("one", "one")
        self.store.switch("one")
        new = app.normalize(cockpit("one", "updated"))
        self.store.save("one", new, update=True)
        self.assertEqual(app.read_json(self.store.auth), new)
        self.assertEqual(self.store.get("one"), new)

    def test_update_inactive_leaves_auth_unchanged(self):
        self.save("one", "one")
        self.save("two", "two")
        self.store.switch("one")
        before = self.store.auth.read_bytes()
        self.store.save("two", app.normalize(cockpit("two", "updated")), update=True)
        self.assertEqual(self.store.auth.read_bytes(), before)

    def test_external_login_cannot_overwrite_previous_profile(self):
        self.save("one", "one")
        self.save("two", "two")
        self.store.switch("one")
        before = self.store.get("one")
        app.write_json(self.store.auth, app.normalize(cockpit("outside")))
        self.store.switch("two")
        self.assertEqual(self.store.get("one"), before)

    def test_same_workspace_different_users_not_confused(self):
        self.save("one", "workspace")
        self.store.switch("one")
        app.write_json(self.store.auth, app.normalize(cockpit("workspace", subject="another-user")))
        self.assertIsNone(self.store.active())

    def test_file_permissions(self):
        self.save()
        self.store.switch("甲")
        for path in (self.home, self.store.profiles):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.store.auth, self.store.path("甲"), self.store.marker):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_invalid_json_and_missing_fields_leave_auth_untouched(self):
        self.save("one", "one")
        self.store.switch("one")
        before = self.store.auth.read_bytes()
        for raw in ('{"secret":"fake-sensitive"', '{"access_token":"fake-sensitive"}', "[]"):
            result = self.run_cli("update", "one", "--stdin", data=raw)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("fake-sensitive", result.stdout + result.stderr)
            self.assertEqual(self.store.auth.read_bytes(), before)

    def test_names_cannot_escape_profile_directory(self):
        for name in ("", ".", "..", "../auth", "a/b", "x\nname", "x\x1b[2J", "a" * 65):
            with self.assertRaises(app.AuthError):
                self.store.path(name)

    def test_duplicate_add_and_missing_update(self):
        self.save()
        with self.assertRaises(app.AuthError):
            self.save()
        with self.assertRaises(app.AuthError):
            self.store.save("missing", app.normalize(cockpit()), update=True)

    def test_symlink_auth_not_overwritten(self):
        self.save()
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text(json.dumps(app.normalize(cockpit())))
        self.store.auth.symlink_to(outside)
        before = outside.read_bytes()
        with self.assertRaises(app.AuthError):
            self.store.switch("甲")
        self.assertEqual(outside.read_bytes(), before)

    def test_keyring_switch_fails_without_changes(self):
        self.save()
        (self.home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        with self.assertRaises(app.AuthError):
            self.store.switch("甲")
        self.assertFalse(self.store.auth.exists())

    def test_provider_nested_setting_is_not_root_setting(self):
        self.save()
        (self.home / "config.toml").write_text('[model_providers.example]\ncli_auth_credentials_store="keyring"\n')
        self.store.switch("甲")
        self.assertTrue(self.store.auth.exists())

    def test_api_key_auth_backed_up(self):
        self.save()
        original = {"auth_mode": "apikey", "OPENAI_API_KEY": "fake-api-key"}
        app.write_json(self.store.auth, original)
        self.store.switch("甲")
        backup = next((self.home / "auth-backups").glob("*.json"))
        self.assertEqual(app.read_json(backup), original)

    def test_remove_does_not_logout(self):
        self.save()
        self.store.switch("甲")
        before = self.store.auth.read_bytes()
        self.store.remove("甲")
        self.assertEqual(self.store.names(), [])
        self.assertEqual(self.store.auth.read_bytes(), before)

    def test_migrate_is_idempotent_and_keeps_original(self):
        self.home.mkdir()
        old = self.home / "old.json"
        old.write_text(json.dumps(app.normalize(cockpit("one"))))
        registry = self.home / "auth-switch.registry"
        registry.write_text(f"one\t{old}\n")
        app.write_json(self.store.auth, app.normalize(cockpit("one", "rotated")))
        self.store.migrate()
        self.assertEqual(self.store.get("one")["tokens"]["refresh_token"], "fake-refresh-rotated")
        before = self.store.get("one")
        self.store.migrate()
        self.assertEqual(self.store.get("one"), before)
        self.assertTrue(old.exists())
        self.assertTrue(registry.exists())

    def test_incomplete_migration_does_not_write_partial_accounts(self):
        self.home.mkdir()
        old = self.home / "old.json"
        old.write_text(json.dumps(cockpit()))
        (self.home / "auth-switch.registry").write_text(f"one\t{old}\ntwo\t/nonexistent.json\n")
        with self.assertRaises(app.AuthError):
            self.store.migrate()
        self.assertEqual(self.store.names(), [])

    def test_interactive_paste_hides_tokens_and_selects_from_menu(self):
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, str(ROOT / "codex_auth.py"), "--home", str(self.home)],
                                stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        received = bytearray()

        def expect(text):
            deadline = time.monotonic() + 5
            while text.encode() not in received:
                if time.monotonic() > deadline:
                    self.fail(f"TTY timed out waiting for {text!r}")
                if select.select([master], [], [], 0.1)[0]:
                    received.extend(os.read(master, 65536))

        try:
            expect("输入序号")
            os.write(master, b"2\n")
            expect("账号名称")
            os.write(master, "测试\n".encode())
            expect("输入隐藏")
            # Wait for termios ECHO to be disabled before emulating a paste.
            import termios
            deadline = time.monotonic() + 5
            while termios.tcgetattr(master)[3] & termios.ECHO:
                if time.monotonic() > deadline:
                    self.fail("TTY input remained visible")
                time.sleep(0.01)
            source = cockpit()
            source["access_token"] = "fake-access-" + "x" * 12000
            raw = (json.dumps([source]) + "\n").encode()
            for i in range(0, len(raw), 2048):
                os.write(master, raw[i:i + 2048])
            expect("已保存账号")
            proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 0)
            self.assertNotIn(b"fake-refresh", received)
            self.assertTrue(self.store.path("测试").exists())
            self.assertEqual(self.store.get("测试")["tokens"]["access_token"], source["access_token"])
        finally:
            proc.kill() if proc.poll() is None else None
            proc.wait()
            os.close(master)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process locks only")
class LockTests(unittest.TestCase):
    def test_real_flock_detected_and_never_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / "thread-writer-locks"
            folder.mkdir()
            lock = folder / "00000000-0000-0000-0000-000000000001.lock"
            worker = subprocess.Popen([sys.executable, "-c",
                "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); "
                "print('ready',flush=True); time.sleep(30)", str(lock)], stdout=subprocess.PIPE)
            try:
                self.assertEqual(worker.stdout.readline(), b"ready\n")
                holders, unknown = app.lock_inventory(home)
                holder = next(h for h in holders if h["pid"] == worker.pid)
                self.assertTrue(holder["protected"])  # Python is never killed as Codex.
                self.assertEqual(unknown, [])
                with self.assertRaises(app.AuthError):
                    app.terminate_holder(holder)
                self.assertIsNone(worker.poll())
                self.assertTrue(lock.exists())
            finally:
                worker.terminate()
                worker.wait(timeout=5)
                worker.stdout.close()
            holders, unknown = app.lock_inventory(home)
            self.assertEqual((holders, unknown), ([], []))
            self.assertTrue(lock.exists())

    def test_protected_pid_cannot_be_terminated(self):
        with self.assertRaises(app.AuthError):
            app.terminate_holder({"pid": os.getpid(), "protected": False})

    def test_pid_reuse_is_rejected(self):
        holder = {"pid": 9999999, "start": "old", "exe": "/fake/codex", "uid": os.geteuid(),
                  "protected": False}
        with mock.patch.object(app, "open_pidfd", return_value=123), \
             mock.patch.object(app.os, "close"), \
             mock.patch.object(app, "ancestors", return_value={1, os.getpid()}), \
             mock.patch.object(app, "process_info", return_value={**holder, "start": "new"}), \
             mock.patch.object(app.signal, "pidfd_send_signal") as send:
            with self.assertRaises(app.AuthError):
                app.terminate_holder(holder)
            send.assert_not_called()

    def test_unlock_terminates_only_simulated_codex_lock_holder(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / "thread-writer-locks"
            folder.mkdir()
            lock = folder / "00000000-0000-0000-0000-000000000002.lock"
            binary = home / "codex"
            # A harmless sleep binary with the expected executable name; never real Codex.
            shutil.copyfile(shutil.which("sleep"), binary)
            binary.chmod(0o700)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            worker = subprocess.Popen([str(binary), "30"], pass_fds=(fd,))
            os.close(fd)
            try:
                holders, unknown = app.lock_inventory(home)
                self.assertEqual(unknown, [])
                self.assertEqual([h["pid"] for h in holders], [worker.pid])
                self.assertFalse(holders[0]["protected"])
                args = type("Args", (), {"all": True, "yes": True, "dry_run": False})()
                with contextlib.redirect_stdout(io.StringIO()):
                    app.unlock(app.Store(home), args)
                worker.wait(timeout=5)
                self.assertEqual(worker.returncode, -15)
                self.assertTrue(lock.exists())
                self.assertEqual(app.lock_inventory(home), ([], []))
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=5)


class InstallerTests(unittest.TestCase):
    def test_install_preserves_legacy_command_and_launches_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "private home"
            bin_dir = Path(tmp) / "bin"
            home.mkdir()
            legacy = home / "codex-auth"
            legacy.write_text("#!/bin/sh\necho legacy\n")
            old = home / "old.json"
            old.write_text(json.dumps(cockpit()))
            (home / "auth-switch.registry").write_text(f"demo\t{old}\n")
            cmd = [sys.executable, str(ROOT / "install.py"), "--home", str(home), "--bin-dir", str(bin_dir)]
            for _ in range(2):
                result = subprocess.run(cmd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            backups = list(home.glob("codex-auth.legacy-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("echo legacy", backups[0].read_text())
            self.assertTrue((bin_dir / "codex-auth").is_symlink())
            result = subprocess.run([str(bin_dir / "codex-auth"), "list"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("demo", result.stdout)
            self.assertNotIn("fake-refresh", result.stdout)
            self.assertTrue((home / "auth-profiles" / "demo.json").exists())
            self.assertFalse((home / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
