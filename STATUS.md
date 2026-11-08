# 实施进度

> 最后更新：2026-11-08
> 当前阶段：P0 桌面链路（Windows-MCP 主通道 + Computer-Use MCP 视觉通道已接框架）；本地桌面控制台可用；多厂商 AI 配置、DPAPI 加密密钥与 Token 用量面板已可用；第三方 MCP 资源支持启动自动初始化、版本化安装与国内镜像回退；Xshell 仅保留框架
> 设计文档：[TECH_DESIGN.md](TECH_DESIGN.md)

## 1. 已完成

| 模块 | 文件 | 状态 | 验证方式 |
|---|---|---|---|
| 能力契约 | `src/xm_agent/contracts.py` | 完成 | 7 个能力、版本号、JSON Schema、模型提示 |
| 数据类型 | `src/xm_agent/types.py` | 完成 | 单元测试 + 端到端 mock |
| 运行时状态机 | `src/xm_agent/runtime.py` | 完成 | observe → policy → act → verify → recover |
| MCP stdio 客户端 | `src/xm_agent/mcp_stdio.py` | 完成 | 对 fake Server 与真实 Windows-MCP 完成握手与调用 |
| 通用能力适配层 | `src/xm_agent/adapters/mcp_tool.py` | 完成 | 工具名与参数重命名由配置驱动 |
| Windows-MCP 适配器 | `src/xm_agent/adapters/windows_mcp.py` | 完成 | 真实 `Snapshot` 文本解析、坐标点击、WaitFor 条件 |
| Computer-Use MCP 适配器 | `src/xm_agent/adapters/computer_use_mcp.py` | 完成 | 截图保存、坐标动作映射、视觉验证降级；真实服务待构建 |
| Xshell MCP 适配器 | `src/xm_agent/adapters/xshell_mcp.py` | 完成，框架保留 | 单测覆盖；本机未安装 Xshell，不验证真实会话 |
| Mock 执行器 | `src/xm_agent/adapters/mock.py` | 完成 | demo 与测试使用 |
| 策略引擎 | `src/xm_agent/policy.py` | 完成 | 默认拒绝、命令白名单、危险命令确认、路径根限制 |
| 租约锁 | `src/xm_agent/lock.py` | 完成 | 获取/释放/争用/过期接管/心跳 |
| 审计日志 | `src/xm_agent/audit.py` | 完成 | JSONL、敏感字段脱敏、按 trace_id 归档 |
| 模型提供方 | `src/xm_agent/providers/` | 完成 | OpenAI 兼容 + Anthropic Messages + fake / scripted；支持视觉消息与 usage 回调 |
| AI 厂商目录 | `src/xm_agent/ai/catalog.py` | 完成 | 18 个预设、OpenAI/Anthropic 协议、套餐差异（GLM Coding Plan、OpenCode Go 等）、可识图启发式 |
| 密钥加密 | `src/xm_agent/ai/crypto.py` | 完成 | Windows DPAPI + 固定 entropy，密文入库，界面只显示末尾提示 |
| 模型列表 | `src/xm_agent/ai/models.py` | 完成 | OpenAI/Anthropic/Gemini/Ollama 四种接口风格，按套餐自动选择；识别 200 业务错误、Coding Plan 回退标准端点；过滤非对话模型并标注视觉能力 |
| 预设端点隔离 | `src/xm_agent/ai/service.py`、`factory.py` | 完成 | 预设提供商的协议、Base URL、模型列表接口与凭据要求由套餐强制决定，忽略前端传值；历史错误记录在读取时自动修复 |
| AI 装配与用量 | `src/xm_agent/ai/factory.py`、`service.py` | 完成 | 活动模型装配、加密读写、连通性测试、usage 回调入库 |
| CLI | `src/xm_agent/cli.py` | 完成 | `demo` / `run` / `gui`、交互确认、UTF-8、四种适配器；`run` 自动读取本地库中的活动模型 |
| 构建装配 | `src/xm_agent/bootstrap.py` | 完成 | 优先使用活动 AI，未配置时回退配置/启发式；CLI 与 UI 共用 |
| SQLite 持久化 | `src/xm_agent/storage.py` | 完成 | 用户偏好、任务历史、AI 提供商/模型/用量；WAL，按模型聚合用量 |
| 本地面板后端 | `src/xm_agent/ui/server.py` | 完成 | 回环 HTTP、随机 token、静态资源、运行线程、事件轮询、AI 配置与用量 API |
| 桌面窗口 | `src/xm_agent/ui/window.py`、`start.cmd` | 完成 | pywebview 6.2.1 + WebView2，cmd 一键启动；启动前自动执行资源初始化 |
| 资源管理 | `src/xm_agent/resources/`、CLI `resources`、设置页「MCP 资源」 | 完成 | 固定版本清单、版本化目录 + junction、GitHub/国内镜像回退、安全解压、venv/npm 构建、检测悬浮窗 |
| 前端页面 | `src/xm_agent/ui/static/` | 完成 | 总览 / 运行 / AI 模型 / 用量 / 历史 / 设置；饼图、分模型统计与资源检测悬浮窗 |
| 配置 | `config/agent.example.toml`、`config/xshell-mcp.json` | 完成 | TOML 适配器映射、子进程 env/cwd、`model` 回退配置 |
| 只读探测脚本 | `scripts/probe_windows_mcp.py`、`scripts/probe_computer_use_mcp.py`、`scripts/probe_xshell_mcp.py` | 完成 | Windows 端到端只读；Computer-Use 已构建；Xshell 框架保留 |
| 测试 | `tests/` | 完成 | 100 个用例全部通过 |
| 测试用 MCP Server | `examples/fake_mcp_server.py` | 完成 | 无第三方依赖，stdio JSON-RPC |

