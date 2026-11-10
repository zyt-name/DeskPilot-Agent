# DeskPilot Agent（XM Agent）

基于能力契约（capability contract）的 Windows 电脑操控 Agent：把桌面观测、点击输入、视觉截图、终端与文件传输统一成 8 个能力，由本地运行时完成「观测 → 决策 → 策略检查 → 执行 → 验证」的闭环。仓库地址：<https://github.com/zyt-name/DeskPilot-Agent>。

当前版本提供：

- Windows-MCP 主通道：UIA 元素树、截图、鼠标键盘、PowerShell。
- Computer-Use MCP 视觉通道：基于截图的坐标级操作（可选）。
- xshell-mcp 终端通道：Xshell 会话桥接（可选，需本机安装 Xshell）。
- auto 模式：模型按任务自动选择通道——普通桌面任务优先 Windows-MCP，SSH / 远程 / Linux 任务走 xshell，Windows-MCP 观测失败时回退 Computer-Use（需可识图模型）。
- 发送框内按厂商分组的模型选择（仅影响本次任务）；任务标题右侧「审计模型」只列出可识图模型，默认跟随可识图的执行模型，未配置时回退到其他可识图模型。
- 视觉提速：模型可一次规划 1–4 步批量动作、截屏缩到 1280 宽并优先 JPEG、屏幕未变化时复用摘要不上送图片；UIA 能表达状态时不依赖截图。
- 回退风险审查：提交/保存/删除/覆盖、上传、`git push`、提权等难撤销操作，以及模型声明 `rollback_risk` 的 UI 动作，由顶层识图审计模型对照当前截图复核；审计模型或截图不可用时强制用户确认。
- 登录阻塞：没有提供账号密码时遇到登录页，模型必须调用 `task.await_user` 暂停任务，禁止绕过登录；UIA 登录页还会被运行时强制拦截。用户登录后在同一会话发送「继续」，即以原目标从当前屏幕接着执行。
- 本地桌面控制台（pywebview + WebView2）：任务运行、历史、AI 模型配置、Token 用量、MCP 资源检测。
- 前台独占：需要屏幕的任务运行时控制台自动最小化，把桌面让给目标应用；任务结束、失败、取消后恢复窗口，暂停（F8）与确认弹窗会临时把窗口调回前台，继续后再次让出。
- 截图提问：暂停后可直接问「这个弹窗在说什么」；截屏前自动收起控制台，答案只显示在暂停面板，不改动桌面，也不会进入任务上下文（需可识图模型）。
- 任务收尾：结束时生成中文任务总结（成功说明当前状态，失败说明原因），工具步骤折叠为「执行工具 N 次」，并发送 Windows 系统通知。
- 多厂商 AI 配置：OpenAI 兼容 / Anthropic Messages / Gemini / Ollama，API Key 使用 Windows DPAPI 加密入库。
- 第三方 MCP 资源自动初始化：启动时检测、缺失时自动下载并构建，支持版本化目录与国内镜像回退。

## 上游 MCP 仓库

本项目不内置第三方 MCP 源码。所有第三方代码在首次启动或点击「检测并修复资源」时，按下列**固定版本**下载到本地 `vendor/`，并已在 `.gitignore` 中排除。

| 组件 | 上游仓库 | 版本 | 固定提交 | 本地目录（稳定入口） | 用途 | 许可 |
|---|---|---|---|---|---|---|
| Windows-MCP | <https://github.com/CursorTouch/Windows-MCP> | 0.8.5 | `30c1472f807eefa44774a2fe23a5b10502a59f23` | `vendor/Windows-MCP-0.8.5` → `vendor/Windows-MCP` | 主桌面通道 | MIT |
| computer-use-mcp | <https://github.com/domdomegg/computer-use-mcp> | 1.8.0 | `a41523056a4373984dc1faddbe757346fe480189` | `vendor/computer-use-mcp-1.8.0` → `vendor/computer-use-mcp` | 视觉通道 | MIT |
| xshell-mcp | <https://github.com/2head1i/xshell-mcp> | 0.1.0 | `709a5f59bc18a19feb8fb2237bef664d60717961` | `vendor/xshell-mcp-0.1.0` → `vendor/xshell-mcp` | 终端通道 | 上游未声明，见仓库 README |

