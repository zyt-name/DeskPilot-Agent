# 实施进度

> 最后更新：2026-11-10
> 当前阶段：P0 桌面链路（Windows-MCP 主通道 + Computer-Use MCP 视觉通道已接框架）；本地桌面控制台可用；多厂商 AI 配置、DPAPI 加密密钥与 Token 用量面板已可用；第三方 MCP 资源支持启动自动初始化、版本化安装与国内镜像回退；Xshell 仅保留框架。任务式界面、流式思考/输出、会话与事件持久化、串行队列与插队、暂停/继续/结束控制、可复用确认弹窗、顶层 AI 审计与两种用户审计模式已落地（TECH_DESIGN 4.14 / 4.15）；视觉提速（批量计划、截屏压缩、屏幕摘要缓存）与回退风险识图审查已落地（TECH_DESIGN 4.16），顶层审计模型强制为可识图模型。登录阻塞与用户接管已落地（TECH_DESIGN 4.17）：无凭据时中止任务等待用户登录，用户回复「继续」后沿用原目标恢复执行。全局 F8、顶部状态胶囊与任务级边缘高亮已完成；运行中控制台自动最小化让出桌面、结束/暂停/等待确认时恢复（TECH_DESIGN 4.19）；右上 HUD 与 `WDA_EXCLUDEFROMCAPTURE` 双后端实测尚未完成。
>
> **浏览器功能（内置浏览器通道）已接入执行链路**：支持 Microsoft Edge、Google Chrome 与内置 Chromium 兜底三种引擎，各用独立 profile（不读取用户日常浏览器数据）；独立进程 + 画面镜像 + 扫码接管，与桌面通道并列而非其子模式，详见 TECH_DESIGN 4.21。驱动方式为**纯 Python 标准库直连 CDP**，不引入 Playwright / Selenium / 任何第三方包。**已完成**：CDP 地基、引擎/会话/观测/输入/镜像六模块、通道抽象与能力路由（`channels.py`）、5 个 `browser.*` 能力契约、审计规则、CLI 与面板接入、面板镜像（侧边抽屉 / 全屏视图 / SSE 帧流）。2026-11-10 真机实测：启动 722ms、观测 62ms、按序号点击 16ms、中文输入、点击后校验命中、优雅关闭后 profile 保留；Runtime 全链路 5 步 0.9s（TECH_DESIGN 4.22.6）；面板 SSE 握手 200、首帧即达、退订后停流（4.22.7）。**已完成（续）**：纯浏览器模式（不挂桌面通道、不截屏、不占屏）、空闲回收调度（后台巡检，任务中与有人看镜像时不回收）、扫码登录接管（识别登录墙 → 无头切可见 0.5s 或前置窗口 → 暂停等用户 → 回复继续复用登录态，持久 Cookie 实测保留）。内置 Chromium 已纳入资源管理（按需安装、双源、启动探针）。资源自动升级已落地（TECH_DESIGN 4.22.15）：旁路安装 → MCP 握手/内核 `--version` 探针 → 通过才切换入口，失败保留旧版并可 `rollback`；`update_check` 支持 `off/notify/auto`。**未完成**：浏览器 DPAPI cookie 快照、可选独立第二窗口。
> 设计文档：[TECH_DESIGN.md](TECH_DESIGN.md)

## 1. 已完成

