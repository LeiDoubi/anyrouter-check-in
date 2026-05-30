# Linux.do 自动浏览助手

基于 Playwright 的 [linux.do](https://linux.do) 自动浏览工具，对应 Tampermonkey 脚本「Linux.do 自动浏览助手 v2」的 Python 实现。

在已登录的浏览器中自动滚动列表、进入未读话题、阅读回复，并随机点赞，用于论坛活跃度维护。

## 功能

- 速度预设：`slow` / `normal` / `fast` / `turbo`
- 列表模式：`latest`（最新）/ `new`（新帖）/ `unread`（未读）
- 随机点赞（含 429 上限检测，API 失败时自动回退为点击按钮）
- 浏览记录持久化（已看话题、已点赞帖子）
- Human Verification（hCaptcha）弹窗检测与自动提交
- 优先使用系统 Chrome，降低 hCaptcha 验证失败概率

## 安装

```bash
# 安装项目依赖
uv sync

# Playwright 浏览器（Chrome 不可用时的回退）
uv run playwright install chromium
```

**推荐**：本机安装 [Google Chrome](https://www.google.com/chrome/)。脚本默认通过 `channel='chrome'` 启动，hCaptcha 兼容性明显优于 Playwright 自带 Chromium。

安装后可用以下命令验证：

```bash
uv run linuxdo-browser --help
```

## 快速开始

### 方式一：浏览器登录（推荐首次使用）

```bash
# 1. 打开 Chrome 登录 linux.do，完成后回终端按 Enter
uv run linuxdo-browser login

# 2. 开始自动浏览
uv run linuxdo-browser run
```

登录态保存在 `~/.config/linuxdo-browser/profile/`，只需登录一次。

### 方式二：导入 Cookie（跳过登录）

在 Chrome 中正常登录 linux.do，从开发者工具复制 Cookie 字符串：

```bash
uv run linuxdo-browser import-cookies 'session=xxx; _forum_session=yyy; ...'

# 或从文件读取
uv run linuxdo-browser import-cookies -f cookies.txt
```

## 命令

| 命令 | 说明 |
|------|------|
| `linuxdo-browser login` | 打开浏览器手动登录，保存 session |
| `linuxdo-browser run` | 开始自动浏览（默认命令） |
| `linuxdo-browser import-cookies` | 从 Cookie 字符串或文件导入登录态 |
| `linuxdo-browser stats` | 查看浏览/点赞统计 |
| `linuxdo-browser clear` | 清除浏览记录 |
| `linuxdo-browser config` | 查看当前配置 |

### `run` 常用参数

```bash
# 快速模式 + 未读列表 + 高点赞概率，最多浏览 20 个话题
uv run linuxdo-browser run --speed fast --list unread --like-chance high --max-topics 20

# 只浏览不点赞
uv run linuxdo-browser run --no-like

# 无头模式（不推荐，hCaptcha 容易失败）
uv run linuxdo-browser run --headless
```

| 参数 | 可选值 | 默认值 | 说明 |
|------|--------|--------|------|
| `--speed` | `slow` / `normal` / `fast` / `turbo` | `normal` | 滚动与阅读速度 |
| `--list` | `latest` / `new` / `unread` | `latest` | 话题列表来源 |
| `--like` / `--no-like` | — | 开启 | 是否随机点赞 |
| `--like-chance` | `low` / `medium` / `high` / `veryHigh` | `medium` | 点赞概率（约 5% / 15% / 25% / 40%） |
| `--max-topics` | 整数 | `50` | 单次会话最多浏览话题数 |
| `--headless` | — | 关闭 | 无头浏览器 |

## 配置与数据目录

所有数据保存在 `~/.config/linuxdo-browser/`：

| 路径 | 说明 |
|------|------|
| `config.json` | 运行配置（速度、列表、点赞等） |
| `state.json` | 浏览历史（已看话题、已点赞、累计回复数） |
| `profile/` | Playwright 持久化浏览器 profile（登录 Cookie） |

### `config.json` 字段

首次 `run` 或 `login` 后自动生成，也可手动编辑：

```json
{
  "speed": "normal",
  "list_type": "latest",
  "enable_like": true,
  "like_chance": "medium",
  "max_topics_per_session": 50,
  "max_likes_per_session": 50,
  "min_like_interval_ms": 2000,
  "return_to_list_delay_ms": 1000,
  "headless": false,
  "stuck_timeout_sec": 30,
  "use_chrome": true,
  "human_verify_timeout_sec": 300
}
```

CLI 参数会覆盖并写回 `config.json`。

## 工作流程

```text
/latest|/new|/unread
    ↓ 滚动列表，找未浏览话题
进入话题页
    ↓ 滚动阅读所有回复，随机点赞
返回列表
    ↓ 重复，直到达到 max_topics 或无新话题
```

## 常见问题

### Human Verification：hCaptcha 完成后 Verify 无法点击

Playwright Chromium 容易被 hCaptcha 识别，导致勾选完成但 Verify 按钮仍 disabled。

**处理步骤：**

1. 删除旧 profile，改用 Chrome 重新登录：

   ```bash
   rm -rf ~/.config/linuxdo-browser/profile
   uv run linuxdo-browser login
   ```

2. 完成 hCaptcha 后，脚本会自动检测 token 并点击 Verify。

3. 若仍失败，改用 Cookie 导入（见上文「方式二」）。

### 点赞失败 HTTP 403

已通过页面内 `fetch`（与用户脚本一致）发送点赞请求；若 API 仍返回 403，会自动回退为点击点赞按钮。

若持续失败，检查：

- 是否已通过 Human Verification
- 账号是否达到点赞上限（脚本会自动关闭点赞并提示 429）
- 登录态是否过期（重新 `login` 或 `import-cookies`）

### macOS 出现 `--no-sandbox` 警告

脚本在 macOS 上不会添加 `--no-sandbox`，若仍看到该警告，说明 profile 缓存了旧启动参数，删除 profile 后重试即可。

### 清除浏览记录

```bash
uv run linuxdo-browser clear
```

只清除 `state.json` 中的历史，不影响登录态。

### 完全重置

```bash
rm -rf ~/.config/linuxdo-browser
uv run linuxdo-browser login
```

## 与签到脚本的关系

`linuxdo-browser` 独立于 AnyRouter 签到流程，不读写 `.accounts.json`，也不推送 GitHub Secrets。两者可并行使用：

- `anyrouter-accounts` / `checkin.py` — AnyRouter 多账号签到
- `linuxdo-browser` — Linux.do 论坛自动浏览

## 免责声明

本工具仅用于学习和研究目的，使用前请确保遵守 [linux.do](https://linux.do) 社区规则与使用条款。请合理设置浏览速度与点赞频率，避免对服务器造成过大压力。