对应的源码归档地址（版本号与提交均来自上述仓库）：

```text
https://github.com/CursorTouch/Windows-MCP/archive/30c1472f807eefa44774a2fe23a5b10502a59f23.zip
https://github.com/domdomegg/computer-use-mcp/archive/a41523056a4373984dc1faddbe757346fe480189.zip
https://github.com/2head1i/xshell-mcp/archive/709a5f59bc18a19feb8fb2237bef664d60717961.zip
```

`config/agent.example.toml` 中的适配器命令指向稳定入口（如 `vendor/Windows-MCP/.venv/Scripts/python.exe`），因此升级版本只需切换稳定入口，不需要改配置。

## 环境自动初始化

`.venv` 不随仓库分发，`start.cmd` 会在启动前自行准备：

1. 已有 `.venv` 且解释器满足 Python 3.12+ 时直接使用。
2. 否则依次探测：`XM_AGENT_PYTHON` 环境变量 → `py` 启动器 → 注册表 `PythonCore` 记录 → `PATH` 上的 `python`/`python3` → 常见安装目录。Microsoft Store 的占位符会被跳过。
3. 找到兼容解释器（≥3.12，与 `pyproject.toml` 的 `requires-python` 一致）后自动创建 `.venv` 并安装 `.[dev,gui]` 依赖；找不到解释器或版本不兼容会提示后退出。

需要指定解释器时设置 `XM_AGENT_PYTHON=D:\path\to\python.exe`。

## 资源自动初始化

### 启动脚本

`start.cmd` 在打开窗口前执行：

```bat
.venv\Scripts\python.exe -m xm_agent resources ensure
```

- 已就绪的资源只做本地版本/入口检查，不访问网络。
- 缺失、版本不符或缺少运行环境时，自动下载、解压并构建；失败不会阻止控制台启动，可在界面中重试。
- 需要同时验证「服务能不能真的启动」时，用界面检测或 `resources check`。

### 界面检测

「设置 → MCP 资源 → 检测并修复资源」会弹出悬浮窗：

1. 显示「正在检测资源完整性」，逐项检查版本、安装阶段与入口文件。
2. 发现缺失项后自动开始下载，显示总体进度条与每项状态。
3. 结构检查通过后启动对应 MCP 服务，完成一次真实的 `initialize` + `tools/list` 握手，显示「运行可用 / 运行不可用」及工具数量。
4. 失败项会保留错误信息；点击「强制重装失败项」可跳过“已就绪”判断重新安装。

### 命令行

```powershell
# 查看状态（必需资源缺失时退出码为 1）
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent resources status

# 检测并安装全部缺失资源
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent resources ensure

# 只安装某一项
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent resources ensure --only computer_use_mcp

# 结构检查 + 真实 MCP 握手（必需资源不可用时退出码为 1）
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent resources check
```

### 版本化目录

每次安装写入 `vendor/<名称>-<版本>/`，并在其中保存 `.xm-agent-resource.json`（版本、提交、来源、安装阶段）。稳定入口优先使用 Windows 目录 junction 指向该版本目录：

```text
vendor/
  Windows-MCP-0.8.5/        # 固定版本源码 + 独立 .venv
  Windows-MCP               # junction -> Windows-MCP-0.8.5
  computer-use-mcp-1.8.0/
  computer-use-mcp          # junction -> computer-use-mcp-1.8.0
```

若机器上已有同版本的 `vendor/<名称>` 目录（例如手动 git clone），检测会识别为「本地已有安装」，不会覆盖；需要新版本时，旧目录会被移动到 `.xm-agent/resource-backup/` 后再建立 junction，避免直接删除。

## 下载源与国内镜像

下载顺序为「官方源 → 已验证镜像」，任意一个成功即停止。以下地址在 2026-11-08 实测可用。