### 已接入的第三方 MCP

三者都以源码方式放在 `vendor/`（已在 `.gitignore` 中忽略）。资源管理器按固定版本安装到 `vendor/<名称>-<版本>`，并用同名 junction 提供稳定入口：

| 项目 | 版本 / 位置 | 优先级 | 状态 |
|---|---|---|---|
| CursorTouch/Windows-MCP | 0.8.8，`vendor/Windows-MCP-0.8.8` → `vendor/Windows-MCP`，独立 `.venv`（Python 3.14） | 主通道 | `tools/list` 返回 21 个工具；`Snapshot` 与 observe-only 运行时通过 |
| domdomegg/computer-use-mcp | 1.8.0，`vendor/computer-use-mcp-1.8.0` → `vendor/computer-use-mcp`（Node + nut.js） | 视觉通道 | 资源管理器已完成下载与 `npm install && npm run build`；`tools/list` 返回 1 个 `computer` 工具，截图 1464×823 / 84 KB 通过 |
| 2head1i/xshell-mcp | 0.1.0，`vendor/xshell-mcp`（Node 20+，已构建 `dist/server.js`） | 框架保留 | 本机未安装 Xshell，暂停真实会话验证 |

## 2. 测试结果

执行命令：

```bash
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q
```

结果：`100 passed`

覆盖范围：

- 能力契约完整性
- 租约锁获取、释放、争用、过期接管、心跳
- 命令白名单、危险命令确认、文件路径限制
- MCP stdio 握手、工具列表、工具调用、错误工具
- Windows-MCP Snapshot 文本解析（中文控件类型、JSON 数组包裹、空窗口）
- Computer-Use 截图内容解析与落盘、坐标动作映射、滚轮方向/步长、验证降级
- 视觉消息仅在 `model.vision=true` 且存在截图时附加
- 元素 ID → 坐标解析、显式坐标、未知元素报错
- Type / Shortcut / Scroll 参数映射、WaitFor 条件构造
- Xshell `xshell_exec` 会话与毫秒超时换算、屏幕读取、SFTP 能力缺口、等待超时
- 运行时正常完成、策略拒绝、确认拒绝、连续失败、锁占用
- 配置加载与示例配置
- SQLite 用户资料读写、任务历史排序与截断
- 面板 API：token 校验、Host 校验、静态资源、任务启动/轮询/落库
- 资源清单版本固定、版本化目录/legacy 检测、过期版本识别、zip 越界拒绝、镜像回退下载与构建链接、面板资源状态/同步 API
- AI 厂商目录、GLM/OpenCode 套餐差异、可识图模型识别
- DPAPI 密钥加密往返、更新时保留旧 Key、清除 Key
- OpenAI 兼容与 Anthropic Messages 的模型拉取、连通性测试、usage 归一化
- 面板 AI API：密钥不回显、模型添加/激活、Token 用量聚合
- 活动模型优先装配、未配置时回退 mock、Ollama 无密钥模式