| 模块 | 文件 | 状态 | 验证方式 |
|---|---|---|---|
| 能力契约 | `src/xm_agent/contracts.py` | 完成 | 13 个能力（8 桌面 + 5 浏览器）、版本号、JSON Schema、按通道裁剪的模型提示 |
| 数据类型 | `src/xm_agent/types.py` | 完成 | 单元测试 + 端到端 mock |
| 运行时状态机 | `src/xm_agent/runtime.py` | 完成 | observe → policy → act → verify → recover；模型响应/观察失败按预算重试并记录事件 |
| 登录阻塞与用户接管 | `src/xm_agent/runtime.py`、`src/xm_agent/ui/server.py`、`src/xm_agent/storage.py`、`src/xm_agent/ui/static/` | 完成 | `task.await_user` 能力；UIA 识别登录页且目标/指令无凭据时确定性中止；同会话回复「继续」沿用原目标恢复，队列透传 `user_message` / `context_note` |
| MCP stdio 客户端 | `src/xm_agent/mcp_stdio.py` | 完成 | 对 fake Server 与真实 Windows-MCP 完成握手与调用 |
| 通用能力适配层 | `src/xm_agent/adapters/mcp_tool.py` | 完成 | 工具名与参数重命名由配置驱动 |
| Windows-MCP 适配器 | `src/xm_agent/adapters/windows_mcp.py` | 完成 | 真实 `Snapshot` 文本解析、坐标点击、WaitFor 条件 |
| Computer-Use MCP 适配器 | `src/xm_agent/adapters/computer_use_mcp.py` | 完成 | 截图保存、坐标动作映射、`clear`/`press_enter`、视觉验证标记延迟；无视觉模型拒绝启动 |
| Xshell MCP 适配器 | `src/xm_agent/adapters/xshell_mcp.py` | 完成，框架保留 | 单测覆盖；本机未安装 Xshell，不验证真实会话 |
| Mock 执行器 | `src/xm_agent/adapters/mock.py` | 完成 | demo 与测试使用 |
| 策略引擎 | `src/xm_agent/policy.py` | 完成 | 默认拒绝、命令白名单、危险命令确认、路径根限制 |
| 租约锁 | `src/xm_agent/lock.py` | 完成 | 获取/释放/争用/过期接管/心跳 |
| 审计日志 | `src/xm_agent/audit.py` | 完成 | JSONL、敏感字段脱敏、按 trace_id 归档 |
| 顶层 AI 审计 | `src/xm_agent/guard/` | 完成 | 操作描述符归一化、确定性敏感识别、盲审（无工具/无任务上下文）、固定 JSON 判决、失败关闭、TOCTOU 复核、120s 缓存；回退风险 UI 操作强制识图审查，识图结果不缓存 |
| 视觉提速 | `src/xm_agent/vision.py`、`src/xm_agent/runtime.py` | 完成 | 1–4 步批量计划（中间步骤延迟验证）、动作预算、截屏缩到 1280 宽 + JPEG q80/PNG 择优、坐标还原、截图哈希 + `screen_card` 缓存；Pillow 可选 |
| 运行控制 | `src/xm_agent/control.py` | 完成 | 暂停/继续/取消/插队抢占/指令注入，步骤边界生效 |
| 流式输出 | `src/xm_agent/providers/` | 完成 | OpenAI SSE（`reasoning_content` / `content`）与 Anthropic `thinking_delta` / `text_delta`，usage 归一化 |
| 模型提供方 | `src/xm_agent/providers/` | 完成 | OpenAI 兼容 + Anthropic Messages + fake / scripted；支持视觉消息与 usage 回调 |
| AI 厂商目录 | `src/xm_agent/ai/catalog.py`、`src/xm_agent/model_caps.py` | 完成 | 18 个预设、OpenAI/Anthropic 协议、套餐差异（GLM Coding Plan、OpenCode Go 等）；图像能力由官方元数据与模型家族表自动判定 |
| 密钥加密 | `src/xm_agent/ai/crypto.py` | 完成 | Windows DPAPI + 固定 entropy，密文入库，界面只显示末尾提示 |
| 模型列表 | `src/xm_agent/ai/models.py` | 完成 | OpenAI/Anthropic/Gemini/Ollama 四种接口风格，按套餐自动选择；识别 200 业务错误、Coding Plan 回退标准端点；过滤非对话模型；优先读取官方模态元数据，缺失时按模型家族表判定视觉能力 |
| 预设端点隔离 | `src/xm_agent/ai/service.py`、`factory.py` | 完成 | 预设提供商的协议、Base URL、模型列表接口与凭据要求由套餐强制决定，忽略前端传值；历史错误记录在读取时自动修复 |
| AI 装配与用量 | `src/xm_agent/ai/factory.py`、`service.py` | 完成 | 活动模型装配、加密读写、连通性测试、usage 回调入库 |
| CLI | `src/xm_agent/cli.py` | 完成 | `demo` / `run` / `gui`、交互确认、UTF-8、四种适配器；`run` 自动读取本地库中的活动模型 |
| 构建装配 | `src/xm_agent/bootstrap.py` | 完成 | 优先使用活动 AI，未配置时回退配置/启发式；CLI 与 UI 共用 |
| SQLite 持久化 | `src/xm_agent/storage.py` | 完成 | 用户偏好、任务历史、AI 提供商/模型/用量、会话/消息、`run_events`、`task_queue`、`audit_decisions`；WAL |
| 本地面板后端 | `src/xm_agent/ui/server.py` | 完成 | 回环 HTTP、随机 token、静态资源、SSE 事件通道（断线重放）、会话 CRUD、串行队列与插队、确认代理、暂停/继续/结束/指令接口 |
| 任务式前端 | `src/xm_agent/ui/static/task.js`、`index.html`、`styles.css` | 完成 | 三段布局、流式思考/输出、思考默认折叠、审计卡片、确认弹窗、会话列表、队列列表、F8（窗口内）暂停与指令注入 |
| 桌面窗口 | `src/xm_agent/ui/window.py`、`start.cmd` | 完成 | pywebview 6.2.1 + WebView2，cmd 一键启动；启动前自动执行资源初始化 |
| 桌面接管 | `src/xm_agent/ui/desktop.py`、`ui/server.py` | 完成 | 需屏幕的任务开始时最小化控制台，结束/失败/取消后恢复；暂停与用户确认时临时恢复，继续后再次让出；`[runtime].release_desktop` 可关；浏览器模式与无窗口时静默跳过 |
| 资源管理 | `src/xm_agent/resources/`、CLI `resources`、设置页「MCP 资源」 | 完成 | 固定版本清单、版本化目录 + junction、GitHub/国内镜像回退、安全解压、venv/npm 构建、检测悬浮窗 |
| 面板其余页面 | `src/xm_agent/ui/static/app.js` | 完成 | 总览 / AI 模型 / 用量 / 历史 / 设置；饼图、分模型统计与资源检测悬浮窗 |
| 用量来源统计 | `src/xm_agent/storage.py`、`src/xm_agent/ui/static/` | 完成 | `ai_usage` 按 `source` 聚合；用量页「审计 Token」卡片显示审计调用占比与次数 |
| 截图提问 | `src/xm_agent/ui/server.py`、`src/xm_agent/ui/static/` | 完成 | 仅暂停中可问；截屏前临时最小化控制台以获得干净画面；纯文本模型前端禁用、后端拒绝；答案显示在暂停横幅内，不落库、不进入任务上下文 |
| 数据保留清理 | `src/xm_agent/maintenance.py`、`src/xm_agent/cli.py` | 完成 | `cleanup` CLI（`--dry-run` / `--keep-days`）；只清理审计 JSONL 与截图图片，不递归删目录；`run` / `gui` 启动时自动执行 |
| 契约测试 | `tests/test_contract.py` | 完成 | 同一组用例参数化跑 mock / windows / computer_use / xshell 四个后端（observe 结构、终止能力失败关闭、verify 结构、close 幂等） |
| 配置 | `config/agent.example.toml`、`config/xshell-mcp.json` | 完成 | TOML 适配器映射、子进程 env/cwd、`model` 回退配置、`[runtime].screenshot_dir` |
| 只读探测脚本 | `scripts/probe_windows_mcp.py`、`scripts/probe_computer_use_mcp.py`、`scripts/probe_xshell_mcp.py` | 完成 | Windows 端到端只读；Computer-Use 已构建；Xshell 框架保留 |
| 测试 | `tests/` | 完成 | 478 个用例全部通过（含 22 项浏览器真机集成） |
| 测试用 MCP Server | `examples/fake_mcp_server.py` | 完成 | 无第三方依赖，stdio JSON-RPC |
| 浏览器通道核心（引擎 / 会话 / 观测 / 输入 / 镜像） | `src/xm_agent/browser/`（`cdp.py`、`engine.py`、`session.py`、`snapshot.py`、`input.py`、`mirror.py`） | 完成 | 纯标准库 CDP：启动、导航、截图、可交互元素清单（带坐标）、中文 `insertText`、坐标点击、镜像帧流、优雅关闭保留登录态；32 个用例（18 逻辑 + 14 真机） |
| 通道抽象与能力路由 | `src/xm_agent/channels.py` | 完成 | `Channel` 四方法契约、`ChannelHub` 按能力名分发、观测跟随上一步通道、不可用通道回落、能力清单按通道裁剪；16 个用例 |
| 浏览器通道适配 | `src/xm_agent/browser/channel.py` | 完成 | `browser.open` / `observe` / `act` / `verify` / `session` 五个能力；元素序号 `b0..bn`、登录墙识别、截图落盘、窗口前置、空闲回收；22 个用例 + 真机探针 |
| 浏览器审计规则 | `src/xm_agent/guard/operation.py`、`auditor.py` | 完成 | 只读能力免审、点击提交类文本复用桌面 effect 规则、非 http 协议标记外部写、高风险点击强制识图复核 |
| 浏览器通道真机探针 | `scripts/probe_browser_channel.py` | 完成 | 本地静态页跑完 open → observe → click → type → verify → close，13 项断言全通过 |
| 面板浏览器镜像 | `src/xm_agent/ui/server.py`、`ui/static/browser.js`、`styles.css` | 完成 | `GET /api/browser` 状态、`POST /api/browser` 动作、`GET /api/browser/stream` SSE 帧流；侧边抽屉与全屏（左对话右浏览器）两形态；无人订阅即停流 |
| 浏览器指纹与导航 | `browser/engine.py`、`browser/stealth.py`、`browser/session.py`、`browser/channel.py`、`browser/input.py` | 完成 | 无头启动按内核版本重组成标准 UA + `--disable-blink-features=AutomationControlled`，每个文档注入脚本把 `navigator.webdriver` 抹成 undefined（`new_page` 先开空白页再注入再导航，重连后重新注入）；`reload`/`back`/`forward` 改走 `Page.reload` 与 `Page.getNavigationHistory`（`history.reload` 不存在）；导航中 CDP 瞬时错误重试、`Not attached to an active page` 重挂；导航后等首屏内容（最多 6s）；拦截页在观测里直接标注；`[runtime.browser] user_agent / hide_automation`；10 个用例（5 单元 + 5 真机） |
| 浏览器断线自愈 | `browser/cdp.py`、`session.py`、`channel.py` | 完成 | 空闲读超时不再判死连接（`_read_exact` 内继续等，半帧保留）；`live_connection()` 原地重连并按 target 重挂标签页、重启镜像；`Page.call` 重试一次、`Page.conn` 返回活连接；内核被杀则自动重启并换新页面；`status()` 不向上抛；8 个用例（4 单元 + 3 真机 + 1 面板） |
| 纯浏览器模式 | `bootstrap.py`、`channels.py`、`runtime.py`、`ui/server.py` | 完成 | `adapter=browser` 不挂桌面通道：能力只剩 `browser.*`、不显示覆盖层、不让出桌面、审计不截桌面；首帧唤醒唯一通道；7 个用例 |
| 空闲回收调度 | `ui/server.py`、`browser/channel.py` | 完成 | 后台巡检按 `keep_alive_seconds` 收敛；任务中与镜像订阅时不回收；负数=永不回收；5 个用例 |
| 扫码登录接管 | `browser/channel.py`、`browser/session.py`、`runtime.py` | 完成 | `handoff()` 无头切可见（0.5s）或前置窗口；登录墙触发 `needs_human` 暂停；7 个用例 |
| 资源更新检测 | `resources/updates.py`、`ui/server.py`、`ui/static/app.js` | 完成 | Releases→Tags 兜底、版本比较、跨大版本门禁、双层缓存、`off/notify`、面板「新版」标记；10 个用例 |
| 内置内核资源管理 | `resources/manifest.py`、`resources/manager.py`、`ui/server.py`、`start.cmd` | 完成 | `kind=browser` 安装分支（不建链接不构建）、双源下载、默认不自动安装、`--version` 启动探针、面板探针按 kind 分流；资源用例 10 项 |
| 资源自动升级 | `resources/upgrade.py`、`resources/manager.py`、`ui/server.py`、`cli.py`、`ui/static/app.js` | 完成 | 旁路安装 → 探针（MCP `tools/list` / 内核 `--version`）→ 通过才切换入口；失败删旁路保留旧版；`active.json` 记录在用版本、`attempts.json` 失败冷却 6h；`resources upgrade|rollback`、面板「升级到 x」与「回滚」按钮；内核按启用版本解析；20 个用例 |
| 浏览器地基验证脚本 | `scripts/probe_browser_cdp.py` | 已完成使命 | 模块落地前的实测依据，保留作排障工具 |

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

