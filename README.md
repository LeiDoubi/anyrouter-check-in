# Any Router 多账号自动签到

[![GitHub Actions](https://github.com/millylee/anyrouter-check-in/workflows/PR%20Quality%20Checks/badge.svg)](https://github.com/millylee/anyrouter-check-in/actions)
[![codecov](https://codecov.io/gh/millylee/anyrouter-check-in/branch/main/graph/badge.svg)](https://codecov.io/gh/millylee/anyrouter-check-in)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/millylee/anyrouter-check-in/main.svg)](https://results.pre-commit.ci/latest/github/millylee/anyrouter-check-in/main)
[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/github/license/millylee/anyrouter-check-in)](LICENSE)

多平台多账号自动签到，理论上支持所有 NewAPI、OneAPI 平台，目前内置支持 Any Router 与 Agent Router，其它可根据文档进行摸索配置。

推荐搭配使用[Auo](https://github.com/millylee/auo)，支持任意 Claude Code Token 切换的工具。

**维护开源不易，如果本项目帮助到了你，请帮忙点个 Star，谢谢!**

用于 Claude Code 中转站 Any Router 网站多账号每日签到，一次 $25，限时注册即送 100 美金，[点击这里注册](https://anyrouter.top/register?aff=gSsN)。业界良心，支持 Claude Sonnet 4.5、GPT-5-Codex、Claude Code 百万上下文（使用 `/model sonnet[1m]` 开启），`gemini-2.5-pro` 模型。

## 功能特性

- ✅ 多平台（兼容 NewAPI 与 OneAPI）
- ✅ 单个/多账号自动签到
- ✅ 多种机器人通知（可选）
- ✅ 绕过 WAF 限制
- ✅ 本地账号管理：一条命令查看各账号余额，并为 Claude Code / Codex 切换 `auth_token`
- ✅ 一键同步：把本地 `.accounts.json` 与通知配置推送到 GitHub Actions Secrets（自动从 session 提取 `api_user`）
- ✅ Linux.do 自动浏览：Playwright 模拟浏览帖子、随机点赞（[`linuxdo-browser`](docs/linuxdo-browser.md)）

## 使用方法

### 1. Fork 本仓库

点击右上角的 "Fork" 按钮，将本仓库 fork 到你的账户。

### 2. 获取账号信息

对于每个需要签到的账号，你需要获取：(可借助 [在线 Secrets 配置生成器](https://millylee.github.io/anyrouter-check-in/))

1. **Cookies**: 用于身份验证
2. **API User**: 用于请求头的 new-api-user 参数（自己配置其它平台时该值需要注意匹配）

#### 获取 Cookies：

1. 打开浏览器，访问 https://anyrouter.top/
2. 登录你的账户
3. 打开开发者工具 (F12)
4. 切换到 "Application" 或 "存储" 选项卡
5. 找到 "Cookies" 选项
6. 复制所有 cookies

#### 获取 API User：

按照下方图片教程操作获得。

> 💡 如果使用下方「[本地配置与一键同步](#本地配置与一键同步推荐)」的方式，`api_user` 会自动从 session 中解析，无需手动获取。

### 3. 设置 GitHub Environment Secret

1. 在你 fork 的仓库中，点击 "Settings" 选项卡
2. 在左侧菜单中找到 "Environments" -> "New environment"
3. 新建一个名为 `production` 的环境
4. 点击新建的 `production` 环境进入环境配置页
5. 点击 "Add environment secret" 创建 secret：
   - Name: `ANYROUTER_ACCOUNTS`
   - Value: 你的多账号配置数据

### 4. 多账号配置格式

支持单个与多个账号配置，可选 `name` 和 `provider` 字段：

```json
[
  {
    "name": "我的主账号",
    "cookies": {
      "session": "account1_session_value"
    },
    "api_user": "account1_api_user_id"
  },
  {
    "name": "备用账号",
    "provider": "agentrouter",
    "cookies": {
      "session": "account2_session_value"
    },
    "api_user": "account2_api_user_id"
  }
]
```

**字段说明**：

- `cookies` (必需)：用于身份验证的 cookies 数据
- `api_user` (必需)：用于请求头的 new-api-user 参数
- `provider` (可选)：指定使用的服务商，默认为 `anyrouter`
- `name` (可选)：自定义账号显示名称，用于通知和日志中标识账号

**默认值说明**：

- 如果未提供 `provider` 字段，默认使用 `anyrouter`（向后兼容）
- 如果未提供 `name` 字段，会使用 `Account 1`、`Account 2` 等默认名称
- `anyrouter` 与 `agentrouter` 配置已内置，无需填写

接下来获取 cookies 与 api_user 的值。

通过 F12 工具，切到 Application 面板，拿到 session 的值，最好重新登录下，该值 1 个月有效期，但有可能提前失效，失效后报 401 错误，到时请再重新获取。

![获取 cookies](./assets/request-session.png)

通过 F12 工具，切到 Network 面板，可以过滤下，只要 Fetch/XHR，找到带 `New-Api-User`，这个值正常是 5 位数，如果是负数或者个位数，正常是未登录。

![获取 api_user](./assets/request-api-user.png)

### 5. 启用 GitHub Actions

1. 在你的仓库中，点击 "Actions" 选项卡
2. 如果提示启用 Actions，请点击启用
3. 找到 "AnyRouter 自动签到" workflow
4. 点击 "Enable workflow"

### 6. 测试运行

你可以手动触发一次签到来测试：

1. 在 "Actions" 选项卡中，点击 "AnyRouter 自动签到"
2. 点击 "Run workflow" 按钮
3. 确认运行

![运行结果](./assets/check-in.png)

## 本地配置与一键同步（推荐）

除了在 GitHub 网页上逐个手动添加 Secret，你还可以在本地用一个 `.accounts.json` 文件统一管理所有账号，并通过脚本一键同步到 GitHub Actions。好处：

- `api_user` 自动从 session 中解析，无需在 F12 里手动查找
- 一条命令查看所有账号余额
- 一条命令为本地 Claude Code / Codex 切换账号（自动写入 `ANTHROPIC_AUTH_TOKEN` / Codex `auth.json`）

### 1. 安装依赖

```bash
uv sync
uv run playwright install chromium
```

### 2. 创建 .accounts.json

复制示例文件并填写（该文件已在 `.gitignore` 中，不会被提交）：

```bash
cp .accounts.json.example .accounts.json
```

```json
[
  {
    "name": "我的主账号",
    "cookies": {
      "session": "你的 session 值"
    },
    "auth_token": "sk-你的API令牌"
  },
  {
    "name": "备用账号",
    "provider": "agentrouter",
    "cookies": {
      "session": "你的 session 值"
    },
    "auth_token": "sk-你的API令牌"
  }
]
```

**字段说明**：

- `cookies.session`（必需）：登录后的 session 值，用于身份验证，并自动解析 `api_user`
- `auth_token`（本地切换账号时必需）：Claude Code / Codex 使用的 API 令牌（形如 `sk-...`），**仅保存在本地，不会被推送到 GitHub**
- `provider`（可选）：服务商，默认 `anyrouter`
- `name`（可选）：账号显示名称

> `api_user` 无需填写，脚本会自动从 `session` 中解析。

### 3. 查看余额与切换账号

```bash
# 交互式：列出所有账号余额，按编号切换（默认行为）
uv run anyrouter-accounts

# 仅列出账号与余额
uv run anyrouter-accounts list

# 切换到第 2 个账号
uv run anyrouter-accounts switch 2

# 查看当前激活账号
uv run anyrouter-accounts current
```

切换账号后，脚本会：

- 写入 `~/.config/anyrouter-check-in/env.sh`（包含 `ANTHROPIC_AUTH_TOKEN` 与 `ANTHROPIC_BASE_URL`）
- 写入 Codex 配置 `~/.codex/auth.json` 与 `~/.codex/config.toml`
- 在 `~/.zshrc` 中加入 `source env.sh`，并清理冲突的 `ANTHROPIC_*` 变量

当前终端立即生效：

```bash
source ~/.config/anyrouter-check-in/env.sh
```

新开终端会自动加载。

> 账号文件查找顺序：`-f` 指定的文件 → 环境变量 `ANYROUTER_ACCOUNTS_FILE` → 当前目录 `./.accounts.json` → `~/.config/anyrouter-check-in/accounts.json` → 项目根目录 `./.accounts.json`。

### 4. 一键同步到 GitHub Actions

把 `.accounts.json`（自动刷新 `api_user`，并**移除 `auth_token`**）推送到 `ANYROUTER_ACCOUNTS`，同时将 `.env` 中的通知配置推送为对应的 Secret：

```bash
# 预览将要推送的内容，不实际写入
uv run python scripts/push_accounts_secret.py --dry-run

# 推送到 origin 仓库的 production 环境
uv run python scripts/push_accounts_secret.py

# 推送的同时，把刷新后的 api_user 写回本地 .accounts.json
uv run python scripts/push_accounts_secret.py --write
```

**前置条件**：已安装并登录 [GitHub CLI](https://cli.github.com/)（`gh auth login`）。

**常用参数**：

- `-R, --repo OWNER/REPO`：指定目标仓库（默认取 `origin` 远程）
- `-e, --env`：GitHub 环境名称（默认 `production`）
- `--env-file`：通知配置文件（默认 `.env`，参考 `.env.example`）
- `--no-auto-api-user`：不自动解析 `api_user`（需在文件中手动提供）
- `--dry-run`：仅校验与预览，不实际推送

## Linux.do 自动浏览

基于 Playwright 的 [linux.do](https://linux.do) 论坛自动浏览工具（Tampermonkey 用户脚本的 Python 版）。自动滚动列表、阅读未看话题、按话题节奏点赞，登录态保存在本地。

**完整文档见：[docs/linuxdo-browser.md](docs/linuxdo-browser.md)**

```bash
# 安装 Playwright 浏览器（Chrome 不可用时的回退；推荐本机安装 Google Chrome）
uv run playwright install chromium

# 首次：浏览器登录
uv run linuxdo-browser login

# 打开交互式 TUI
uv run linuxdo-browser

# 直接开始浏览
uv run linuxdo-browser run

# 可选：快速 + 未读列表 + 限制话题数 + 每话题最多 5 页 + 至少阅读 10 分钟
uv run linuxdo-browser run --speed fast --list unread --max-topics 20 --max-topic-pages 5 --min-read-minutes 10

# 从 Chrome 复制 Cookie 导入（跳过登录）
uv run linuxdo-browser import-cookies 'session=xxx; _forum_session=yyy'

# 查看统计 / 清除记录
uv run linuxdo-browser stats
uv run linuxdo-browser clear
```

数据目录：`~/.config/linuxdo-browser/`（配置、浏览历史、浏览器 profile）。

## 执行时间

- 脚本每 6 小时执行一次（1. action 无法准确触发，基本延时 1~1.5h；2. 目前观测到 anyrouter 的签到是每 24h 而不是零点就可签到）
- 你也可以随时手动触发签到

## 注意事项

- 请确保每个账号的 cookies 和 API User 都是正确的
- 可以在 Actions 页面查看详细的运行日志
- 支持部分账号失败，只要有账号成功签到，整个任务就不会失败
- 报 401 错误，请重新获取 cookies，理论 1 个月失效，但有 Bug，详见 [#6](https://github.com/millylee/anyrouter-check-in/issues/6)
- 请求 200，但出现 Error 1040（08004）：Too many connections，官方数据库问题，目前已修复，但遇到几次了，详见 [#7](https://github.com/millylee/anyrouter-check-in/issues/7)

## 配置示例

### 基础配置（向后兼容）

假设你有两个账号需要签到，不指定 provider 时默认使用 anyrouter：

```json
[
  {
    "cookies": {
      "session": "abc123session"
    },
    "api_user": "user123"
  },
  {
    "cookies": {
      "session": "xyz789session"
    },
    "api_user": "user456"
  }
]
```

### 多服务商配置

如果你需要同时使用多个服务商（如 anyrouter 和 agentrouter）：

```json
[
  {
    "name": "AnyRouter 主账号",
    "provider": "anyrouter",
    "cookies": {
      "session": "abc123session"
    },
    "api_user": "user123"
  },
  {
    "name": "AgentRouter 备用",
    "provider": "agentrouter",
    "cookies": {
      "session": "xyz789session"
    },
    "api_user": "user456"
  }
]
```

## 自定义 Provider 配置（可选）

默认情况下，`anyrouter`、`agentrouter` 已内置配置，无需额外设置。如果你需要使用其他服务商，可以通过环境变量 `PROVIDERS` 配置：

### 基础配置（仅域名）

大多数情况下，只需提供 `domain` 即可，其他路径会自动使用默认值：

```json
{
  "customrouter": {
    "domain": "https://custom.example.com"
  }
}
```

### 完整配置（自定义路径）

如果服务商使用了不同的 API 路径、请求头或需要 WAF 绕过，可以额外指定：

```json
{
  "customrouter": {
    "domain": "https://custom.example.com",
    "login_path": "/auth/login",
    "sign_in_path": "/api/checkin",
    "user_info_path": "/api/profile",
    "api_user_key": "New-Api-User",
    "bypass_method": "waf_cookies",
    "waf_cookie_names": ["acw_tc", "cdn_sec_tc", "acw_sc__v2"]
  }
}
```

**关于 `bypass_method`**：

- 不设置或设置为 `null`：直接使用用户提供的 cookies 进行请求（适合无 WAF 保护的网站）
- 设置为 `"waf_cookies"`：使用 Playwright 打开浏览器获取 WAF cookies 后再进行请求（适合有 WAF 保护的网站）

> 注：`anyrouter` 和 `agentrouter` 已内置默认配置，无需在 `PROVIDERS` 中配置

### 在 GitHub Actions 中配置

1. 进入你的仓库 Settings -> Environments -> production
2. 添加新的 secret：
   - Name: `PROVIDERS`
   - Value: 你的 provider 配置（JSON 格式）

**字段说明**：

- `domain` (必需)：服务商的域名
- `login_path` (可选)：登录页面路径，默认为 `/login`（仅在 `bypass_method` 为 `"waf_cookies"` 时使用）
- `sign_in_path` (可选)：签到 API 路径，默认为 `/api/user/sign_in`
- `user_info_path` (可选)：用户信息 API 路径，默认为 `/api/user/self`
- `api_user_key` (可选)：API 用户标识请求头名称，默认为 `new-api-user`
- `bypass_method` (可选)：WAF 绕过方法
  - `"waf_cookies"`：使用 Playwright 打开浏览器获取 WAF cookies 后再执行签到
  - 不设置或 `null`：直接使用用户 cookies 执行签到（适合无 WAF 保护的网站）
- `waf_cookie_names` (可选)：绕过 WAF 所需 cookie 的名称列表，`bypass_method` 为 `waf_cookies` 时必须设置

**配置示例**（完整）：

```json
{
  "customrouter": {
    "domain": "https://custom.example.com",
    "login_path": "/auth/login",
    "sign_in_path": "/api/checkin",
    "user_info_path": "/api/profile",
    "api_user_key": "x-user-id",
    "bypass_method": "waf_cookies"
  }
}
```

**内置配置说明**：

- `anyrouter`：
  - `bypass_method: "waf_cookies"`（需要先获取 WAF cookies，然后执行签到）
  - `sign_in_path: "/api/user/sign_in"`
- `agentrouter`：
  - `bypass_method: null`（直接使用用户 cookies 执行签到）
  - `sign_in_path: "/api/user/sign_in"`

**重要提示**：

- `PROVIDERS` 是可选的，不配置则使用内置的 `anyrouter` 和 `agentrouter`
- 自定义的 provider 配置会覆盖同名的默认配置

## 开启通知

脚本支持多种通知方式，可以通过配置以下环境变量开启，如果 `webhook` 有要求安全设置，例如钉钉，可以在新建机器人时选择自定义关键词，填写 `AnyRouter`。

### 邮箱通知(STMP)

- `EMAIL_USER`: 发件人邮箱地址/STMP 登录地址
- `EMAIL_PASS`: 发件人邮箱密码/授权码
- `EMAIL_SENDER`: 邮件显示的发件人地址(可选，默认: EMAIL_USER)
- `CUSTOM_SMTP_SERVER`: 自定义发件人 SMTP 服务器(可选)
- `EMAIL_TO`: 收件人邮箱地址

### 钉钉机器人

- `DINGDING_WEBHOOK`: 钉钉机器人的 Webhook 地址

### 飞书机器人

- `FEISHU_WEBHOOK`: 飞书机器人的 Webhook 地址

### 企业微信机器人

- `WEIXIN_WEBHOOK`: 企业微信机器人的 Webhook 地址

### PushPlus 推送

- `PUSHPLUS_TOKEN`: PushPlus 的 Token

### Server 酱

- `SERVERPUSHKEY`: Server 酱的 SendKey

### Telegram Bot

- `TELEGRAM_BOT_TOKEN`: Telegram Bot 的 Token
- `TELEGRAM_CHAT_ID`: Telegram Chat ID

### Gotify 推送

- `GOTIFY_URL`: Gotify 服务的 URL 地址（例如: https://your-gotify-server/message）
- `GOTIFY_TOKEN`: Gotify 应用的访问令牌
- `GOTIFY_PRIORITY`: Gotify 消息优先级 (1-10, 默认为 9)

### Bark 推送

- `BARK_KEY`: Bark 应用的 Key（APP 打开时即可看到）
- `BARK_SERVER`: 自建 Bark 服务器地址 (可选，默认: https://api.day.app)

配置步骤：

1. 在仓库的 Settings -> Environments -> production -> Environment secrets 中添加上述环境变量
2. 每个通知方式都是独立的，可以只配置你需要的推送方式
3. 如果某个通知方式配置不正确或未配置，脚本会自动跳过该通知方式
4. 如果使用上方的「本地配置与一键同步」，也可以把这些变量写进 `.env`（参考 `.env.example`），运行 `push_accounts_secret.py` 时会自动同步到 GitHub

## 故障排除

如果签到失败，请检查：

1. 账号配置格式是否正确
2. cookies 是否过期
3. API User 是否正确
4. 网站是否更改了签到接口
5. 查看 Actions 运行日志获取详细错误信息

## 本地开发环境设置

如果你需要在本地测试或开发签到脚本本身，请按照以下步骤设置：

> 仅想在本地管理账号、查看余额或为 Claude Code / Codex 切换账号？请看上方「本地配置与一键同步」一节。

```bash
# 安装所有依赖
uv sync --dev

# 安装 Playwright 浏览器
uv run playwright install chromium

# 创建 .env 文件并配置（注意：JSON 必须是单行格式）
# 示例：
# ANYROUTER_ACCOUNTS=[{"name":"账号1","cookies":{"session":"xxx"},"api_user":"12345"}]
# PROVIDERS={"agentrouter":{"domain":"https://agentrouter.org"}}

# 运行签到脚本
uv run checkin.py
```

## 测试

```bash
uv sync --dev

# 安装 Playwright 浏览器
uv run playwright install chromium

# 运行测试
uv run pytest tests/

# 查看测试覆盖率
uv run pytest tests/ --cov=. --cov-report=html
```

## 贡献指南

欢迎贡献代码！在提交 Pull Request 之前，请阅读[贡献指南](CONTRIBUTING.md)。

### 代码质量

本项目使用以下工具确保代码质量：

- **Ruff**: 代码风格检查和格式化
- **MyPy**: 静态类型检查
- **Bandit**: 安全漏洞扫描
- **Pytest**: 自动化测试
- **pre-commit**: Git 提交前自动检查

所有 Pull Request 会自动运行以下检查：

- ✅ 代码风格检查（Ruff Lint & Format）
- ✅ 类型检查（MyPy）
- ✅ 安全扫描（Bandit）
- ✅ 测试运行（Pytest）
- ✅ 测试覆盖率报告（Codecov）

### 本地开发

```bash
# 安装开发依赖
uv sync --dev

# 安装 pre-commit 钩子
uv run pre-commit install

# 运行代码检查
uv run ruff check .
uv run ruff format .
uv run mypy .
uv run bandit -r . -c pyproject.toml

# 运行测试
uv run pytest tests/ --cov=.
```

## 免责声明

本脚本仅用于学习和研究目的，使用前请确保遵守相关网站的使用条款.