| 类型 | 源 | 地址 | 实测 |
|---|---|---|---|
| GitHub 归档 | 官方 | `https://github.com/<repo>/archive/<sha>.zip` | 可用（本机直连不稳定） |
| GitHub 归档镜像 | ghproxy.net | `https://ghproxy.net/<官方归档 URL>` | 200 / application/zip |
| GitHub 归档镜像 | gh-proxy.com | `https://gh-proxy.com/<官方归档 URL>` | 200 / application/zip |
| GitHub 归档镜像 | ghfast.top | `https://ghfast.top/<官方归档 URL>` | 200 / application/zip |
| Python 依赖 | PyPI | `https://pypi.org/simple` | 200 |
| Python 依赖 | 清华 | `https://pypi.tuna.tsinghua.edu.cn/simple` | 200 |
| Python 依赖 | 阿里云 | `https://mirrors.aliyun.com/pypi/simple` | 200 |
| Python 依赖 | 腾讯云 | `https://mirrors.cloud.tencent.com/pypi/simple` | 200 |
| Node 依赖 | npm 官方 | `https://registry.npmjs.org` | 200 |
| Node 依赖 | npmmirror | `https://registry.npmmirror.com` | 200 |

说明：

- `xshell-mcp` 未发布到 npm（官方与 npmmirror 均返回 404），只能从 GitHub 归档安装。
- 下载的 zip 会校验结构并限制条目数/解压体积，拒绝 `..`、绝对路径、盘符与符号链接条目。
- 归档先缓存到 `.xm-agent/resources/`，构建失败重试时不会重复下载。

## 环境要求

- Windows 10/11，x64。
- Python 3.12+（项目与 Windows-MCP 0.8.5 均使用项目内 `.venv`）。
- Node.js 20+（仅 Computer-Use MCP 与 xshell-mcp 需要）。
- WebView2 Runtime（pywebview 桌面窗口）。
- 本机安装 Xshell 后，xshell-mcp 才能执行真实终端命令。

## 安装与启动

```bat
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,gui]"
start.cmd
```

`start.cmd debug` 会打开开发者工具；其他参数原样传给 `gui` 子命令。

```powershell
# 仅启动本地 API（浏览器访问打印出的 URL）
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent gui --server-only --port 8765

# 清理过期审计日志与截图（--dry-run 只统计）
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent cleanup --dry-run

# 离线 demo
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent demo

# 运行一次任务（使用 GUI 中配置的活动模型）
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent run --goal "打开记事本并输入 hello"
```

## 配置

- 主配置：`config/agent.example.toml`（适配器、策略、审计、默认模型）。
- 长任务稳定性：`[runtime]` 的 `max_steps`（默认 30）、`max_provider_retries`（默认 2，模型输出不可解析或调用失败时重试）、`provider_retry_delay`（默认 1.2 秒，仅 429/超时/连接/5xx 退避）。
- 数据保留：`[audit].keep_days`（默认 7）同时约束审计 JSONL 与 `[runtime].screenshot_dir` 下的截图；`xm-agent cleanup` 立即清理，`--dry-run` 只统计不删除，启动 `run` / `gui` 时会自动执行一次。
- 窗口让出：`[runtime].release_desktop`（默认 `true`）控制屏幕任务运行时是否最小化控制台；浏览器模式（`--server-only`）无窗口可让，该项不生效。
- Xshell 桥接配置：`config/xshell-mcp.json`（路径相对 `vendor/xshell-mcp` 解析）。
- 在 Xshell 标签页运行 `config/xshell-bridge-default.js` 前，先把其中的 `REPO_ROOT` 改为本仓库的绝对路径。
- AI 厂商、密钥提示、模型与用量保存在 `.xm-agent/app.db`（SQLite，WAL）。
- API Key 使用 DPAPI 加密；界面、日志与接口都不回显明文，只显示末尾提示。

## 目录结构

```text
src/xm_agent/
  adapters/       # Windows-MCP / Computer-Use / Xshell / Mock 适配器
  ai/             # 厂商目录、模型列表、加密、AI 服务
  providers/      # OpenAI 兼容 / Anthropic / fake
  resources/      # 第三方 MCP 清单、下载、校验、构建、检测
  ui/             # 回环 HTTP API、静态前端、pywebview 窗口
  maintenance.py  # 审计日志与截图的保留期清理
  runtime.py      # 能力契约运行时
  policy.py       # 能力白名单、命令前缀、路径限制、确认门
  storage.py      # SQLite 持久化
config/           # 配置示例
scripts/          # 只读探测脚本
tests/            # pytest 用例
start.cmd         # 资源初始化 + 桌面控制台启动
```