结果：`433 passed`

覆盖范围：

- 能力契约完整性
- 批量计划：一次决策执行多步、中间步骤延迟验证、失败丢弃剩余批次、终止动作截断、动作预算按动作计数
- 截屏：哈希确定性、无 Pillow 回退、缩放与坐标还原、保存产物与哈希；屏幕未变化复用 `screen_card` 且不重复上送图片
- 回退风险识图审查：提交/删除 UI 动作附带截图、无识图模型强制确认、无截图不猜测、终端删除不强制截图、显式 `rollback_risk` 标记；审计模型选择拒绝纯文本模型
- 租约锁获取、释放、争用、过期接管、心跳
- 命令白名单、危险命令确认、文件路径限制
- MCP stdio 握手、工具列表、工具调用、错误工具
- Windows-MCP Snapshot 文本解析（中文控件类型、JSON 数组包裹、空窗口）
- Computer-Use 截图内容解析与落盘、坐标动作映射、滚轮方向/步长、验证降级
- 截图仅在活动模型被判定支持图像输入且观测包含截图时附加
- 元素 ID → 坐标解析、显式坐标、未知元素报错
- Type / Shortcut / Scroll 参数映射、WaitFor 条件构造
- Xshell `xshell_exec` 会话与毫秒超时换算、屏幕读取、SFTP 能力缺口、等待超时
- 运行时正常完成、策略拒绝、确认拒绝、连续失败、锁占用
- 配置加载与示例配置
- SQLite 用户资料读写、任务历史排序与截断
- 面板 API：token 校验、Host 校验、静态资源、任务启动/轮询/落库
- 资源清单版本固定、版本化目录/legacy 检测、过期版本识别、zip 越界拒绝、镜像回退下载与构建链接、面板资源状态/同步 API
- AI 厂商目录、GLM/OpenCode 套餐差异、图像能力自动识别（官方模态元数据、Ollama families/capabilities、模型家族表、未知模型保守判文本、历史手动标记不采信）
- DPAPI 密钥加密往返、更新时保留旧 Key、清除 Key
- OpenAI 兼容与 Anthropic Messages 的模型拉取、连通性测试、usage 归一化
- 面板 AI API：密钥不回显、模型添加/激活、Token 用量聚合
- 活动模型优先装配、未配置时回退 mock、Ollama 无密钥模式
- 顶层审计：只读动作免审、策略拒绝不送审、硬拦截路径、AI allow/deny/ask_user、用户拒绝、审计模型缺失时升级确认、TOCTOU 变更阻止、缓存、声明式敏感并入、审计器盲性（看不到任务目标与理由）、跨项目/用户数据读取强制用户确认、AI `allow` 不得绕过用户决定、自动确认不得跳过强制确认
- 视觉提速：批量计划一次决策、中间步骤延迟验证、失败丢弃剩余批次、终止动作截断批次；截图不变复用 `screen_card` 且不上送图片；截屏压缩与坐标缩放、无 Pillow 回退
- 回退风险审查：提交/删除/覆盖 UI 动作携带截图送审、无识图模型或无截图时强制用户确认、终端删除等文本审计不强制截图、显式 `rollback_risk` 标记生效；审计模型选择拒绝纯文本模型
- 运行控制：暂停阻塞至继续、取消抛错、插队立即抛错并唤醒暂停线程、指令只消费一次
- 会话与事件持久化：消息生命周期、删除会话后任务保留、事件顺序与回放、队列排序/插队/状态/移除、审计决策记录
- 面板：会话 CRUD、忙时排队并端到端执行、SSE 回放与鉴权、暂停/指令/继续/取消、确认代理、插队时拒绝待确认并抢占直启任务
- 登录阻塞：模型主动 `task.await_user` 中止并返回待办提示；UIA 识别登录页且目标/指令未提登录或注册、也未提供凭据时确定性拦截；目标本身包含登录/注册/密码或已给出凭据时不拦截
- 用户接管恢复：同会话回复「继续」复用被阻塞任务的原目标并注入上下文说明；队列中的恢复任务透传用户消息与上下文；无待处理交接时显式 resume 被拒绝；等待状态在会话切换后恢复显示
- 通道路由：能力名前缀判定、精确声明优先、观测跟随上一步通道、显式指定通道观测、不可用通道回落桌面、缺失通道给出可读错误、能力清单按通道聚合
- 浏览器通道：Observation 形状与桌面对齐、元素序号 `b0..bn` 与坐标还原、截图落盘、登录墙识别（密码框 / URL / 提示文案）与普通页面不误判、点击三种定位、中文输入与回车、选择器未命中报错、等待超时、URL/标题/正文/元素校验、会话 status/handoff/close、空闲回收保留与强制关闭
- 跨通道端到端：同一任务内先桌面后浏览器，观测源切换正确、浏览器动作后不再截桌面、关闭会话后隐式观测回落桌面而不重启内核
- 契约：13 项能力 schema 完整、`capabilities_prompt` 按通道过滤
- 面板浏览器接口：状态查询（启用/未启用）、动作鉴权与未知动作拒绝、帧订阅与退订、队列上限下只留最新帧、伪通道下动作委派

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
- [x] 确认交互从 CLI 文本升级为可复用的确认接口（确认代理 + 面板弹窗，批准仅对当前步骤生效）
- [x] 契约测试框架：同一组用例跑所有后端（`tests/test_contract.py`，4 个后端参数化）
- [x] 任务式界面：三段布局（导航 / 对话 / 任务列表），历史并入任务页
- [x] 流式思考与输出：Provider SSE、面板 SSE 事件通道、思考默认折叠、动作卡片
- [x] 会话与事件持久化：`conversations` / `messages` / `run_events` / `task_queue`，支持断线重放
- [x] 前台独占：单任务串行 + 运行中控制台最小化让出桌面，结束/失败/取消后恢复窗口；暂停与确认弹窗临时恢复，继续后再次让出（`ui/desktop.py`，`[runtime].release_desktop`）
- [x] F8（控制台窗口内）暂停、继续、结束控制与指令注入
- [x] F8 全局暂停、顶部状态胶囊与屏幕边缘高亮（`RegisterHotKey` + `WDA_EXCLUDEFROMCAPTURE`，注册失败回退窗口内 F8）
- [ ] 右上角 HUD：实时思考/步骤、悬停滚轮、缩放、折叠与位置记忆
- [ ] `WDA_EXCLUDEFROMCAPTURE` 截图排除：Windows-MCP 与 Computer-Use 分别实测，准备回退方案
- [x] 暂停菜单：继续 / AI 注入 / 截图提问 / 结束控制
- [x] 截图提问：暂停后截全屏交视觉模型问答（`POST /api/runs/{id}/ask_screen`）；纯文本模型在界面禁用、服务端拒绝；截图前临时收起控制台，拍完恢复
- [x] 串行队列、插队抢占与恢复；全程禁止并行任务
- [x] 顶层 AI 审计：操作描述符、盲审、失败关闭、执行前复核
- [x] 用户审计模式（AI 审计 / 每次确认）与确认弹窗
- [x] 登录阻塞与用户接管：`task.await_user` 中止、UIA 登录页确定性兜底、同会话「继续」沿用原目标恢复
- [ ] 验证码 / 二次验证 / 纯图形登录页的识别与接管（当前仅文本启发式，未命中时依赖模型主动调用）

### P1

- [x] 破坏性 UI 操作识别（删除/卸载/格式化类元素名与危险快捷键）
- [ ] 审计决策面板与统计：模型、耗时、风险、用户决定回放
- [x] 审计 Token 用量单独统计（`ai_usage.source` 聚合，用量页「审计 Token」卡片）
- [ ] 框选截图提问（多显示器负坐标与 DPI 缩放）
- [ ] 运行中 HUD 悬停滚轮/缩放（不抢焦点、不截获点击）
- [ ] 用户非注入输入自动暂停（宿主低级钩子 + Windows-MCP `USER_CONTROL` 合并判定）
- [ ] 窗口归属跟踪（新建/激活窗口差集）与结束操控现场策略
- [ ] Xshell 真实会话验证（仅在装有 Xshell 的机器上进行，框架已就绪）
- [ ] SFTP 文件传输后端（xshell-mcp 不含 SFTP 工具）
- [ ] SSH 作为第二终端后端，验证替换 Xshell 不改 Agent
- [ ] UIA 失败时切换到 Computer-Use 视觉兜底，动作后强制截图
- [ ] 失败恢复策略：重新观测、切换通道、回退、人工接管
- [ ] 登录页视觉兜底识别：纯图形 / 验证码 / 二次验证页面用识图模型判定并请求用户接管
- [ ] 凭据安全输入：避免密码以明文出现在任务目标、会话记录与审计日志中，接入系统凭据存储
- [x] 截图与审计的保留策略、清理任务（`maintenance.cleanup` + `cleanup` CLI；压缩与缩放见 `vision.py`）

