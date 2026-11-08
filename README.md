# DeskPilot Agent（XM Agent）

基于能力契约（capability contract）的 Windows 电脑操控 Agent：把桌面观测、点击输入、视觉截图、终端与文件传输统一成 7 个能力，由本地运行时完成「观测 → 决策 → 策略检查 → 执行 → 验证」的闭环。仓库地址：<https://github.com/zyt-name/DeskPilot-Agent>。

当前版本提供：

- Windows-MCP 主通道：UIA 元素树、截图、鼠标键盘、PowerShell。
- Computer-Use MCP 视觉通道：基于截图的坐标级操作（可选）。
- xshell-mcp 终端通道：Xshell 会话桥接（可选，需本机安装 Xshell）。
- 本地桌面控制台（pywebview + WebView2）：任务运行、历史、AI 模型配置、Token 用量、MCP 资源检测。
- 多厂商 AI 配置：OpenAI 兼容 / Anthropic Messages / Gemini / Ollama，API Key 使用 Windows DPAPI 加密入库。
- 第三方 MCP 资源自动初始化：启动时检测、缺失时自动下载并构建，支持版本化目录与国内镜像回退。

## 上游 MCP 仓库

本项目不内置第三方 MCP 源码。所有第三方代码在首次启动或点击「检测并修复资源」时，按下列**固定版本**下载到本地 `vendor/`，并已在 `.gitignore` 中排除。

| 组件 | 上游仓库 | 版本 | 固定提交 | 本地目录（稳定入口） | 用途 | 许可 |
|---|---|---|---|---|---|---|
| Windows-MCP | <https://github.com/CursorTouch/Windows-MCP> | 0.8.8 | `6613da7248cea1c22ac89ea9fdf74f11cfbecac8` | `vendor/Windows-MCP-0.8.8` → `vendor/Windows-MCP` | 主桌面通道 | MIT |
| computer-use-mcp | <https://github.com/domdomegg/computer-use-mcp> | 1.8.0 | `a41523056a4373984dc1faddbe757346fe480189` | `vendor/computer-use-mcp-1.8.0` → `vendor/computer-use-mcp` | 视觉通道 | MIT |
| xshell-mcp | <https://github.com/2head1i/xshell-mcp> | 0.1.0 | `709a5f59bc18a19feb8fb2237bef664d60717961` | `vendor/xshell-mcp-0.1.0` → `vendor/xshell-mcp` | 终端通道 | 上游未声明，见仓库 README |

对应的源码归档地址（版本号与提交均来自上述仓库）：

```text
https://github.com/CursorTouch/Windows-MCP/archive/6613da7248cea1c22ac89ea9fdf74f11cfbecac8.zip
https://github.com/domdomegg/computer-use-mcp/archive/a41523056a4373984dc1faddbe757346fe480189.zip
https://github.com/2head1i/xshell-mcp/archive/709a5f59bc18a19feb8fb2237bef664d60717961.zip
```

`config/agent.example.toml` 中的适配器命令指向稳定入口（如 `vendor/Windows-MCP/.venv/Scripts/python.exe`），因此升级版本只需切换稳定入口，不需要改配置。

## 资源自动初始化

### 启动脚本

`start.cmd` 在打开窗口前执行：

```bat
.venv\Scripts\python.exe -m xm_agent resources ensure
```

- 已就绪的资源只做本地版本/入口检查，不访问网络。
- 缺失、版本不符或缺少运行环境时，自动下载、解压并构建；失败不会阻止控制台启动，可在界面中重试。

### 界面检测

「设置 → MCP 资源 → 检测并修复资源」会弹出悬浮窗：

1. 显示「正在检测资源完整性」，逐项检查版本与入口文件。
2. 发现缺失项后自动开始下载，显示总体进度条与每项状态。
3. 完成后列出每项资源的版本和结果；失败项会保留错误信息。

### 命令行

```powershell
# 查看状态（必需资源缺失时退出码为 1）
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent resources status

# 检测并安装全部缺失资源
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent resources ensure

# 只安装某一项
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent resources ensure --only computer_use_mcp
```

### 版本化目录

每次安装写入 `vendor/<名称>-<版本>/`，并在其中保存 `.xm-agent-resource.json`（版本、提交、来源、安装阶段）。稳定入口优先使用 Windows 目录 junction 指向该版本目录：

```text
vendor/
  Windows-MCP-0.8.8/        # 固定版本源码 + 独立 .venv
  Windows-MCP               # junction -> Windows-MCP-0.8.8
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
- Python 3.14（Windows-MCP 要求 `>=3.14`；项目自身 `>=3.12`）。
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

# 离线 demo
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent demo

# 运行一次任务（使用 GUI 中配置的活动模型）
PYTHONPATH=src .venv\Scripts\python.exe -m xm_agent run --goal "打开记事本并输入 hello"
```

## 配置

- 主配置：`config/agent.example.toml`（适配器、策略、审计、默认模型）。
- Xshell 桥接配置：`config/xshell-mcp.json`。
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

当前 100 项用例通过，覆盖能力契约、策略、锁、审计、MCP 协议、适配器、AI 配置、用量、面板 API，以及资源清单、版本检测、镜像回退、安全解压与版本化安装。

## 安全与数据

- 仅监听 `127.0.0.1`，POST 请求要求随机 `X-XM-Token`；GET 也校验 Host。
- API Key 使用 Windows DPAPI（用户/机器绑定）加密，不写入日志、不返回前端。
- 第三方仓库的下载/构建需要用户确认（启动脚本与界面检测即为确认入口）；本项目不修改上游源码，只在其目录内创建 `.venv` / `node_modules` / `dist` 与资源标记文件。
- `.gitignore` 排除 `.venv/`、`.xm-agent/`、`vendor/`、构建产物与测试缓存，提交中不包含第三方源码或本地数据。

## 第三方许可

第三方组件版权归各自作者所有，使用时请遵守对应仓库许可。Windows-MCP 与 computer-use-mcp 为 MIT；xshell-mcp 上游未声明许可，请在使用前确认。本项目不重新分发这些源码，仅在运行时按固定版本下载。
