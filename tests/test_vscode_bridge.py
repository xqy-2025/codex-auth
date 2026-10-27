import contextlib
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import codex_auth as app


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
if not NODE:
    candidates = list(Path.home().glob(".vscode-server/cli/servers/Stable-*/server/node"))
    NODE = str(candidates[0]) if candidates else None


@unittest.skipUnless(NODE and hasattr(os, "getuid"), "Node and Unix sockets required")
class VSCodeBridgeTests(unittest.TestCase):
    def test_bridge_accepts_restart_without_reading_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codex"
            home.mkdir()
            marker = root / "restarted"
            script = root / "stub.js"
            script.write_text('''
const Module = require("module");
const original = Module._load;
Module._load = function(name, ...args) {
  if (name === "vscode") return {commands: {executeCommand: command => {
    if (command !== "workbench.action.restartExtensionHost") throw Error("wrong command");
    require("fs").writeFileSync(process.argv[3], "called");
    return Promise.resolve();
  }}};
  return original.call(this, name, ...args);
};
require(process.argv[2]).activate({subscriptions: []});
''')
            env = {**os.environ, "CODEX_HOME": str(home)}
            proc = subprocess.Popen([NODE, str(script), str(ROOT / "vscode-bridge" / "extension.js"),
                                     str(marker)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not list((home / "codex-auth-vscode").glob("*.sock")):
                    if proc.poll() is not None:
                        self.fail("bridge failed to start: " + proc.stderr.read().decode(errors="replace"))
                    if time.monotonic() >= deadline:
                        self.fail("bridge failed to start: timed out")
                    time.sleep(0.02)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertTrue(app.restart_vscode(app.Store(home)))
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(marker.read_text(), "called")
                self.assertFalse((home / "auth.json").exists())
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()

    def test_bridge_disabled_by_default_and_switch_sends_when_enabled(self):
        from test_codex_auth import cockpit
        with tempfile.TemporaryDirectory() as tmp:
            store = app.Store(Path(tmp) / "codex")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                store.save("one", app.normalize(cockpit()))
                self.assertFalse((store.profiles / ".restart-vscode").exists())
                app.configure_vscode(store, "enable")
                self.assertTrue((store.profiles / ".restart-vscode").exists())
                # Absence of the bridge never prevents a successful account switch.
                store.switch("one")
                self.assertEqual(store.active(), "one")
                app.configure_vscode(store, "disable")
                self.assertFalse((store.profiles / ".restart-vscode").exists())

    def test_enabled_switch_requests_one_restart(self):
        from test_codex_auth import cockpit
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            store = app.Store(Path(tmp) / "codex")
            store.save("one", app.normalize(cockpit()))
            app.configure_vscode(store, "enable")
            with mock.patch.object(app, "restart_vscode", return_value=True) as restart:
                store.switch("one")
                restart.assert_called_once_with(store)

    def test_multiple_live_windows_are_not_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = app.Store(Path(tmp) / "codex")
            directory = store.home / "codex-auth-vscode"
            directory.mkdir(mode=0o700, parents=True)
            sockets = []
            workers = []
            messages = []

            def serve(server):
                try:
                    conn, _ = server.accept()
                    with conn:
                        messages.append(conn.recv(100))
                        conn.sendall(b"pong\n")
                except OSError:
                    pass

            try:
                for i in range(2):
                    server = socket.socket(socket.AF_UNIX)
                    server.bind(str(directory / f"{i}.sock"))
                    server.listen()
                    sockets.append(server)
                    worker = threading.Thread(target=serve, args=(server,), daemon=True)
                    worker.start()
                    workers.append(worker)
                with contextlib.redirect_stderr(io.StringIO()) as warning:
                    self.assertFalse(app.restart_vscode(store))
                self.assertIn("多个 VS Code 窗口", warning.getvalue())
                self.assertEqual(messages, [b"ping\n", b"ping\n"])
            finally:
                for server in sockets:
                    server.close()
                for worker in workers:
                    worker.join(timeout=1)

    def test_installer_packages_only_bridge_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-code"
            script.write_text('''#!/usr/bin/env python3
import sys, zipfile
with zipfile.ZipFile(sys.argv[2]) as z:
    assert set(z.namelist()) == {"[Content_Types].xml", "extension.vsixmanifest", "extension/package.json", "extension/extension.js"}
''')
            script.chmod(0o700)
            proc = subprocess.run([os.sys.executable, str(ROOT / "vscode-bridge" / "install.py"),
                                   "--code", str(script)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