## 测试

```powershell
PYTHONPATH=src .venv\Scripts\python.exe -m pytest -q
```

当前 317 项用例通过，覆盖能力契约、策略、锁、审计、视觉审查与截屏缓存、批量计划、登录阻塞与继续、跨后端契约（mock / windows / computer_use / xshell）、控制台窗口接管（让出/恢复/暂停/确认联动）、截图提问（暂停校验/纯文本拒绝/失败分支）、数据保留清理、MCP 协议、适配器、AI 配置与视觉能力判定、用量来源、面板 API，以及资源清单、版本检测、镜像回退、安全解压与版本化安装。

## 规划中：内置浏览器通道（浏览器功能，未实现）

> 本节描述的是**尚未实现**的功能，不属于当前版本能力。当前版本只有桌面与终端通道。地基已验证（见下），功能代码尚未开始。

计划增加一条与桌面操控并列的执行通道：Agent 在自有的浏览器进程中完成任务，**不截取、不点击、不占用**用户桌面。

- **零额外依赖**：直接说 Chrome DevTools Protocol，WebSocket 客户端用 Python 标准库实现，**不引入 Playwright、Selenium 或任何第三方包**。打包分发后不需要 `pip install` 任何东西。
- **三种引擎**：Microsoft Edge、Google Chrome，以及内置 Chromium 兜底；其他浏览器不在支持范围。
- **独立 profile**：每个引擎一份独立数据（`.xm-agent/browser/profiles/<engine>/`），互不共享，也不读取用户日常浏览器的登录数据；首次使用某引擎需扫码一次，之后长期免登录。
- **双视觉通路**：AI 用 CDP 截图与无障碍树看页面；用户看同一页面的镜像帧（screencast → SSE → canvas）。
- **识图与元素双路点击**：DOM 引用优先，canvas 类页面用视觉坐标兜底；中文输入走 `insertText` 绕开输入法。
- **三种界面表层**：任务页侧边抽屉、全屏浏览器视图（左侧可展开对话列）、可选的独立第二窗口。
- **不抢桌面**：该通道运行时不显示操控胶囊、不最小化控制台、不调用 `DesktopHandoff`。
- **内置内核纳入资源管理**：Chromium 用 Google 官方 Chrome for Testing 构建，由 `start.cmd` 检查、按需下载，启动探针校验完整性，支持国内镜像。
- **资源更新检测**（含第三方 MCP）：查 GitHub 新版本，先过兼容门禁与探针，再切到新版本，失败保留旧版可回滚。
- **已知限制**：部分站点会拦截自动化与无头浏览器，滑块/点选验证码需转人工；不承诺绕过风控，首次启用会明示风险。

**地基已实测**（本机 Edge 157，纯标准库，零第三方依赖）：内核启动 0.16s、页面截图 133ms、坐标点击 14ms 且精确命中、实时镜像约 14fps、中文输入可用、`Browser.close` 优雅关闭后登录态完整保留（硬杀会丢，这是个真实坑）。详见 `TECH_DESIGN.md` 4.21.14 与 4.21.15。

设计与边界见 `TECH_DESIGN.md` 4.21。

## 安全与数据

- 仅监听 `127.0.0.1`，POST 请求要求随机 `X-XM-Token`；GET 也校验 Host。
- API Key 使用 Windows DPAPI（用户/机器绑定）加密，不写入日志、不返回前端。
- 第三方仓库的下载/构建需要用户确认（启动脚本与界面检测即为确认入口）；本项目不修改上游源码，只在其目录内创建 `.venv` / `node_modules` / `dist` 与资源标记文件。
- `.gitignore` 排除 `.venv/`、`.xm-agent/`、`vendor/`、构建产物与测试缓存，提交中不包含第三方源码或本地数据。

## 第三方许可

第三方组件版权归各自作者所有，使用时请遵守对应仓库许可。Windows-MCP 与 computer-use-mcp 为 MIT；xshell-mcp 上游未声明许可，请在使用前确认。本项目不重新分发这些源码，仅在运行时按固定版本下载。