#### 浏览器功能（内置浏览器通道 · 新模块，全部未开始）

> 与桌面通道并列的独立执行通道，详见 TECH_DESIGN 4.21。不占用、不截取、不点击用户桌面。

- [x] 【浏览器功能】P0 地基验证：无头内核启动 156ms、截图 73ms、坐标点击 8ms 精确命中、镜像约 12fps、中文输入、`Browser.close` 后登录态仍在（TECH_DESIGN 4.21.14，纯标准库，无第三方依赖）
- [x] 【浏览器功能】`browser/` 子包落地：标准库 CDP 客户端（后台读线程 / 写锁 / 事件订阅 / 死锁规避）、引擎解析、会话与标签、快照、输入、镜像帧
- [x] 【浏览器功能】引擎解析：Edge / Chrome / 内置 Chromium 三级回落，指定引擎缺失时明确报错不静默降级；版本探测走 `--product-version`
- [x] 【浏览器功能】独立 profile：按引擎隔离目录 + 进程内注册表与跨进程 pid 锁文件（含残留锁清理）
- [x] 【浏览器功能】零依赖分发：`pyproject.toml` 未新增任何依赖，WebSocket/解压/HTTP 全走标准库
- [x] 【浏览器功能】空闲回收调度：面板后台线程按 `keep_alive/3`（5–60s）巡检；任务运行中、有人看镜像时不回收；负数表示永不回收
- [x] 【浏览器功能】`browser.*` 能力契约与适配器（open / observe / act / verify / session）
- [x] 【浏览器功能】通道抽象与路由：`channels.py` 按能力名分发，浏览器动作后观测网页、关闭后回落桌面
- [x] 【浏览器功能】镜像表层：侧边抽屉、全屏视图（左侧对话列）、SSE 帧流；可选独立第二窗口未做
- [x] 【浏览器功能】扫码登录与接管：`raw.login_wall` 判定 → `handoff()`（无头切可见 / 已可见前置）→ `task.await_user` 暂停 → 用户「继续」后复用登录态；目标本身是登录或已给密码时不误停
- [ ] 【浏览器功能】登录态兜底：`Browser.close` 优雅退出与 `DevToolsActivePort` 残留清理已完成；DPAPI cookie 快照未做
- [x] 【浏览器功能】内置 Chromium 纳入资源管理：`kind="browser"`、Chrome for Testing 官方与 npmmirror 双源、按需安装（默认不拉 210MB）、`chrome.exe --version` 启动探针、`start.cmd` 内核检查
- [x] 【浏览器功能】资源更新检测：GitHub Releases/Tags（含 tags 兜底）、版本比较、跨大版本门禁、仓库 6h + 面板 10min 双层缓存、`off/notify/auto` 配置、面板「新版」标记
- [x] 【浏览器功能】资源自动升级：旁路安装（`<dir>-<version>`，不动稳定入口）+ 探针通过才切换 + 失败删旁路保留旧版 + `attempts.json` 冷却 + `resources rollback` + `auto` 档；20 个用例 + 真机失败路径
- [ ] 【浏览器功能】风控兜底：可见模式、人工接管、验证码转人工，明确告知失败原因

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
10. **模型列表差异**：网关型服务（OpenCode、方舟接入点、部分 Coding Plan）可能不提供标准 `/models`；界面已允许自定义模型 ID，图像能力由后端根据官方元数据与模型名称自动判定，无需用户选择。
11. **Computer-Use MCP 已构建，但首次初始化依赖网络与 Node.js 20+**：资源管理器会下载固定版本并执行 `npm install && npm run build`；失败时设置页会显示错误，可重试，旧目录不会被直接删除。
12. **Computer-Use 依赖 nut.js 原生模块**：Windows 下需要可用的预编译二进制，安装失败时需要回退到 UIA 主通道。
13. **Computer-Use 没有元素树**：只能靠截图坐标；单步 `desktop.verify` 会标记为“待视觉核对”。无视觉模型时不再允许启动（会盲操作并虚假完成）；后端会依据官方模型元数据与模型名称自动识别图像能力，识别为纯文本时在「AI 模型」中换用多模态模型，或改用 Windows-MCP；有视觉时 `task.finish` 前会用当步截图向模型复核，判否即拒绝完成并要求继续操作。
14. **锁是文件锁**：过期接管存在很小的竞争窗口；没有跨进程单调 fencing 计数；目前只支持单机。
15. **清理依赖启动或手动触发**：`cleanup` 已接入 `run` / `gui` 启动与 CLI，但没有常驻定时任务，长时间运行的服务仍需手动执行；截图目录只识别常见图片扩展名，其他文件不动。
16. **策略不是沙箱**：命令白名单是前缀匹配，无法阻止白名单命令被滥用；桌面操作仍依赖人工确认与审计。
17. **MCP 客户端是自研最小实现**：已覆盖当前所需方法，但生产环境应迁移到官方 MCP SDK。
18. **尚无桌面安装包**：已有 pywebview 本地面板与 `start.cmd`，但没有安装器、托盘、更新器；后期可按 Tauri + sidecar 迁移，UI 只依赖本地 HTTP API。
19. **vendor 目录体积大**：Windows-MCP 的 venv 约数百 MB，Computer-Use 还带 Node 依赖，分发时必须换成精简依赖或预构建运行时。
20. **流式增量不落库**：`thinking` / `assistant` 增量事件只走 SSE，`run_events` 持久化其余事件；切换会话或重启后只能看到最终消息里截断的思考（前 8000 字），不能逐 token 回放。
21. **确认等待有时限**：单次用户确认最长等待 300 秒，超时按拒绝处理；确认只对当前操作哈希有效，操作变化会重新确认。
22. **真正的多桌面隔离不可行**：Windows 虚拟桌面共享同一交互会话，只有当前桌面渲染并接收输入；隐藏 `CreateDesktop` 桌面不渲染；RDP 单客户端只允许一个交互会话。因此采用前台独占 + 单任务串行，真隔离只能上 Windows Sandbox / Hyper-V，且无法与宿主应用共享窗口。
23. **`WDA_EXCLUDEFROMCAPTURE` 尚未实测**：顶部提示、发光层与 HUD 计划用该 API 排除出截图，但 Windows-MCP 与 Computer-Use（nut.js）的截图后端是否遵守尚未验证；失效时需回退为截图前隐藏或裁剪，可能有闪烁。
24. **F8 有环境限制**：低级键盘钩子可能收不到 UAC/提权窗口的按键，也可能与目标应用快捷键冲突；需要可配置快捷键与 HUD/托盘暂停入口，彻底覆盖提权窗口需后期增加辅助进程。
25. **截图提问依赖视觉模型**：执行模型 `vision=false` 时按钮禁用、接口直接拒绝，需要切换可识图模型；本版本不做本地 OCR。答案只显示在暂停面板，不写入任务上下文，不会影响后续执行。
26. **暂停与插队存在延迟**：暂停只在步骤边界生效，正在等待的模型请求需要可取消才能快速响应；单个 MCP 动作不会被强行中断。
27. **前台独占不等于互不影响**：用户中途移动鼠标、点击或输入会改变 AI 依赖的界面状态。v1 依赖 F8 手动暂停与 Windows-MCP `USER_CONTROL` 保护；Computer-Use 没有该保护，非注入输入自动暂停列入 P1。
28. **HUD 交互与点击穿透冲突**：运行中 HUD 必须点击穿透，否则会挡住 AI 的点击坐标；因此运行中只能悬停滚轮查看，点击/拖动 HUD 视为用户接管并暂停，暂停后才可缩放与选择文本。
29. **被插队任务留下窗口**：插队任务可能修改或关闭前一个任务的窗口；恢复前必须重新观测，目标不存在时按失败处理，不重放旧动作。
30. **中文输入法与注入框**：空格用于选词，回车用于换行；发送必须用按钮或 `Ctrl+Enter`，暂停面板打开时 F8 不触发恢复。
31. **顶层审计模型未配置时敏感操作全部升级**：没有可用的审计模型时，AI 审计模式对敏感操作返回 `ask_user`（失败关闭），不会静默放行；用户需要配置模型或接受更频繁的确认。
32. **审计器与被审计模型可能是同一个**：`[audit_ai]` 未指定独立模型时复用活动模型，隔离仅靠系统提示词与无工具调用，不能保证真正独立判断；建议生产环境指定不同厂商/模型。
33. **审计只看单步操作**：每个操作独立评估，不感知跨步骤组合风险（例如分多步删除多个目录），也不理解任务目标；组合风险仍需用户确认兜底。
34. **只读操作默认免审**：`desktop.observe` / `desktop.verify` / `wait.for` / `task.finish` 与滚动/移动不进入审计；执行模型可通过 `sensitive` 声明强制送审，但该声明依赖模型自觉，规则侧仍会补充检测。
35. **审计缓存 120 秒**：仅缓存 AI 审计的 `allow` / `deny`，键包含操作哈希、策略结果、模式与关键环境；环境变化未被完全覆盖时可能复用旧判决。
36. **插队会中止当前任务**：插队抢占通过 `RunPreempted` 结束当前运行，不会挂起并在插队任务结束后恢复；被中止任务的窗口状态可能改变，后续需重新观测。
37. **F8 依赖 `RegisterHotKey`**：全局热键、顶部胶囊与边缘高亮已实现，但 UAC / 提权窗口收不到热键（`RegisterHotKey` 不跨越完整性级别）；注册失败时回退为窗口内 F8。右上 HUD 尚未实现。
38. **删除会话保留任务记录**：删除会话会把其 `runs.conversation_id` 置空，任务历史与审计仍保留；这是有意的审计留痕，不是残余数据。
39. **审计模型必须是识图模型**：回退风险 UI 操作需要对照截图审查，纯文本模型无法保证判断质量，界面与 API 会拒绝选择纯文本模型；未配置任何可识图模型时，这类操作一律强制用户确认。
40. **识图审查有额外开销与隐私面**：回退风险 UI 动作每次都会截屏并调用一次视觉模型（不走 120 秒缓存），比文本审计更慢、更贵；截图可能包含屏幕上的隐私内容，本地 Ollama 可规避出网但需要额外算力。
41. **登录页识别是启发式**：确定性拦截依赖 UIA 文本/活动窗口中的登录关键词与密码/账号字段组合，非标准登录页（纯图形、验证码墙、二次验证）可能识别不到，此时仍依赖模型自觉调用 `task.await_user`；凭据仅从目标与指令文本判断，不会主动读取密码管理器。
42. **控制台最小化期间看不到实时步骤**：桌面任务运行时窗口被最小化，SSE 与事件仍在后台累积，恢复后完整回放；暂停（F8）与确认弹窗会自动把窗口调回前台。浏览器模式（`--server-only`）没有窗口可让，`release_desktop` 不生效。