离线 demo：

```bash
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent demo
```

结果：3 步完成，`status=done`，审计文件写入 `.xm-agent/audit/<trace_id>.jsonl`。

真实只读探测：

```bash
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_windows_mcp.py --list
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_windows_mcp.py --snapshot --parse
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_windows_mcp.py --runtime
```

结果：

- 21 个工具，名称与源码一致（Snapshot / Click / Type / Shortcut / WaitFor / App / PowerShell / FileSystem 等）。
- `Snapshot` 返回 2.5–80 KB 文本（FastMCP 以 JSON 数组字符串返回），活动窗口、窗口列表、UIA 树可解析。
- observe-only 运行时 `status=done, steps=1`，审计与锁正常写入/释放。
- 已通过 `POSTHOG_API_KEY=""` 关闭上游遥测。

Computer-Use MCP（视觉通道，已由资源管理器构建）：

```bash
# 资源管理器会自动下载固定版本并执行 npm install / npm run build
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent resources ensure --only computer_use_mcp

PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_computer_use_mcp.py --list
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_computer_use_mcp.py --screenshot --save
```

预期：`tools/list` 只有 1 个 `computer` 工具；`--screenshot` 返回 `image_width/image_height` 与 PNG，并保存到 `.xm-agent/probe/`。实测通过：`tools: 1`，截图 `1464×823`、84007 bytes，保存到 `.xm-agent/probe/computer-use-*.png`。

Xshell MCP（框架保留，本机未安装 Xshell）：

- 服务已构建，`tools/list` 返回 6 个工具；适配器、配置、桥接脚本均保留。
- 本机没有 Xshell，暂停真实会话验证；未来在装有 Xshell 的机器上按以下步骤启用：
  1. 在 Xshell 中打开目标会话，保持标签页在前台。
  2. `工具 → 脚本 → 运行`，选择 `config/xshell-bridge-default.js`。
  3. 执行 `scripts/probe_xshell_mcp.py --sessions --status`，会话列表出现 `default` 后即可验证 `terminal.exec`。

## 3. 待完成

### P0

- [x] Windows-MCP 真实只读链路（`tools/list`、`Snapshot`、observe-only 运行时）
- [x] Computer-Use MCP 构建（资源管理器自动完成 `npm install && npm run build`）
- [x] Computer-Use `tools/list` 与截图探测
- [ ] Computer-Use 接入视觉模型，跑通“截图 → 决策 → 坐标动作 → 再截图”闭环
- [ ] 真实模型联调：OpenAI 兼容接口 + 20 个固定任务基线
- [ ] 截图采集、DPI 缩放、多显示器坐标归一化
- [ ] 确认交互从 CLI 文本升级为可复用的确认接口
- [ ] 契约测试框架：同一组用例跑所有后端

### P1

- [ ] Xshell 真实会话验证（仅在装有 Xshell 的机器上进行，框架已就绪）
- [ ] SFTP 文件传输后端（xshell-mcp 不含 SFTP 工具）
- [ ] SSH 作为第二终端后端，验证替换 Xshell 不改 Agent
- [ ] UIA 失败时切换到 Computer-Use 视觉兜底，动作后强制截图
- [ ] 失败恢复策略：重新观测、切换通道、回退、人工接管
- [ ] 截图与审计的保留策略、压缩、清理任务

### P2

- [ ] 单层 MCP 网关接入（mcpproxy-go / 1MCP 二选一）
- [ ] 多客户端接入与工具命名空间
- [ ] 桌面壳（Tauri + Python sidecar）与签名安装包
- [ ] 壳与 Runtime/MCP 层双通道更新

## 4. 已知问题

