#!/usr/bin/env python3
"""Local, dependency-free Codex account manager. Never sends credentials anywhere."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import termios
import time

VERSION = "1.0.0"
AUTH_CLAIM = "https://api.openai.com/auth"


class AuthError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def valid_name(name):
    if (not name or len(name) > 64 or name in (".", "..")
            or not all(c.isalnum() or c in "._-" for c in name)):
        raise AuthError("名称限 1–64 个中文、字母、数字、点、横线或下划线，不能是 . 或 ..。")
    return name


def claims(token):
    """Read metadata only; this does not verify a JWT or authenticate a user."""
    try:
        payload = token.split(".")[1]
        obj = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return obj if isinstance(obj, dict) else {}
    except (ValueError, IndexError, TypeError, UnicodeError):
        return {}


def parse_json(raw):
    try:
        return json.loads(raw.lstrip("\ufeff"))
    except (ValueError, UnicodeError):
        # JSONDecodeError includes the source document. Never echo that document.
        raise AuthError("JSON 无效，请粘贴完整的 JSON 对象或数组。") from None


def normalize(data):
    if not isinstance(data, dict):
        raise AuthError("账号必须是 JSON 对象。")
    if data.get("type", "codex") != "codex":
        raise AuthError("只支持 type 为 codex 的账号。")
    if data.get("auth_mode", "chatgpt") != "chatgpt":
        raise AuthError("只支持 ChatGPT 登录凭据。")
    source = data.get("tokens", data)
    if not isinstance(source, dict):
        raise AuthError("tokens 必须是 JSON 对象。")
    tokens = {}
    for key in ("id_token", "access_token", "refresh_token"):
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AuthError(f"缺少或无效的字段：{key}")
        tokens[key] = value
    account_id = source.get("account_id")
    if not account_id:
        for token in (tokens["id_token"], tokens["access_token"]):
            auth = claims(token).get(AUTH_CLAIM, {})
            if isinstance(auth, dict):
                account_id = auth.get("chatgpt_account_id")
                if account_id:
                    break
    if not isinstance(account_id, str) or not account_id.strip():
        raise AuthError("缺少 account_id，且无法从令牌读取。")
    tokens["account_id"] = account_id
    refreshed = data.get("last_refresh")
    if refreshed is not None:
        if not isinstance(refreshed, str):
            raise AuthError("last_refresh 必须是 ISO 8601 时间字符串。")
        try:
            parsed = datetime.fromisoformat(refreshed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        except ValueError:
            raise AuthError("last_refresh 必须是带时区的 ISO 8601 时间。") from None
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": tokens, "last_refresh": refreshed or now()}


def identity(auth):
    tokens = auth["tokens"]
    subject = claims(tokens["id_token"]).get("sub", "")
    return tokens["account_id"], subject


def safe_label(value):
    # Imported metadata and legacy names must not inject terminal control codes.
    return "".join(c if c.isprintable() else "?" for c in str(value))[:100]


def describe(auth):
    payload = claims(auth["tokens"]["id_token"])
    email = payload.get("email", "")
    return safe_label(email or auth["tokens"]["account_id"])


def read_json(path):
    if path.is_symlink():
        raise AuthError(f"拒绝读取符号链接：{path.name}")
    try:
        return parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        raise AuthError(f"无法读取文件：{path.name}") from None


def private_dir(path):
    if path.is_symlink():
        raise AuthError(f"私有目录不能是符号链接：{path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_text(path, text):
    if path.is_symlink():
        raise AuthError(f"拒绝覆盖符号链接：{path.name}")
    fd, tmp = tempfile.mkstemp(prefix=".auth-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, obj):
    atomic_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def select(items, title, labels=None):
    if not items:
        raise AuthError("没有可选择的账号，请先运行 codex-auth add。")
    print(title, file=sys.stderr)
    for i, item in enumerate(items, 1):
        print(f"  {i}. {labels[i - 1] if labels else item}", file=sys.stderr)
    if not sys.stdin.isatty():
        raise AuthError("非交互模式请明确指定名称或选项。")
    answer = input("输入序号或名称（回车取消）：").strip()
    if not answer:
        raise AuthError("已取消。")
    # A numeric account name is accepted when it isn't an in-range menu number.
    if answer.isdecimal() and 1 <= int(answer) <= len(items):
        return items[int(answer) - 1]
    if answer in items:
        return answer
    raise AuthError("无效的选择。")


def credential_input(args):
    if getattr(args, "file", None):
        data = read_json(Path(args.file).expanduser())
    elif getattr(args, "stdin", False):
        data = parse_json(sys.stdin.read())
    else:
        if not sys.stdin.isatty():
            raise AuthError("非交互导入请使用 --stdin 或 --file PATH。")
        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        hidden = previous.copy()
        # Canonical TTY input truncates long pasted lines at ~4096 bytes.
        hidden[3] &= ~(termios.ECHO | termios.ICANON)
        hidden[6] = previous[6][:]
        hidden[6][termios.VMIN] = 1
        hidden[6][termios.VTIME] = 0
        buffer = bytearray()
        try:
            termios.tcsetattr(fd, termios.TCSANOW, hidden)
            print("粘贴 Cockpit Tools JSON 或 Codex auth.json（输入隐藏）。\n"
                  "完整 JSON 最后一行回车后自动结束；空行结束无效输入；Ctrl-C 取消。", file=sys.stderr)
            while True:
                chunk = os.read(fd, 65536)
                eof = not chunk or b"\x04" in chunk
                buffer.extend(chunk.replace(b"\x04", b""))
                if not eof and b"\n" not in chunk:
                    continue
                try:
                    data = json.loads(buffer.decode("utf-8-sig"))
                    break
                except (ValueError, UnicodeError):
                    if eof or bytes(buffer).endswith((b"\n\n", b"\r\n\r\n")) or not buffer.strip():
                        raise AuthError("JSON 无效，请粘贴完整的 JSON 对象或数组。") from None
                    continue
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, previous)
            print(file=sys.stderr)
    if isinstance(data, list):
        if not data:
            raise AuthError("JSON 数组为空。")
        # Validate every record before displaying metadata or saving anything.
        records = [normalize(item) for item in data]
        if len(records) == 1:
            return records[0]
        index = getattr(args, "index", None)
        if index is not None:
            if not 1 <= index <= len(records):
                raise AuthError("--index 超出 JSON 数组范围（序号从 1 开始）。")
            return records[index - 1]
        indexes = [str(i) for i in range(1, len(records) + 1)]
        selected = select(indexes, "JSON 中有多个账号，请选择：", [describe(a) for a in records])
        return records[int(selected) - 1]
    if getattr(args, "index", None) not in (None, 1):
        raise AuthError("JSON 对象仅支持 --index 1。")
    return normalize(data)


class Store:
    def __init__(self, home):
        self.home = Path(home).expanduser().absolute()
        self.profiles = self.home / "auth-profiles"
        self.auth = self.home / "auth.json"
        self.marker = self.profiles / ".active"

    @contextmanager
    def locked(self):
        private_dir(self.home)
        private_dir(self.profiles)
        lock_path = self.profiles / ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AuthError("另一个 codex-auth 正在修改账号，请稍后重试。") from None
            yield
        finally:
            os.close(fd)

    def path(self, name):
        return self.profiles / (valid_name(name) + ".json")

    def names(self):
        if not self.profiles.exists():
            return []
        return sorted(p.stem for p in self.profiles.glob("*.json") if not p.is_symlink())

    def get(self, name):
        return normalize(read_json(self.path(name)))

    def current_auth(self):
        if not self.auth.exists() and not self.auth.is_symlink():
            return None
        try:
            data = read_json(self.auth)
            if isinstance(data, dict) and (data.get("auth_mode") == "apikey" or
                                          (data.get("OPENAI_API_KEY") and not data.get("tokens"))):
                return None
            return normalize(data)
        except AuthError:
            raise AuthError("当前 auth.json 无效或不是支持的凭据，请先检查；尚未覆盖。") from None

    def active(self):
        current = self.current_auth()
        if current is None:
            return None
        try:
            if self.marker.is_symlink():
                raise AuthError("活动账号标记不能是符号链接。")
            marked = self.marker.read_text().strip() if self.marker.exists() else None
            if marked and identity(self.get(marked)) == identity(current):
                return marked
        except (AuthError, OSError):
            pass
        matches = []
        for name in self.names():
            try:
                if identity(self.get(name)) == identity(current):
                    matches.append(name)
            except AuthError:
                continue
        return matches[0] if len(matches) == 1 else None

    def backup(self):
        if self.auth.is_symlink():
            raise AuthError("auth.json 为符号链接，请先改用普通文件。")
        if self.auth.exists():
            folder = self.home / "auth-backups"
            private_dir(folder)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            # Preserve even API-key credentials and unknown fields for recovery.
            atomic_text(folder / f"auth-{stamp}.json", self.auth.read_text(encoding="utf-8"))

    def sync(self):
        name = self.active()
        if name:
            write_json(self.path(name), self.current_auth())

    def check_file_store(self):
        config = self.home / "config.toml"
        if not config.exists():
            return
        # Parse TOML so an unrelated provider setting cannot be mistaken for the root setting.
        try:
            import tomllib
            settings = tomllib.loads(config.read_text(encoding="utf-8"))
        except ImportError:
            raise AuthError("检查 config.toml 需要 Python 3.11 或更高版本。") from None
        except (ValueError, OSError):
            raise AuthError("无法解析 config.toml，请先检查配置。") from None
        mode = settings.get("cli_auth_credentials_store", "file")
        if mode != "file":
            raise AuthError(f"当前凭据存储方式为 {safe_label(mode)}。请将 config.toml 的 "
                            'cli_auth_credentials_store 设为 "file" 后切换。')

    def choose(self, name=None):
        if name is not None:
            self.get(name)
            return name
        active = self.active()
        names = self.names()
        labels = [f"{'* ' if n == active else ''}{n}  {describe(self.get(n))}" for n in names]
        return select(names, "选择账号（* 当前账号）：", labels)

    def switch(self, name):
        with self.locked():
            self.check_file_store()
            self.get(name)  # Validate the target before changing the previous profile.
            self.sync()
            target = self.get(name)  # It may have just been refreshed by sync().
            self.backup()
            write_json(self.auth, target)
            atomic_text(self.marker, name + "\n")
        print(f"已切换至 {name}。已打开的 Codex 客户端可能需要重新加载才能使用新账号。")

    def save(self, name, auth, update=False):
        with self.locked():
            path = self.path(name)
            exists = path.exists() or path.is_symlink()
            if update and not exists:
                raise AuthError("账号不存在，请先 add。")
            if exists and not update:
                raise AuthError("同名账号已存在，请使用 update。")
            active = self.active()
            if update and active == name:
                self.check_file_store()
                self.backup()
            write_json(path, auth)
            if update and active == name:
                write_json(self.auth, auth)
                atomic_text(self.marker, name + "\n")
        print(f"已{'更新' if update else '保存'}账号：{name}" +
              ("（当前 auth.json 已同步）" if update and active == name else ""))

    def remove(self, name):
        with self.locked():
            self.get(name)
            active = self.active()
            self.path(name).unlink()
            if active == name and self.marker.exists():
                self.marker.unlink()
        print(f"已移除保存的账号：{name}。当前 auth.json 仍可使用。")

    def migrate(self):
        registry = self.home / "auth-switch.registry"
        if not registry.exists():
            print("没有旧版登记表，无需迁移。")
            return
        pending = []
        try:
            lines = registry.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise AuthError("无法读取旧版登记表。") from None
        seen = set()
        for line in lines:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                raise AuthError("旧版登记表格式无效，应为名称、制表符、文件路径。")
            name, source = parts
            valid_name(name)
            if name in seen:
                raise AuthError("旧版登记表存在重复名称，请先修正。")
            seen.add(name)
            if self.path(name).exists():
                continue
            pending.append((name, normalize(read_json(Path(source).expanduser()))))
        with self.locked():
            for name, auth in pending:
                if not self.path(name).exists():
                    write_json(self.path(name), auth)
            self.sync()
        print(f"已迁移 {len(pending)} 个账号，旧登记表和旧文件已保留。")


def process_info(pid, proc=Path("/proc")):
    root = proc / str(pid)
    raw = (root / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    return {"pid": pid, "ppid": int(fields[1]), "start": fields[19],
            "exe": os.readlink(root / "exe"), "uid": root.stat().st_uid}


def ancestors():
    result = {1, os.getpid()}
    pid = os.getpid()
    while pid > 1:
        try:
            pid = process_info(pid)["ppid"]
        except (OSError, ValueError, IndexError):
            break
        if pid in result:
            break
        result.add(pid)
    return result


def lock_inventory(home):
    """Probe flock state and find actual lock-holding FDs, including OFD locks."""
    if not sys.platform.startswith("linux"):
        raise AuthError("会话解锁目前支持 Linux；其他系统请退出占用会话的 Codex 客户端。")
    folder = Path(home) / "thread-writer-locks"
    if folder.is_symlink():
        raise AuthError("会话锁目录不能是符号链接。")
    busy = {}
    if not folder.exists():
        return [], []
    for path in sorted(folder.glob("*.lock")):
        if not re.fullmatch(r"[0-9a-fA-F-]{36}\.lock", path.name):
            continue
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                busy[(st.st_dev, st.st_ino)] = path.stem
        finally:
            os.close(fd)
    if not busy:
        return [], []
    holders = []
    found = set()
    protected = ancestors()
    for root in Path("/proc").iterdir():
        if not root.name.isdecimal():
            continue
        try:
            info = process_info(int(root.name))
            if info["uid"] != os.geteuid():
                continue
            threads = set()
            for fd in (root / "fd").iterdir():
                try:
                    st = fd.stat()
                    key = (st.st_dev, st.st_ino)
                    if key in busy and "lock:" in (root / "fdinfo" / fd.name).read_text():
                        threads.add(busy[key])
                        found.add(key)
                except (OSError, ValueError):
                    continue
            if threads:
                codex = Path(info["exe"]).name == "codex"
                info.update(threads=sorted(threads), protected=info["pid"] in protected or not codex)
                holders.append(info)
        except (OSError, ValueError, IndexError):
            continue
    return sorted(holders, key=lambda h: h["pid"]), [v for k, v in busy.items() if k not in found]


def open_pidfd(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Some Conda builds omit os.pidfd_open even when Linux supports it.
    import ctypes
    import platform
    if platform.machine().lower() not in ("x86_64", "amd64", "aarch64", "arm64"):
        raise AuthError("当前 Python 缺少 pidfd_open，请改用支持该接口的 Python。")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0))
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def terminate_holder(holder):
    """Use pidfds to avoid accidentally signalling a recycled PID."""
    if holder["protected"] or holder["pid"] in ancestors():
        raise AuthError("当前命令的父进程、PID 1 和非 Codex 进程受保护，需手动关闭对应客户端。")
    if not hasattr(signal, "pidfd_send_signal"):
        raise AuthError("当前 Python / 内核不支持 pidfd，请手动关闭上面列出的 Codex 进程。")
    try:
        fd = open_pidfd(holder["pid"])
    except ProcessLookupError:
        return
    try:
        current = process_info(holder["pid"])
        if any(current[k] != holder[k] for k in ("start", "exe", "uid")):
            raise AuthError("进程已变化，请重新运行 unlock 检查。")
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError):
        return
    finally:
        os.close(fd)


def unlock(store, args):
    holders, unknown = lock_inventory(store.home)
    for holder in holders:
        print(f"PID {holder['pid']}  {len(holder['threads'])} 个对话锁  "
              f"{'受保护，请手动关闭客户端' if holder['protected'] else '可结束'}")
        for thread in holder["threads"]:
            print(f"  {thread}")
    if unknown:
        print(f"有 {len(unknown)} 个锁的占用进程不可见。请在宿主机终端运行本命令。")
    if not holders and not unknown:
        print("没有被占用的对话锁，无需解锁。未占用的 .lock 文件可正常保留。")
        return
    if args.dry_run:
        return
    eligible = [h for h in holders if not h["protected"]]
    if not eligible:
        raise AuthError("没有可自动结束的占用进程。请关闭对应客户端后重试。")
    if not args.all:
        chosen = select([str(h["pid"]) for h in eligible], "选择要释放会话的进程：")
        eligible = [h for h in eligible if str(h["pid"]) == chosen]
    print("结束这些进程会中断它们正在生成的回复，同一进程中的其他对话也会断开。")
    if not args.yes:
        if not sys.stdin.isatty():
            raise AuthError("实际解锁需要交互确认；脚本中请使用 --all --yes。")
        if input("输入 yes 结束上面选中的进程：").strip().lower() != "yes":
            raise AuthError("已取消。")
    # Recheck actual ownership after the confirmation; never kill an idle server.
    fresh, _ = lock_inventory(store.home)
    for holder in eligible:
        if any(h["pid"] == holder["pid"] and h["start"] == holder["start"] for h in fresh):
            terminate_holder(holder)
    deadline = time.monotonic() + 4
    selected = {(h["pid"], h["start"]) for h in eligible}
    while True:
        remaining, unknown = lock_inventory(store.home)
        remaining = [h for h in remaining if (h["pid"], h["start"]) in selected]
        if not remaining:
            print("所选进程已释放对话锁。可重新打开对话；客户端重启后可能重新获取锁。")
            return
        if time.monotonic() >= deadline:
            raise AuthError("部分进程仍占用对话锁，请手动退出对应客户端；未强制结束进程。")
        time.sleep(0.2)


def self_update():
    root = Path(__file__).resolve().parent
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True)
        if result.returncode:
            raise AuthError("Git 操作失败。请检查仓库上游、网络和工作区状态后重试。")
        return result.stdout.strip()
    if git("rev-parse", "--show-toplevel") != str(root):
        raise AuthError("self-update 需要从独立的 Git 克隆仓库安装。")
    if git("status", "--porcelain"):
        raise AuthError("工作区有未提交修改，请先保存修改后更新。")
    git("rev-parse", "--abbrev-ref", "@{upstream}")
    git("pull", "--ff-only")
    print("命令已从 Git 上游更新，下次运行生效。")


def parser():
    p = argparse.ArgumentParser(description="Codex 本地账号管理（Python 3.11+，无第三方依赖）")
    p.add_argument("--home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
                   help="Codex 数据目录，默认 CODEX_HOME 或 ~/.codex")
    p.add_argument("--version", action="version", version=VERSION)
    commands = p.add_subparsers(dest="command")
    for command, help_text in (("add", "输入名字和 JSON，新增账号"),
                               ("update", "替换已保存账号的 JSON 凭据")):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("name", nargs="?")
        source = sub.add_mutually_exclusive_group()
        source.add_argument("--file", help="从 JSON 文件导入")
        source.add_argument("--stdin", action="store_true", help="从标准输入导入")
        sub.add_argument("--index", type=int, help="JSON 数组中要导入的序号，从 1 开始")
    for command, aliases, help_text in (("switch", ["use"], "列表选择并切换账号"),
                                       ("remove", ["rm"], "移除保存的账号")):
        sub = commands.add_parser(command, aliases=aliases, help=help_text)
        sub.add_argument("name", nargs="?")
    commands.add_parser("list", aliases=["ls"], help="列出账号，* 表示当前账号")
    commands.add_parser("migrate", help="导入旧版 auth-switch.registry，保留旧文件")
    commands.add_parser("self-update", help="通过 git pull --ff-only 更新命令")
    sub = commands.add_parser("unlock", help="检查并释放被 Codex 进程占用的对话锁（Linux）")
    sub.add_argument("--all", action="store_true", help="选择所有可结束的占用进程")
    sub.add_argument("--dry-run", action="store_true", help="仅查看，不结束进程")
    sub.add_argument("--yes", action="store_true", help="已确认中断所选进程，跳过确认提示")
    return p


def menu():
    choices = {"switch": "切换账号", "add": "新增账号（名字 + JSON）", "update": "更新账号凭据",
               "list": "列出账号", "remove": "移除保存的账号", "unlock": "解锁所有对话进程",
               "self-update": "更新 codex-auth 命令"}
    return select(list(choices), "codex-auth", list(choices.values()))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Preserve original `codex-auth NAME` and `codex-auth NAME PATH` shortcuts.
    known = {"add", "update", "switch", "use", "remove", "rm", "list", "ls", "migrate",
             "self-update", "unlock", "help"}
    prefix = []
    if argv[:1] == ["--home"] and len(argv) >= 2:
        prefix, argv = argv[:2], argv[2:]
    if argv and not argv[0].startswith("-") and argv[0] not in known:
        if len(argv) == 1:
            argv = ["switch", argv[0]]
        elif len(argv) == 2:
            argv = ["add", argv[0], "--file", argv[1]]
    if argv == ["help"]:
        argv = ["--help"]
    args = parser().parse_args(prefix + argv)
    store = Store(args.home)
    if args.command is None:
        args.command = menu()
        if args.command == "unlock":
            args.all, args.yes, args.dry_run = True, False, False
    if args.command in ("add", "update"):
        name = getattr(args, "name", None)
        if args.command == "update":
            name = store.choose(name)
        elif not name:
            if not sys.stdin.isatty():
                raise AuthError("非交互模式请提供账号名称。")
            name = input("账号名称：").strip()
        valid_name(name)
        store.save(name, credential_input(args), update=args.command == "update")
    elif args.command in ("switch", "use"):
        store.switch(store.choose(getattr(args, "name", None)))
    elif args.command in ("remove", "rm"):
        store.remove(store.choose(getattr(args, "name", None)))
    elif args.command in ("list", "ls"):
        active = store.active()
        for name in store.names():
            print(f"{'*' if name == active else ' '} {name}  {describe(store.get(name))}")
        if not store.names():
            print("尚无账号。运行 codex-auth add，或 codex-auth migrate 导入旧账号。")
    elif args.command == "migrate":
        store.migrate()
    elif args.command == "unlock":
        unlock(store, args)
    elif args.command == "self-update":
        self_update()
    return 0


def entrypoint():
    try:
        return main()
    except AuthError as exc:
        print(f"codex-auth: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ncodex-auth: 已取消。", file=sys.stderr)
        return 130
    except (OSError, UnicodeError):
        # Avoid exception dumps that might contain credential data or remote URLs.
        print("codex-auth: 操作失败，请检查文件权限、路径和进程访问权限。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint())