## 5. 下一步

1. 原生覆盖层收尾：右上角 HUD（思考/步骤、悬停滚动、折叠）；在 Windows-MCP 与 Computer-Use 两个截图后端分别实测 `WDA_EXCLUDEFROMCAPTURE`，准备失效回退。全局 F8、顶部状态胶囊与边缘高亮已完成。
2. 截图提问增强：框选区域、多显示器负坐标与 DPI 归一化（当前为全屏）；支持把答案直接转成下一条指令。
3. 用视觉模型跑通 Computer-Use 的“截图 → 决策 → 坐标动作 → 再截图”闭环。
4. 前台独占增强：运行中让出桌面与结束/暂停恢复已完成；剩余窗口归属跟踪与结束操控现场策略。
5. 继续以 Windows-MCP 为主通道，先跑 3 个固定任务，再扩到 20 个基线；UIA 失败时再切视觉通道。
6. 补截图与坐标归一化（DPI、多显示器），为覆盖层定位与通道切换做准备。
7. 为顶层审计指定独立模型，补充审计决策面板与耗时回放（Token 已按来源单独统计）；用真实模型验证 `allow/ask_user/deny` 判决质量。
8. 登录接管增强：验证码 / 二次验证 / 纯图形登录页的视觉识别；凭据安全输入与系统凭据存储，避免密码进入会话与审计日志。
9. Xshell 等有安装环境的机器再做真实会话验证；当前不阻塞主线。
10. 再决定是否引入网关与多客户端。
11. 用真实厂商 Key 验证模型列表、限流与错误提示；对不提供 `/models` 的网关完善手动模型流程。
12. 如需费用面板，再增加各厂商/套餐价格表，把 Token 换算成金额。

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

# 清理过期审计日志与截图（启动 run/gui 时也会自动执行一次）
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent cleanup --dry-run
PYTHONPATH=src .venv/Scripts/python.exe -m xm_agent cleanup --keep-days 14

# 桌面控制台内的资源流程：
# 「设置」→「MCP 资源」→「检测并修复资源」
# 悬浮窗显示检测进度；缺失项自动从 GitHub 或已验证镜像下载，按版本安装到 vendor/<名称>-<版本>

# 桌面控制台内的 AI 配置流程：
# 1) 「AI 模型」→ 选择厂商与套餐 → 填入 API Key（协议/URL/模型列表接口自动配置）
# 2) 点击「获取模型列表」拉取官方模型，在「模型名称」下拉中选择
# 3) 需要非列表内模型时选「使用其他模型」，只填模型 ID，图像能力自动判定
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

