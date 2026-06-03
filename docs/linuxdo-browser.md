# Linux.do Playwright 测试助手

基于 Playwright 的 Linux.do 论坛流程测试工具，用于自有论坛环境中验证登录、浏览话题、帖子阅读记录、点赞上限、草稿/升级进度等后台逻辑是否正常。

工具使用真实浏览器 profile，不新增验证码绕过逻辑；若测试环境启用了验证码，需要人工完成后再继续。

## 功能

- 多账号 profile：每个账号独立保存登录态
- 浏览流程：打开列表、进入未浏览话题、滚动阅读回复
- 本地统计：记录话题、帖子、阅读分钟、运行 session
- 保守限额：支持单次和本地每日话题/点赞上限
- 话题级点赞：进入话题时决定是否点赞，优先点赞已浏览内容里正文较长的帖子
- Connect 状态：从 `https://connect.linux.do` 同步当前等级和升级进度提示
- 数据管理：支持清理账号记录和完全重置本地测试数据

## 安装

```bash
uv sync
uv run playwright install chromium
uv run linuxdo-browser --help
```

推荐本机安装 Google Chrome。脚本默认优先启动系统 Chrome，失败时回退到 Playwright Chromium。

## 怎么运行

首次使用先安装依赖并确认 CLI 可用：

```bash
uv sync
uv run playwright install chromium
uv run linuxdo-browser --help
```

添加一个命名测试账号并登录：

```bash
uv run linuxdo-browser accounts add main --target-level 3
```

浏览器打开后，在页面里完成登录；登录完成后回到终端按 Enter。登录态会保存到 `~/.config/linuxdo-browser/profiles/main/`。

运行一次浏览测试流程：

```bash
uv run linuxdo-browser run --account main --max-topics 5 --daily-topic-limit 20 --daily-like-limit 5
```

同步并查看升级状态：

```bash
uv run linuxdo-browser status --account main
```

只读取本地缓存，不打开浏览器：

```bash
uv run linuxdo-browser status --account main --offline
```

完全重置本地测试数据：

```bash
uv run linuxdo-browser reset --yes
```

如果只想使用默认账号，也可以这样跑：

```bash
uv run linuxdo-browser login
uv run linuxdo-browser run --max-topics 5
uv run linuxdo-browser status
```

## 快速开始

```bash
# 添加测试账号并打开浏览器登录
uv run linuxdo-browser accounts add main --target-level 3

# 或只登录默认账号
uv run linuxdo-browser login

# 运行一次浏览流程
uv run linuxdo-browser run --account main --max-topics 5

# 同步 connect.linux.do 的等级状态
uv run linuxdo-browser status --account main
```

登录态保存在 `~/.config/linuxdo-browser/profiles/<account>/`。

## 常用命令

| 命令 | 说明 |
|------|------|
| `linuxdo-browser accounts add/list` | 管理测试账号 |
| `linuxdo-browser login` | 打开浏览器手动登录 |
| `linuxdo-browser run` | 运行一次浏览测试 |
| `linuxdo-browser run-all` | 顺序运行所有启用账号 |
| `linuxdo-browser status` | 打开浏览器同步 Connect 状态并展示进度 |
| `linuxdo-browser status --offline` | 只读取本地缓存状态 |
| `linuxdo-browser sync-status` | 只同步 Connect 状态 |
| `linuxdo-browser stats` | 查看本地浏览统计 |
| `linuxdo-browser clear` | 清理账号活动记录和状态快照 |
| `linuxdo-browser reset --yes` | 删除全部 Linux.do 本地测试数据 |
| `linuxdo-browser import-cookies` | 从 Cookie 字符串或文件导入登录态 |

## 运行参数

```bash
uv run linuxdo-browser run \
  --account main \
  --speed normal \
  --list latest \
  --max-topics 10 \
  --max-topic-pages 5 \
  --daily-topic-limit 20 \
  --daily-like-limit 5
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--speed` | `normal` | 滚动与阅读速度：`slow` / `normal` / `fast` / `turbo` |
| `--list` | `latest` | 列表来源：`latest` / `new` / `unread` |
| `--max-topics` | `50` | 单次运行最多打开的新话题数 |
| `--max-topic-pages` | `5` | 每个话题最多浏览的视口页数 |
| `--daily-topic-limit` | `50` | 本机本地日期内最多记录的新话题数，`0` 表示不限制 |
| `--daily-like-limit` | `30` | 本机本地日期内最多点赞数，`0` 表示不限制 |
| `--like` / `--no-like` | 开启 | 是否执行点赞流程 |
| `--like-chance` | `medium` | 无每日限额时的备用点赞节奏；同时设置话题/点赞日限额时优先按两者比例调度 |
| `--headless` | 关闭 | 无头运行，验证码测试环境不推荐 |

## 配置与数据

所有本地数据位于 `~/.config/linuxdo-browser/`：

| 路径 | 说明 |
|------|------|
| `config.json` | CLI 写入的运行配置 |
| `linuxdo.sqlite3` | 账号、活动事件、运行记录、Connect 快照 |
| `profiles/` | 多账号浏览器 profile |
| `state.json` / `profile/` | 旧版兼容文件，重新初始化时可删除 |

完全重置：

```bash
uv run linuxdo-browser reset --yes
uv run linuxdo-browser login
```

## 本机定时

可用 cron 或 launchd 定时运行。示例 cron：

```cron
15 9 * * * cd /path/to/anyrouter-check-in && uv run linuxdo-browser run-all --max-topics 5 >> ~/.config/linuxdo-browser/cron.log 2>&1
45 9 * * * cd /path/to/anyrouter-check-in && uv run linuxdo-browser sync-status >> ~/.config/linuxdo-browser/cron.log 2>&1
```

如果测试环境会触发验证码，请使用有界面的本机浏览器运行，并确保登录 profile 已经完成人工验证。