1. **本机 `python` 命令不可用**：WindowsApps 别名指向损坏路径，使用 `py` 启动器或 `.venv/Scripts/python.exe`。
2. **PyPI 官方 CDN 下载不稳定**：`files.pythonhosted.org` 连接可能被重置；资源管理器的 Python 构建会依次尝试 PyPI、清华、阿里云、腾讯云镜像，Node 构建会依次尝试 npm 官方与 npmmirror。
3. **xshell-mcp 暂不验证**：本机未安装 Xshell，服务虽已构建，但 bridge 必须在 Xshell 标签页内运行；仅保留适配器与配置框架。
4. **xshell-mcp 能力缺口**：无退出码、无 SFTP、必须在 Xshell 标签页内运行 bridge 脚本；`terminal.exec` 只能返回屏幕文本。
5. **UIA 覆盖不稳定**：DOM 型页面（如浏览器）会把大量 `word` 节点塞进 500 节点预算，某些时刻只解析出 1 个可用元素；已过滤 `word/text`，但深层控件仍可能被截断。
6. **Windows-MCP 要求 Python 3.14+**：与 Runtime 的 3.12+ 基线不同，必须分 venv / 分目录打包。
7. **真实厂商密钥尚未全面联调**：GLM Coding Plan 已用真实 Key 验证 `glm-4.5` / `glm-4.6` / `glm-5.3` 连通与用量归一化；OpenAI / Anthropic / Gemini 等仍只有本地模拟端点验证，限流、余额不足、模型下线等错误未覆盖。GLM Coding Plan 的 Anthropic 端点不提供模型列表，已自动回退到标准端点；OpenCode Zen 等网关仍可能返回空列表，此时用「使用其他模型」手动添加。
8. **DPAPI 密文与 Windows 用户绑定**：API Key 只能由保存它的 Windows 用户在同一台机器上解密；复制 `app.db` 到其他账户/机器后需要重新填写 Key。
9. **用量只统计 Token，不统计金额**：不同厂商/套餐计价不同，当前记录 prompt/completion/total 与延迟；费用换算需要维护价格表，暂未实现。
10. **模型列表差异**：网关型服务（OpenCode、方舟接入点、部分 Coding Plan）可能不提供标准 `/models`；界面已允许自定义模型与手动标记「可识图」。
11. **Computer-Use MCP 已构建，但首次初始化依赖网络与 Node.js 20+**：资源管理器会下载固定版本并执行 `npm install && npm run build`；失败时设置页会显示错误，可重试，旧目录不会被直接删除。
12. **Computer-Use 依赖 nut.js 原生模块**：Windows 下需要可用的预编译二进制，安装失败时需要回退到 UIA 主通道。
13. **Computer-Use 没有元素树**：只能靠截图坐标；语义验证必须由视觉模型完成，适配器当前返回“已截图、待视觉判断”的降级结果。
14. **锁是文件锁**：过期接管存在很小的竞争窗口；没有跨进程单调 fencing 计数；目前只支持单机。
15. **审计只写不清理**：`AuditLog.prune()` 已实现，但 CLI 尚未调度执行；截图目录同样没有清理任务。
16. **策略不是沙箱**：命令白名单是前缀匹配，无法阻止白名单命令被滥用；桌面操作仍依赖人工确认与审计。
17. **MCP 客户端是自研最小实现**：已覆盖当前所需方法，但生产环境应迁移到官方 MCP SDK。
18. **尚无桌面安装包**：已有 pywebview 本地面板与 `start.cmd`，但没有安装器、托盘、更新器；后期可按 Tauri + sidecar 迁移，UI 只依赖本地 HTTP API。
19. **vendor 目录体积大**：Windows-MCP 的 venv 约数百 MB，Computer-Use 还带 Node 依赖，分发时必须换成精简依赖或预构建运行时。
20. **面板任务历史只存元数据**：SQLite 保存目标、后端、状态、步数与原因；实时事件只保留当前进程最近 300 条，进程重启后不再提供回放（完整步骤仍在审计 JSONL）。
21. **面板确认门未做弹窗**：当前只有“自动确认”与“一律拒绝”两种模式；需要人工确认的动作必须开自动确认，后续接入可复用确认接口。

## 5. 下一步

