# codex-auth

Codex 本地多账号管理命令。直接输入名称、粘贴 Cockpit Tools 导出的 JSON，即可生成 Codex 的认证文件；切换时从终端列表选择账号。

Python 3.11+，仅使用标准库。认证管理支持 Linux / macOS；会话进程解锁支持 Linux。此项目是独立工具。

## 安装

```bash
git clone <你的仓库地址> codex-auth
cd codex-auth
python3 install.py
codex-auth
```

也可直接运行 `./bin/codex-auth`。安装器将启动脚本写入 `~/.codex/codex-auth`，并在 `~/.local/bin/codex-auth` 创建链接；请确保 `~/.local/bin` 已在 PATH 中。旧的 `alias codex-auth="$HOME/.codex/codex-auth"` 可继续使用，无须修改 `.bashrc`。

安装会迁移旧版 `auth-switch.registry`，保留旧登记表、账号文件及旧命令备份。已有同名新账号不会被旧记录覆盖。源码仍在本仓库，移动仓库后请重新安装。

自定义安装：`python3 install.py --home /path/to/codex --bin-dir /path/to/bin`。安装器优先采用环境变量 `CODEX_HOME`，安装后的启动器固定使用安装时选定的数据目录。直接调用源码时，可用 `CODEX_HOME` 或命令前的 `--home PATH` 指定目录。

## 使用

`codex-auth` 打开数字菜单，输入序号选择操作。`codex-auth switch` 列出账号，输入序号或名称切换。空输入取消；纯数字与列表序号冲突时，序号优先，可用 `codex-auth switch 19` 明确选择名称为 `19` 的账号。

```bash
codex-auth add                 # 先输入名称，再粘贴 JSON，隐藏输入
codex-auth add 工作账号         # 指定名称，直接粘贴 JSON
codex-auth switch              # 从列表选择
codex-auth switch 工作账号      # 直接切换
codex-auth 工作账号             # 兼容旧版切换快捷方式
codex-auth list                # * 表示当前账号
codex-auth update              # 选择账号，再粘贴新的 JSON 凭据
codex-auth update 工作账号      # 更新指定账号；当前账号会同步 auth.json
codex-auth remove 工作账号      # 移除保存的账号，保留当前 auth.json
codex-auth migrate             # 手动迁移旧登记表
codex-auth self-update         # 从已配置的 Git 上游更新本工具
codex-auth --help
```

粘贴完整 JSON 后，在最后一个 `]` 或 `}` 后按回车即可自动结束。JSON 未完成时，空行结束并报错，Ctrl-C 取消。有效输入不会显示到终端，也不会进入 shell 历史。

支持 Cockpit Tools 单个对象、对象数组以及原生 Codex `auth.json`。数组含多个账号时逐次选择要导入的账号。脚本导入可用：

```bash
codex-auth add demo --stdin < /安全位置/cockpit-export.json
codex-auth add demo --file /安全位置/cockpit-export.json --index 2
codex-auth demo /安全位置/原生-auth.json  # 兼容旧式 NAME PATH，转换并保存独立副本
```

`--index` 从 1 开始。非交互模式必须指定名称，并使用 `--stdin` 或 `--file`；多账号数组必须指定 `--index`。凭据不应作为命令行参数。

转换后只保存以下结构，令牌和原 `last_refresh` 原样保留；缺少 `last_refresh` 时采用导入时间，缺少 `account_id` 时尝试从 JWT 元数据读取：

```json
{
  "auth_mode": "chatgpt",
  "OPENAI_API_KEY": null,
  "tokens": {
    "id_token": "FAKE_ID_TOKEN",
    "access_token": "FAKE_ACCESS_TOKEN",
    "refresh_token": "FAKE_REFRESH_TOKEN",
    "account_id": "FAKE_ACCOUNT_ID"
  },
  "last_refresh": "2026-10-21T01:18:13.000Z"
}
```

本工具做格式转换，不验证 JWT 签名、不验证账号能否登录，也不主动联网刷新令牌。可用性由 Codex 验证。已登录的 Codex 会自动刷新凭据，因此切换前会将当前 `auth.json` 中的令牌回存至对应账号，避免下次切回旧 refresh token。外部登录产生了不同身份时，不会回写到原账号。

## 对话进程解锁

```bash
codex-auth unlock --dry-run     # 仅查看锁及占用 PID
codex-auth unlock               # 选择一个占用进程，确认后释放
codex-auth unlock --all         # 选择所有可结束的占用进程，确认后释放
codex-auth unlock --all --yes   # 明确允许中断所选进程，供脚本使用
```

针对 `thread-writer-locks/*.lock` 的实际占用检查，不以文件存在判断对话被锁。程序通过 Linux `/proc` 找到持锁文件描述符，再使用 pidfd 向当前用户的 Codex 原生进程发送 SIGTERM；整个进程中的所有会话都会断开，正在生成的回复会中断。客户端重启可能重新占用会话。不会删除锁文件或 SQLite 数据库，也不会强制 SIGKILL。没有占用的 `.lock` 文件可以保留。

当前命令的父进程、PID 1、其他用户进程以及非 Codex 进程受保护，需手动关闭客户端；容器内进程不可见时请在宿主机终端运行。此功能释放本机对话写入锁，不修改历史对话的账号归属或绕过服务端权限。

## 文件与备份

```text
~/.codex/
  auth.json                    当前 Codex 凭据
  auth-profiles/
    工作账号.json               保存的账号
    .active                    当前账号标记
    .lock                      本工具的并发写入锁
  auth-backups/
    auth-时间戳.json             覆盖当前凭据前的备份
```

私有目录权限为 `700`，认证文件和备份为 `600`，写入使用同目录临时文件及原子替换。备份只保存在本机，不自动清理，需按自己的恢复需求管理。切换不会终止客户端；已打开的 Codex 可能仍缓存旧身份，需要重新加载客户端。

本工具适用于文件凭据存储。若配置了 `keyring`、`auto` 或 `ephemeral`，请先在 `~/.codex/config.toml` 顶层设置：

```toml
cli_auth_credentials_store = "file"
```

关于 Codex 的凭据存储与令牌刷新行为，见 [OpenAI 官方认证文档](https://learn.chatgpt.com/docs/auth)。

本工具自身的锁用于串行化多个 `codex-auth` 命令，Codex 客户端不会参与这把锁。请避免在客户端正在刷新凭据时同时切换账号；存在客户端与工具并发写入的时间窗口。

## 更新与发布

`update` 更新账号凭据，`self-update` 更新命令源码。后者要求本项目是独立 Git 仓库、工作区干净、分支已配置上游，执行 `git pull --ff-only`，不丢弃本地修改。源码更新后下次运行即生效。

上传 GitHub 前，仅提交本项目的源码、测试和文档。账号数据始终在仓库外的 Codex 数据目录，`.gitignore` 另行忽略 JSON、数据库、认证备份与环境文件。不要将自己的导出文件、令牌、登记表或 `~/.codex` 放进仓库。

```bash
python3 -m unittest discover -s tests -v
# 首次使用 Git 时，在当前仓库设置自己的提交身份：
git config user.name "你的名字"
git config user.email "你的 GitHub 邮箱或 noreply 邮箱"
git add README.md LICENSE .gitignore .github codex_auth.py install.py bin tests
git commit -m "Add interactive Codex account manager"
git remote add origin <你的仓库地址>
git push -u origin main
```

MIT License。