- 2026-11-08：确认下一阶段交互方案并写入 TECH_DESIGN 4.14：任务式界面 + 流式思考/输出、前台独占 + F8 暂停、顶部提示/边缘发光/右上 HUD（`WDA_EXCLUDEFROMCAPTURE` 排除截图）、暂停菜单 AI 注入、全屏截图提问、串行队列与插队；明确不做多桌面隔离、多任务并行与 Windows 多鼠标，框选截图与用户输入自动暂停列入 P1；同步更新任务级状态机、非目标、P0/P1 待办与已知问题。
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
- 2026-11-08：修复任务执行链路：Runtime 现在自行处理模型请求的 `desktop.observe`（不再转发给只实现 act/verify 的后端）；Windows 适配器在 `USER_CONTROL` 时按 `wait_seconds` 在 30 秒内等待重试；`desktop.act` 契约补充 `button` / `clear` / `press_enter`，避免 Run 输入框历史文本被追加；Windows 验证从整句期望中提取引号文本或 URL；Anthropic 决策输出上限由 2048 提高到 8192，默认模型超时改为 120 秒；提示词明确用 Win+R 打开网页、终端后端不执行桌面目标。实测 `windows` 与 `computer_use` 均以“打开github”完成，`xshell` 在桥接不可用时给出明确结束说明；测试增至 103。
- 2026-11-08：修复所有模型 404 的根因：GLM Coding Plan 的 Anthropic Base URL 缺少 `/v1`，且旧记录把协议存成了 `openai`，导致请求发到 `/api/anthropic/chat/completions`。预设提供商的协议/Base URL/模型列表接口/凭据要求改由套餐在后端强制决定，历史错误记录读取时自动修复；界面仅对「自定义」显示这些字段。连通性测试改用短系统提示并兼容仅返回 `thinking` 块推理模型（GLM-4.5）。真实 Key 验证 `glm-4.5` / `glm-4.6` / `glm-5.3` 均通过，测试增至 91。
- 2026-11-08：落地 TECH_DESIGN 4.14 / 4.15。新增顶层审计 `guard/`（操作描述符、确定性命中、盲审、固定判决、失败关闭、TOCTOU、120s 缓存）、运行控制 `control.py`（暂停/继续/取消/插队/指令注入）、Provider 流式（OpenAI `reasoning_content` / Anthropic thinking）、SQLite 会话/消息/事件/队列/审计决策表、面板 SSE（Last-Event-ID 重放）、会话 CRUD、串行队列与插队、确认代理与运行控制接口；前端「运行任务」改为「任务」三段布局（对话流 + 任务列表 + 队列），思考默认折叠，审计卡片、确认弹窗、F8 窗口内暂停与指令注入。浏览器实测：mock 任务流式完成、思考折叠、`confirm_all` 弹窗、批准后审计卡片、F8 暂停/继续、指令注入、忙时排队、插队中止当前任务并启动队列、会话切换与历史回放；会话详情与运行详情补充暂停状态，切换回暂停中的任务会恢复暂停横幅；测试增至 149。
- 2026-11-08：修复桌面控制体验：新增仓库级 `config/windows-mcp.toml` 并通过 `--config` 传入，关闭 Windows-MCP 自带的同意弹窗与所有权输入钩子（其低层钩子会吞 F8），任务执行期间不再逐次询问；新增任务级屏幕占用边缘高亮（点击穿透、呼吸动画、`WDA_EXCLUDEFROMCAPTURE`，Windows-MCP/Computer-Use 任务显示，任务结束/失败/取消后隐藏），不再随单次工具调用闪烁；新增全局 F8（`RegisterHotKey` 独立消息循环，窗口失焦仍可暂停/继续），注册失败自动回退窗口内 F8；新增 `/api/bootstrap` 热键能力标记避免重复触发；新增 5 个回归测试，测试增至 159，Windows-MCP 实测 `ControlStatus` 为 `disabled`。
- 2026-11-08：把“涉及其他项目/用户数据由用户决定”固化进顶层审计：操作描述符新增 `user_data` 目标识别（文档/下载/桌面/图片/视频/音乐/OneDrive 等，且不在当前项目/工作区内），跨项目访问现在包含只读；`Sensitivity.requires_user` 由跨项目、用户数据、受保护路径、凭据访问确定性触发；审计提示词与 JSON scope 增加 `user_data` / `requires_user`，审计模型返回 `allow` 但 scope 或类别涉及跨项目/用户数据时自动升级 `ask_user`；Gate 再次兜底，AI `allow` 不能绕过用户确认；确认回调新增 `mandatory` 参数，任务界面“自动确认”与 CLI `--yes` 只能跳过普通确认，不能批准跨项目/用户数据的强制用户决定。新增 5 个回归测试，测试增至 154。
- 2026-11-08：修复 Computer-Use 虚假完成。根因：`glm-5.3-flash` 的 `vision=false`，截图从未发给模型；`desktop.act` 的 `type` 忽略 `clear`/`press_enter`，URL 写入 Run 对话框后从未回车；`wait.for`/`desktop.verify` 返回延迟成功，`task.finish` 又只看模型声明。现在：computer_use 无视觉模型直接拒绝启动并提示切换模型或 Windows-MCP；`type` 支持 `ctrl+a` 清空与回车提交；`Verification` 新增 `deferred` 标记并在界面显示“待视觉核对”；`task.finish` 前用当步截图调用视觉模型复核，判否即阻止完成并写入历史，连续两次判否按失败结束；提示词按 UIA/视觉后端分流，禁止盲操作与虚假完成，并告知模型是否真的附带了截图。新增 9 个回归测试，测试总数 168。
- 2026-11-08：图像输入能力改为后端自动判定，不再由用户勾选。新增 `src/xm_agent/model_caps.py`：优先读取官方模型列表的模态/能力元数据（OpenAI 兼容 `architecture.input_modalities` / `supported_input_modalities` / `modality` / `supports_image_in` / `capabilities`、Anthropic `capabilities.image_input.supported`、Ollama `capabilities` 与 `details.families`），缺失时按维护的官方模型家族表从模型 ID 推导，未知模型保守判为纯文本。`ai_models` 新增 `vision_source` 记录来源，读取时不再信任历史界面写入的 `vision`；新增/获取模型接口移除 `vision` 参数，界面删除「支持图像输入」勾选；`computer_use` 的拒绝提示改为说明当前模型被自动识别为纯文本。同步更新配置示例与文档，新增 5 个能力判定回归测试，测试总数 173。
- 2026-11-09：修正「可识图」判定：GLM-5.3-Flash 全系（`glm-5.3-flash` / `glm-5.3-flashx` / 面向法律场景的 FlashX-Legal）与 `deepseek-v4.1-flash`（含 `-1017` 等版本后缀）均为原生多模态，此前被误判为纯文本，导致模型列表不带「可识图」标注、Computer-Use 直接拒绝启动；`model_caps` 命名规则补上这两族，已缓存模型在读取时按新规则重算并标记 `vision_source=model`，无需重新拉取，重新「获取模型列表」同样按新规则标注；同步修正受影响的测试断言，测试总数 173。
- 2026-11-09：桌面提示增强：任务占用屏幕时在屏幕顶部居中显示状态胶囊（蓝色＝正在操控 · 按 F8 暂停；琥珀色＝已暂停 · 按 F8 继续），点击穿透且 `WDA_EXCLUDEFROMCAPTURE` 不进入截图；暂停/继续经 `/api/runs/<id>/pause|resume` 与 F8 同步刷新胶囊与边框状态。边框高亮由 70ms 四帧硬切换改为 120ms 连续呼吸（0.5~1.0，3.6s 一个周期），每 4s 在蓝→紫→青→天蓝之间平滑换色；暂停时停止动画显示静态琥珀色；新增 `tests/test_overlay.py` 与暂停状态联动测试，测试总数 181。
- 2026-11-09：修复 Windows-MCP 启动即退出的问题。根因：固定的 0.8.8 要求 Python ≥3.14，而应用与资源构建使用 3.12，`pip install -e .` 实际失败，`.venv` 里只有 pip，服务一启动就退出；资源检测又只看 venv/python.exe 是否存在，把「安装到一半」误判为已就绪，导致界面不触发修复。现在固定到支持 Python 3.12 的 0.8.5，资源检测把安装阶段非 `ready` 判为未完成，MCP 启动失败时错误信息附带 stderr 与退出码；重建后 `tools/list` 通过，测试增至 203。
- 2026-11-09：资源检查从「文件齐不齐」升级为「能不能真的跑」。新增 `bootstrap.probe_adapter_kind()`：按资源 id 找到配置中的同类型后端，实际启动 MCP 子进程并完成 `initialize` + `tools/list`，返回可用性、工具列表与失败原因；资源检测接口在结构检查后追加可用性探测，界面逐项显示「运行可用 / 运行不可用」和握手工具数，必需资源运行失败时任务判失败，并提供「强制重装失败项」（跳过已就绪判断重装）；新增 `resources check` CLI（结构 + 真实握手，必需资源不可用时退出码 1）；结构未就绪的资源不再复用旧探测结果。真机验证 Windows-MCP 20 个工具、Computer-Use 1 个、xshell-mcp 6 个均握手通过；新增 7 个测试，测试总数 210。
- 2026-11-09：任务收尾体验：`task.finish` 提示词要求模型用简体中文总结（成功以「已完成」开头并描述当前可见状态；无法完成时传 `success=false` 且以「未能完成：」开头说明原因与剩余工作），运行时新增 `TaskResult.summary`，非中文 runtime 原因自动翻译为中文总结；任务流把逐个工具卡片改为 Codex 风格的可折叠「执行工具 N 次」分组（失败数、当前/结束状态，思考、模型输出、审计详情内嵌，结束时自动折叠），结束位置显示可读的「任务总结」正文（支持标题、列表、加粗、行内代码与代码块）；任务结束（含失败/中止/需人工）通过 PowerShell WinRT 发送 Windows 系统通知，GUI 异步、CLI 同步；新增 summary/notify 及结束总结、失败声明、通知回调测试，测试总数 225。
- 2026-11-09：新增 `auto` 后端与任务级模型选择。auto 由模型按目标挑后端（关键词兜底）：普通桌面任务优先 Windows-MCP，SSH/远程/Linux 走 xshell，Windows-MCP 观测失败时回退 Computer-Use（仅可识图模型）；选择过程以 `route` 事件显示原因与兜底链。发送框新增按厂商分组的模型选择，仅作用于本次任务并随队列持久化；「审计模型」选择移至任务标题右侧，同样按厂商分组并持久化，默认跟随执行模型，删除对应厂商/模型时自动回退。新增 auto 路由、审计选择、队列模型透传与 route 事件测试，测试总数 239。
- 2026-11-09：修复长任务中途失败且无有效提示。根因：模型返回 JSON 前后夹带解释或推理被截断时，`parse_action_json` 直接抛错，Runtime 首次异常就结束任务；观察失败同样立即终止；默认步数 12 偏少。现在：JSON 解析容忍前后解释与 Markdown 围栏并提取首个平衡对象；提示词要求只输出 JSON；决策请求固定 8192 tokens；每步模型失败按 `max_provider_retries` 重试并注入“只输出合法 JSON、缩短 rationale”的纠正指令，429/超时/连接/5xx 退避重试，401/403 等鉴权错误直接失败；观察失败重试一次；`max_steps` 默认提高到 30；界面显示 `provider_retry` / `provider_recovered` / `observation_retry` 事件；新增解析容忍、重试成功/耗尽/鉴权短路、观察重试测试，测试总数 247。
- 2026-11-09：视觉提速与回退风险识图审查。运行时支持模型一次返回 1–4 步批量计划：计划只观测/决策一次，中间步骤只校验回执（`deferred`），末步统一验证；终止动作截断批次，任一步失败丢弃剩余动作并重新观测；`max_steps` 改为按动作计数。截屏统一缩到 1280 宽并在 JPEG q80 与 PNG 中择优，记录缩放系数还原坐标（Pillow 可选，缺失时保留原图）。观测截图哈希与模型 `screen_card` 组成屏幕缓存，哈希未变化时不再上送图片（`screen_cache` / `screenshot_cached`）。新增回退风险审查：提交/保存/删除/覆盖等 UI 动作、上传、`git push`、提权、高/严重敏感度或显式 `rollback_risk` 进入 `needs_rollback_review`，其中 `desktop.act` 强制附带当前截图给顶层审计模型；无识图模型或无截图时失败关闭并强制用户确认，识图判决不缓存。审计模型选择强制为可识图模型（API 拒绝纯文本模型，未显式选择时回退到可识图执行/活动/首个可识图模型），界面审计下拉只列可识图模型并显示回退风险识图标记。新增批量计划、屏幕缓存、截屏压缩/坐标还原、识图审查与审计模型校验测试，测试总数 267。真机自测中发现并修复：启动引导未把 `ai` 状态写入前端，发送框模型默认值会回落到首个厂商而非当前活动模型。
- 2026-11-09：登录阻塞与用户接管（TECH_DESIGN 4.17）。新增 `task.await_user` 能力与系统提示：需要登录且目标/指令未提供账号密码时，模型必须中止并请求用户登录，不得绕过、猜测、注册或找回密码。运行时增加确定性兜底：UIA 观测到登录页（登录关键词 + 密码/账号字段，或「请先登录/扫码登录」）且目标、任务上下文、用户指令都不涉及登录/注册/凭据时，强制把动作改写为 `task.await_user`，任务以 `needs_human` 结束并返回待办提示。面板支持同会话恢复：用户回复「继续/已登录」时复用被阻塞任务的原目标，注入「用户已完成暂停事项，请从当前屏幕继续原任务」的上下文，队列同样透传用户消息与上下文；无待处理交接时显式 resume 返回明确错误。前端新增等待横幅、「我已登录，继续」按钮与输入提示，切换会话可恢复等待状态。新增登录阻塞、凭据豁免、恢复链路与无待办拒绝测试，测试总数 273。
- 2026-11-09：补数据保留、用量来源统计与跨后端契约测试。`ai_usage` 增加按 `source` 聚合，用量页新增「审计 Token」卡片（占总量比例与审计调用次数），审计开销不再混在总用量里无法区分；新增 `maintenance.cleanup`：按 `[audit].keep_days` 清理过期审计 JSONL 与 `[runtime].screenshot_dir` 下的截图（只匹配扩展名、不递归目录、目录缺失不创建），CLI 增加 `cleanup --dry-run/--keep-days`，`run` 与 `gui` 启动时自动执行一次，`AuditLog.prune` 支持 `dry_run`；新增 `tests/test_contract.py`：同一组用例参数化跑 mock / windows / computer_use / xshell，验证 observe 结构、终止能力失败关闭、verify 结构与 close 幂等；新增清理与用量来源测试，测试总数 294。
- 2026-11-09：前台独占补全（TECH_DESIGN 4.19）。新增 `ui/desktop.py` 的 `DesktopHandoff`：需要屏幕的任务开始前最小化控制台窗口，任务结束、失败、取消或等待用户接管后恢复；暂停（F8 / 面板）与用户确认弹窗会把窗口调回前台，继续后再次让出；多任务并发时以「最后一个屏幕任务结束」为恢复点，避免误恢复。`run_gui` 通过 pywebview 的启动回调把真实窗口交给接管器，浏览器模式与未创建窗口时全部静默跳过；`[runtime].release_desktop` 可整体关闭，bootstrap 暴露 `desktop_handoff` 供界面判断。新增 `tests/test_desktop.py`（幂等最小化/恢复、窗口异常吞掉、配置开关、暂停/确认联动、bootstrap 字段），测试总数 305。
- 2026-11-09：暂停菜单补「截图提问」（TECH_DESIGN 4.20）。暂停中可对当前屏幕提问：`POST /api/runs/{id}/ask_screen` 校验任务存在且已暂停，用本次任务的模型（可识图）截图提问；纯文本模型前端禁用按钮、后端返回明确错误。截图前临时收起控制台（`DesktopHandoff.release` + 350ms），拍完立即恢复，避免窗口遮住要问的内容。答案只渲染在暂停横幅内，不入库、不进入任务上下文，桌面不会被点击或改动。新增 `tests/test_screen_ask.py`（问题校验、暂停/结束校验、纯文本拒绝、截图隐藏与恢复、截图/模型/空答案失败分支）与 `ask_screen` 路由测试，测试总数 317。
- 2026-11-10：新增「浏览器功能（内置浏览器通道）」设计（TECH_DESIGN 4.21），仅文档，未实现代码。明确该模块为与桌面/终端并列的独立执行通道：独立 Chromium 进程 + 持久化 profile + CDP 截图与 DOM 快照 + screencast 镜像 + 扫码接管；运行时不截屏、不显覆盖层、不让出桌面。同步 STATUS 头部标注与 P1 待办（8 项，全部未开始）、README 新增「规划中」章节，避免文档把规划当成已完成。依赖未安装、测试数仍为 317。
- 2026-11-10：浏览器功能方案修订（TECH_DESIGN 4.21 重写）。引擎范围收敛为 **Microsoft Edge、Google Chrome、内置 Chromium 兜底**三种，其余不支持；明确采用**独立 profile**（按引擎隔离，不复用用户真实浏览器数据，首次各扫一次码）。新增 4.21.3 引擎解析与 profile 隔离、4.21.4 内置 Chromium 接入资源完整性（`kind="browser"`、`start.cmd` 检查、`/json/version` 探针、国内镜像）、4.21.11 资源更新检测（Releases/Tags + 兼容门禁 + 旁路安装 + 探针通过才切换 + 回滚 + off/notify/auto）。分阶段补 P5 资源与更新。仍未实现代码。
- 2026-11-10：浏览器通道**放弃 Playwright，改为纯标准库直连 CDP**，并完成 P0 地基实测。起因是 `pip install playwright` 在本机反复 `ReadTimeoutError`（`files.pythonhosted.org` 拉大 wheel 超时），排查后确认 CDP 层已足够覆盖全部需求，且去掉依赖能直接满足「打包后零环境依赖」。TECH_DESIGN 4.21 相应重写：新增 4.21.2 驱动选型（CDP 方法与 Playwright 对应物对照、四条分发优势、代价）、4.21.3 改为直接 Popen 启动 + 删除 `DevToolsActivePort` 残留、4.21.4 内置内核改用 Chrome for Testing 官方构建、4.21.8 补登录态必须优雅关闭 + DPAPI 快照兜底、新增 4.21.14 实测数据与两个必踩坑、新增 4.21.15 打包与零环境依赖（依赖分层、下载器约定、首次运行流程、离线降级）。实测：启动 0.16s、截图 133ms/7KB、点击 14ms 精确命中、镜像 72ms/帧约 14fps、中文 insertText 需先聚焦、硬杀丢 Cookie 而 `Browser.close` 保留、headful 可启动。STATUS 浏览器待办从 10 条调整为 13 条（P0 已完成，新增 `browser/` 子包、登录态兜底、零依赖分发三项）。代码仍未落地，测试数仍为 317。
- 2026-11-10：**浏览器通道核心模块落地**（TECH_DESIGN 4.21 P1 前半）。新增 `src/xm_agent/browser/`：`cdp.py`（RFC 6455 分帧 + 后台读线程 + 写锁 + 事件订阅 + `post()` 只发不等，避免镜像 ack 在读线程里同步等待导致死锁）、`engine.py`（引擎解析含注册表 App Paths、`--product-version` 版本探测、`ProfileLock` 进程内注册表 + 跨进程 pid 锁文件 + 残留锁清理、启动前删 `DevToolsActivePort`、优雅关闭）、`session.py`（`BrowserSession` / `Page`，多标签、导航、观测、动作、镜像、`ensure_visible()` 切可见模式）、`snapshot.py`（截图按 `clip+scale` 缩放、可交互元素清单带中心点坐标、无障碍树精简、可见文本）、`input.py`（鼠标/滚轮/`insertText`/按键映射/等待）、`mirror.py`（帧流 + 只留最新帧 + 静止页面兜底首帧）。配置新增 `[runtime.browser]`（engine / profile_root / headless / keep_alive_seconds / mirror_fps / screenshot_max_width / connect_timeout）并校验非法值。新增 32 个用例：`tests/test_browser_engine.py` 18 项纯逻辑（三级回落与降级提示、指定引擎不静默替换、锁互斥与残留清理、配置解析与校验、JS 转义），`tests/test_browser_session.py` 14 项真机（启动、导航、快照结构、JPEG、坐标点击命中、按序号点击、中文输入、无焦点丢弃、按键与滚轮、等待文本、镜像帧、静止页首帧、AX 树、统计、优雅关闭保留登录态），无浏览器环境自动跳过。测试总数 349。
- 2026-11-10：**资源自动升级落地（TECH_DESIGN 4.22.15）**。新增 `src/xm_agent/resources/upgrade.py`：`Upgrader` 串起「旁路安装 → 探针 → 切换 → 失败回滚」，`ResourceManager` 增加 `stage/switch/discard/prune/rollback` 与 `active.json`（在用版本）/`attempts.json`（失败冷却 6h）。旧版本「出厂钉住」语义不变：清单里的 `version` 仍是要构建的版本，升级记录放在数据目录，`expected()` 优先用它、目录不在就自动忽略。探针按 kind 分流：MCP 把配置里指向稳定入口的 `command`/`cwd` 改写到旁路目录后跑真实 `tools/list`，内核跑 `chrome.exe --version`。`update_check` 增加 `auto` 档（同大版本、无任务在跑、不在冷却期才动），面板资源行新增「升级到 x.y.z」与「已升级 vX · 回滚 vY」按钮，CLI 新增 `resources upgrade|rollback`。内核没有稳定入口链接，因此 `browser/engine.py` 改为先读启用版本记录，升级与回滚对内核同样成立，读数失败回落「取最新」。
  真机测出两个真问题并修掉：① `make_prober` 曾用 `resource.dest`（清单版本）而非探针入参做路径改写，导致「验证旧版、切换新版」——假 `python.exe` 也能探针通过，已改为从 `dest` 推导并加回归用例；② `_tail` 把 stderr 拼在 stdout 前面，pip 的 `ERROR:` 被截掉，现在 stderr 放最后并用 `reason_of()` 提取原因行。真机结果：上游 v0.8.8 与本机 Python 3.12 不兼容，构建失败后入口仍指 0.8.5、状态「已就绪」、`attempts.json` 记下 `requires a different Python: 3.12.10 not in '>=3.14'`；另造「构建成功但起不来」的旁路版本，探针 0.33s 报 WinError 216、旁路目录被删、入口不变；真实配置对 0.8.5 目录跑改写后的命令握手成功（20 工具、3.32s）；界面点「升级」→「已升级 · 回滚」→ 点回滚 → toast「已切回 0.8.5」，入口与记录同步回到 0.8.5。新增 `tests/test_resource_upgrade.py` 23 项，测试总数 456。