1. 确认并授权构建 `domdomegg/computer-use-mcp`，跑 `tools/list` 与一次只读截图，校准返回结构与坐标。
2. 用视觉模型跑通 Computer-Use 的“截图 → 决策 → 坐标动作 → 再截图”闭环。
3. 继续以 Windows-MCP 为主通道，先跑 3 个固定任务，再扩到 20 个基线；UIA 失败时再切视觉通道。
4. 补截图与坐标归一化（DPI、多显示器），为通道切换做准备。
5. Xshell 等有安装环境的机器再做真实会话验证；当前不阻塞主线。
6. 再决定是否引入网关与多客户端。
7. 用真实厂商 Key 验证模型列表、限流与错误提示；对不提供 `/models` 的网关完善手动模型流程。
8. 如需费用面板，再增加各厂商/套餐价格表，把 Token 换算成金额。

## 6. 运行方式

```bash
# 首次安装开发与面板依赖
.venv/Scripts/python.exe -m pip install -e ".[dev,gui]"

# 一键启动桌面控制台（推荐；启动前自动检测并初始化 MCP 资源）
start.cmd

# 资源状态与初始化
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent resources status
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent resources ensure
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent resources ensure --only computer_use_mcp

# 桌面控制台内的资源流程：
# 「设置」→「MCP 资源」→「检测并修复资源」
# 悬浮窗显示检测进度；缺失项自动从 GitHub 或已验证镜像下载，按版本安装到 vendor/<名称>-<版本>

# 桌面控制台内的 AI 配置流程：
# 1) 「AI 模型」→ 选择厂商与套餐 → 填入 API Key（协议/URL/模型列表接口自动配置）
# 2) 点击「获取模型列表」拉取官方模型，在「模型名称」下拉中选择
# 3) 需要非列表内模型时选「使用其他模型」，填写 ID 并勾选是否识图
# 4) 「保存并测试」发送一次真实请求，「用量」查看 Token 与占比
# 5) 仅「自定义 / 其他」端点才会显示协议、Base URL 与模型列表接口

# 启动时打开开发者工具
start.cmd debug

# 不打开窗口，只启动本地 API（浏览器访问打印出的 URL）
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent gui --server-only --port 8765

# 直接以 Python 启动桌面窗口
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent gui --config config/agent.example.toml

# 离线 demo
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent demo

# 使用默认 mock 配置运行任务
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent run --goal "输入 hello 并保存"

# Windows-MCP 只读探测
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_windows_mcp.py --list
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_windows_mcp.py --snapshot --parse
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_windows_mcp.py --runtime

# Computer-Use MCP 探测（资源管理器已构建；如缺失先执行 resources ensure）
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_computer_use_mcp.py --list
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_computer_use_mcp.py --screenshot --save

# Xshell MCP 探测（本机未安装 Xshell，仅保留框架）
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_xshell_mcp.py --list --sessions --status

# Xshell 内加载桥接：工具 → 脚本 → 运行 config/xshell-bridge-default.js
# 然后再执行上面的探测，会话列表应包含 "default"；随后可跑只读命令
PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_xshell_mcp.py --exec pwd

# 使用本地库中的活动模型运行（GUI 配置后自动生效）
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent run --goal "打开记事本并输入 hello"

# 指定数据库或回退到 config/agent.example.toml 的 [model]
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent run \
  --db .xm-agent/app.db \
  --config config/agent.example.toml \
  --adapter windows \
  --goal "打开记事本并输入 hello"

# 视觉通道（Computer-Use MCP 已构建；在「AI 模型」中选择标注「可识图」的模型）
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent run \
  --config config/agent.example.toml \
  --adapter computer_use \
  --goal "打开记事本并输入 hello"

# 测试
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q
```

## 7. 变更日志

