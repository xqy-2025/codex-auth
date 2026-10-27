"use strict";

// This bridge receives only ping/restart commands; it never reads auth.json.
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const vscode = require("vscode");

let server;
let socketPath;

function activate(context) {
  if (process.platform === "win32") return;
  const codexHome = process.env.CODEX_HOME || path.join(os.homedir(), ".codex");
  const dir = path.join(codexHome, "codex-auth-vscode");
  try {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const stat = fs.lstatSync(dir);
    if (stat.isSymbolicLink() || !stat.isDirectory() || stat.uid !== process.getuid()) {
      throw new Error("unsafe bridge directory");
    }
    fs.chmodSync(dir, 0o700);
    socketPath = path.join(dir, `${process.pid}.sock`);
    if (fs.existsSync(socketPath)) {
      const old = fs.lstatSync(socketPath);
      if (!old.isSocket() || old.uid !== process.getuid()) throw new Error("unsafe socket path");
      fs.unlinkSync(socketPath);
    }
    server = net.createServer(client => {
      client.setTimeout(2000, () => client.destroy());
      let incoming = "";
      client.on("error", () => {});
      client.on("data", chunk => {
        incoming += chunk.toString("utf8");
        if (incoming.length > 100) return client.destroy();
        if (!incoming.includes("\n")) return;
        if (incoming === "ping\n") return client.end("pong\n");
        if (incoming !== "restart\n") return client.end("unsupported\n");
        client.end("accepted\n", () => {
          setTimeout(() => {
            Promise.resolve(vscode.commands.executeCommand("workbench.action.restartExtensionHost"))
              .catch(error => console.error("codex-auth: extension host restart failed", error));
          }, 200);
        });
      });
    });
    server.on("error", error => console.error("codex-auth: bridge failed", error));
    server.listen(socketPath, () => {
      try { fs.chmodSync(socketPath, 0o600); }
      catch (error) { console.error("codex-auth: cannot protect bridge socket", error); server.close(); }
    });
    context.subscriptions.push({ dispose: deactivate });
  } catch (error) {
    console.error("codex-auth: bridge unavailable", error);
  }
}

function deactivate() {
  if (server) server.close();
  if (socketPath) {
    try { fs.unlinkSync(socketPath); } catch (_) { /* already removed */ }
  }
  server = undefined;
  socketPath = undefined;
}

module.exports = { activate, deactivate };