- 2026-11-10：**修复自测反馈的两个真 bug（TECH_DESIGN 4.22.16）**。① CDP 空闲超时被判成断线：socket 超时既是 connect 上限也是每次 `recv` 上限，页面静止时不推消息，`recv` 超时被读线程当致命错误，之后每次调用都报「连接已关闭: 读线程退出: timed out」——用户自测的失败任务正是此因（06:26:17 观测 → 模型想 26s → 06:26:44 失败）。改为在 `_read_exact` 里把超时当「继续等」，半帧数据保留，帧边界不错。② 连接/页面不自愈：新增 `live_connection()`（原地重连 + 按 target 重新 attach + 重启镜像）、`Page.call` 重试一次、`Page.conn` 返回活连接、`_PageLink` 代理让快照/输入自动获得自愈；内核被杀时通道自动重启并换新页面。③ 面板 `index.html` 引了 `/static/browser.js` 但 `_ASSETS` 没登记 → 脚本 404 → 浏览器按钮永远隐藏（点了没反应），补登记并加「引用即取得到」的用例。顺带：`status()` 不再向上抛（面板轮询不该把区域打没）、状态栏不再显示「无头 · 无头」。新增 `tests/test_browser_cdp.py` 4 项 + 真机 3 项（空闲 30s / 断连重连 / 内核死亡）+ 面板资源 2 项，测试 456 → 465。
- 2026-11-10：**修复第二次自测失败（TECH_DESIGN 4.22.17）**。用户重跑「搜索自如官网」，报 `RuntimeError: 页面求值失败: TypeError: history.reload is not a function`，随后的 `goto` 验证仍不过，连续 2 次失败判死。两个独立 bug：① `back/forward/reload` 原来统一走 `evaluate("history.<action>()")`，而 `history.reload` 根本不存在（刷新在 `location` 上，且 `location.reload()` 会让求值上下文销毁、回包丢失）。改为 CDP 原生：`Page.reload` + `Page.getNavigationHistory`/`navigateToHistoryEntry`，历史越界返回 False 并给出「已经是最前一页」；配套把导航中的 `Inspected target navigated or closed` 归为瞬时错误重试、`Not attached to an active page` 归为 stale session 重挂（连续两次失败才判失效）。② 无头模式 UA 带 `HeadlessChrome/157.0.0.0`、`navigator.webdriver=true`，自如的腾讯 EdgeOne 直接返回「请求已被站点的安全策略拦截」——这也是上一轮失败的真因之一（当时只看到「无交互元素」）。修法：无头启动按内核真实版本重组成标准 UA（`Chrome/157.0.0.0` + `Edg/157.0.4341.42`）并加 `--disable-blink-features=AutomationControlled`；每个文档注入脚本把 `Navigator.prototype.webdriver` 的 getter 改成 undefined，`new_page` 改为先开空白页→注入→导航，重连后重新注入。另加 `wait_content`（readyState=complete 不等于有内容，SPA 首屏之后才渲染，否则观测看到空标题 0 元素会被误判成拦截）与拦截页识别（元素 ≤3 且正文含强特征时在 summary 明说「重复请求/刷新不会放行」）。真机：`https://www.ziroom.com/` 直接拿到真实首页（39 个可交互元素），reload → goto → back → back → forward 全通，`verify("自如")` 通过；对照组（不注入）稳定返回拦截页。新增 10 项用例（5 单元 + 5 真机），测试 465 → 478。
- 2026-11-10：**浏览器改为「一个可见窗口、任务结束可直接接手」（TECH_DESIGN 4.22.18）**。按反馈把内置浏览器从「无头、一次任务一个」改成用户能接着用的那一个：
  ① `[runtime.browser].headless` 的 `auto` 由「无头」改为「可见」，只有调用方显式要求时才无头（`session.auto_mode()` 保留按环境回退的余地）；
  ② 启动命令不再附带 `about:blank`，内核自己开的那一页就够——原先会多出一个永远空着的标签，窗口里并排两个标签；
  ③ 新增 `BrowserChannel.focus_single_tab()`：新任务开工前把多余标签关掉、只留当前这一页（用户可能正站在上面等 AI 接手），不再把上几次任务翻开的标签越攒越多；
  ④ 任务结束不再关浏览器，收尾改为把真窗口提到最前；
  ⑤ 新增 `browser/window.py`：CDP 的 `Page.bringToFront` 只激活标签页，真机上IDE 占着前台时窗口一动不动，故补一次 Win32 `SetForegroundWindow`（按进程枚举顶层窗口，按有无 owner 与面积挑出主窗口，避开翻译/下载气泡），非 Windows 直接跳过；
  ⑥ 观感清理：翻译气泡在 Edge 下走自己的开关名，真机对比确认 `Translate,TranslateUI,msEdgeTranslate,EdgeTranslate` 四个一起关才不出现；`--disable-blink-features=AutomationControlled` 会让页面顶部挂「不受支持的命令行标记」提示条，成对加 `--test-type`。
  顺带修掉一个真实竞态：任务收尾原本是「先宣告 run 终态、再写 assistant 消息」，轮询方一看到 `needs_human` 就来读对话，会读到最后一条还是用户那句「继续」（`test_await_user_handoff_and_resume` 因此偶发失败）；现在改为先落消息再 `finish_run`。新增 `tests/test_browser_window.py` 与启动参数、单标签、可见模式回归用例，测试 480 → 489，lint (F,E9) 全绿。