- 2026-11-08：完成 P0 骨架、能力契约、MCP stdio 客户端、mock 适配器、策略、锁、审计、CLI 与 29 个测试。
- 2026-11-08：克隆并安装 Windows-MCP 0.8.8（独立 venv），完成 `tools/list`、`Snapshot`、observe-only 运行时只读验证。
- 2026-11-08：新增 `WindowsMcpAdapter` 与 `XshellMcpAdapter`，配置支持子进程 env/cwd，测试增至 44 个。
- 2026-11-08：克隆 xshell-mcp，确认 6 个工具与文件队列桥接实现；构建待授权。
- 2026-11-08：获授权后构建 xshell-mcp（`dist/server.js`），`tools/list` 通过；因无在线 bridge，会话列表为空。
- 2026-11-08：新增 `config/xshell-bridge-default.js`，用于在 Xshell 标签页内加载 bridge；真实会话验证待人工启动。
- 2026-11-08：`probe_xshell_mcp.py` 增加 `--exec`（关闭 `allowDangerous`），用于 bridge 就绪后跑 `pwd` 验证 `terminal.exec`。
- 2026-11-08：主线调整为 Windows-MCP + Computer-Use MCP；Xshell 因本机未安装降为框架保留。
- 2026-11-08：克隆 domdomegg/computer-use-mcp 1.8.0，新增 `ComputerUseMcpAdapter`、视觉消息通道（`model.vision`）、探测脚本与 13 个测试，测试总数 57。
- 2026-11-08：新增 SQLite 用户资料/任务历史、回环 HTTP 面板、pywebview 桌面窗口、三页前端与 `start.cmd`；`gui` 子命令与浏览器模式联调通过，测试总数 70。
- 2026-11-08：新增多厂商 AI 配置（OpenAI / Anthropic 协议、18 个厂商预设、GLM/OpenCode 套餐差异）、DPAPI 加密密钥入库、官方模型列表与可识图标注、连通性测试、活动模型装配与 Token 用量面板；前端新增「AI 模型」「用量」两页，测试总数 82。
- 2026-11-08：将模型选择并入提供商表单：填写 Key 后「获取模型列表」自动保存并拉取官方模型，「模型名称」下拉选中即切换活动模型，支持「使用其他模型」自定义 ID；切换厂商/套餐清空待选，端点变更时后端清除旧官方模型缓存；修复切换厂商后显示名称不更新与「保存并测试」不生效的问题；测试增至 83。
- 2026-11-08：模型列表改为按套餐自动选择接口风格并锁定（仅自定义端点可手选）；识别 HTTP 200 包裹的业务错误（如 GLM Coding Plan 的 `success:false`），不再误报「0 个模型」；GLM Coding Plan 自动回退到标准端点取模型；空结果不再清空已有模型，错误响应兼容 GBK 文本；浏览器验证正常/错误/空三种场景，测试增至 86。
- 2026-11-08：新增第三方 MCP 资源管理：固定版本清单（含提交 SHA）、`vendor/<名称>-<版本>` 版本化目录 + Windows junction、GitHub/ghproxy.net/gh-proxy.com/ghfast.top 归档回退、PyPI/清华/阿里/腾讯与 npm/npmmirror 构建回退、zip 越界校验、启动脚本自动初始化、`resources status/ensure` CLI、设置页资源检测悬浮窗与进度条；实测下载并构建 Computer-Use MCP 1.8.0，`tools/list` 返回 1 个工具、截图 1464×823 通过；测试增至 100；新增 README 标注上游仓库、版本与镜像源。
- 2026-11-08：修正用量口径：仅在模型响应通过形状校验后记录 `usage`，HTTP 200 包裹的业务错误不再产生 0 Token 调用；用量汇总、分模型/提供商与最近记录只统计 `total_tokens > 0` 的成功调用；用量页标注 Token 为计费单位；测试增至 93。
- 2026-11-08：修复所有模型 404 的根因：GLM Coding Plan 的 Anthropic Base URL 缺少 `/v1`，且旧记录把协议存成了 `openai`，导致请求发到 `/api/anthropic/chat/completions`。预设提供商的协议/Base URL/模型列表接口/凭据要求改由套餐在后端强制决定，历史错误记录读取时自动修复；界面仅对「自定义」显示这些字段。连通性测试改用短系统提示并兼容仅返回 `thinking` 块推理模型（GLM-4.5）。真实 Key 验证 `glm-4.5` / `glm-4.6` / `glm-5.3` 均通过，测试增至 91。
