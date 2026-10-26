#!/usr/bin/env python3
"""Install launchers without changing shell startup files or copying secrets to the repo."""
from datetime import datetime, timezone
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def install():
    parser = argparse.ArgumentParser(description="安装 codex-auth，并迁移旧版账号")
    parser.add_argument("--home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--bin-dir", default=str(Path.home() / ".local" / "bin"))
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("需要 Python 3.11 或更高版本。")
    source = Path(__file__).resolve().parent / "codex_auth.py"
    home = Path(args.home).expanduser().absolute()
    bin_dir = Path(args.bin_dir).expanduser().absolute()
    launcher = home / "codex-auth"
    link = bin_dir / "codex-auth"
    if home.is_symlink() or bin_dir.is_symlink() or launcher.is_symlink():
        parser.error("请为安装位置提供普通目录和普通启动脚本。")
    if link.exists() and not (link.is_symlink() and link.resolve() == launcher):
        parser.error(f"{link} 已存在且不是本工具的启动链接，请先移走该文件。")
    # Migration validates credentials before the existing command is replaced.
    subprocess.run([sys.executable, str(source), "--home", str(home), "migrate"], check=True)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = ("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(source)) +
              " --home " + shlex.quote(str(home)) + ' "$@"\n')
    if launcher.exists() and launcher.read_text() != script:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = home / ("codex-auth.legacy-" + stamp)
        shutil.copyfile(launcher, backup)
        backup.chmod(0o700)
        print(f"旧命令备份：{backup}")
    fd, tmp = tempfile.mkstemp(prefix=".codex-auth-install-", dir=home)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(script)
            os.fchmod(out.fileno(), 0o700)
        os.replace(tmp, launcher)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if not link.is_symlink():
        link.symlink_to(launcher)
    print(f"已安装：{launcher}\n命令链接：{link}\n源代码：{source.parent}")
    print("已有 ~/.codex/codex-auth 的 shell 别名可直接继续使用。新终端直接运行 codex-auth。")


if __name__ == "__main__":
    try:
        install()
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        print("安装失败，请检查权限和旧版账号数据。", file=sys.stderr)
        sys.exit(1)
