# 电脑操控 Agent 技术方案（v0.1）

> 范围：Windows 桌面 + 远程 SSH 的可控自动化。
> 本版本不包含本地小模型部署。
> 状态：设计稿，尚未开始编码。

## 0. 结论与边界

目标：通过 MCP 统一接入桌面、终端、文件能力，由 Agent 完成"观察 → 决策 → 执行 → 验证"闭环，实现对电脑的直接操控。

本版本不做：

- 本地小模型（0.8B / 7B 级）部署与推理
- 多租户、跨机器任务调度
- 无人值守的破坏性操作
- 同一桌面并发运行多个 Agent
- 真正的系统级多桌面隔离（每个会话一个隐藏桌面并实时内嵌显示）
- 同一桌面并行运行多个任务
- Windows 多鼠标 / 独立第二光标
- 本机 Xshell 真实会话验证（框架保留，见 4.3 与 STATUS）

设计原则：

- 单层网关、单执行器、单仲裁器
- 所有动作可审计、可中止、可回放
- 结构化观测优先，视觉仅作兜底
- 危险动作默认拒绝，必须人工确认

---

## 1. 技术选型

### 1.1 选型总览

| 层 | 选型 | 备选 | 说明 |
|---|---|---|---|
| MCP 网关 | `mcpproxy-go` 或 `1MCP` | ContextForge（企业多租户）、MetaMCP | 只保留一层聚合，不叠加 moxy + router + proxy |
| 桌面执行器 | Windows-MCP（CursorTouch） | dsh-cua | UIA 元素树优先；dsh-cua 自带人类让出仲裁，但需核实打包与维护状态 |
| 视觉兜底 | computer-use-mcp（domdomegg/computer-use-mcp，已选定） | 自研 screenshot-act 循环 | 必须配置支持图像输入的模型才启用；仅 UIA 失败时降级 |
| 远程终端 | Xshell MCP（首个后端，本机暂未安装） | SSH / SFTP MCP、XTerminal MCP | 中间层抽象为 `terminal.exec`，后续可平滑替换后端 |
| 编排运行时 | Python 3.12+ + 官方 MCP SDK | TypeScript + MCP SDK | Python 的 Windows 自动化生态更完整 |
| 模型 | 云端多模态模型（OpenAI / Anthropic / Gemini 兼容） | Claude / GPT / Gemini / GLM / DeepSeek / Kimi / Grok / 本地 Ollama | v0.1 不内置本地小模型部署；做多协议供应商抽象、官方模型列表与用量统计 |
| 互斥仲裁 | 运行时内置 lease lock | Redis 锁（多机时） | TTL + 心跳 + fencing token，不暴露给模型 |
| 策略引擎 | 网关侧默认拒绝 + 任务级 capability | OPA（后续） | 工具白名单、参数校验、主机/路径范围 |
| 审计 | JSONL + 截图 + 动作回执 | SQLite 索引 + OpenTelemetry | 每一步可回放 |
| 配置与密钥 | SQLite + Windows DPAPI 加密 | Vault（企业） | 禁止明文存放密码与私钥；界面只显示密文提示 |
| 打包 | Python 运行时 + CLI，用户会话内托盘进程 | Tauri 桌面壳（后续） | 不注册为系统服务（见 5.4）；v0.1 先做 CLI 与本地运行时 |
| 测试 | pytest + 固定任务集 + 回放 | Playwright（浏览器场景） | 先建基线，再谈优化 |

### 1.2 关键取舍

**为什么只保留一层网关**

moxy、mcp-tool-router、mcp-proxy 的职责高度重叠：命名空间、转发、别名、策略。串行叠加只增加一跳延迟、一套配置和三倍排障成本。单机单 Agent 阶段甚至可以不用网关，由宿主直接挂载多个 MCP Server；有多客户端或远程接入需求时再加。

**为什么 UIA 优先、视觉兜底**

UIA 返回结构化元素树，坐标与控件天然对齐，不受 DPI 缩放影响，token 消耗低，动作确定性强。视觉方案适合覆盖 Electron、自绘 UI、游戏、远程桌面画面等 UIA 盲区，但延迟高、成本高、grounding 不稳定，只作为第二条通道。

**为什么 Xshell 是首个后端，但不绑死 Xshell**

运维当前使用 Xshell，v0.1 接入 Xshell MCP 是合理的：复用已有会话与凭据，迁移成本最低。已确认 2head1i/xshell-mcp 的实现方式不是 COM 或 SSH 封装，而是在 Xshell 标签页内运行 `bridge.js`，通过本地文件队列收发请求；因此不需要保存服务器密码，但要求用户先在该标签页启动 bridge。它只返回屏幕文本，没有退出码与 SFTP 工具，所以中间层仍须把 `terminal.exec` / `file.transfer` 作为独立能力，并保留 SSH / SFTP 作为可切换后端。后续新增其他终端 MCP 时，只改中间层映射，不改 Agent。当前开发机未安装 Xshell，因此该后端只保留框架，不作为本轮主线。

**为什么锁不暴露给模型**

如果 `claim` / `release` 作为 MCP 工具交给模型，模型会忘记释放，进程崩溃后形成死锁。锁应由编排器按任务自动获取和释放，带 TTL、心跳和 fencing token，人类输入永远优先。

**为什么 v0.1 不内置本地小模型**

0.8B 级模型无法稳定输出点击坐标；7B 级视觉模型在 8GB 显存上会溢出到 CPU，单步延迟不可接受。先用云端视觉模型跑通闭环和评测集，再评估本地化收益。如果用户已有 Ollama 等本地推理服务，只把它作为「无密钥的自定义端点」接入，不随应用打包模型文件。

### 1.3 工具面收敛

对外只暴露少量高阶工具，底层通道由编排器选择，不交给模型选：

```text
desktop_observe    # 获取 UIA 元素树，可选附带截图与活动窗口
desktop_act        # 按 element_id 或坐标执行单一动作
desktop_verify     # 校验元素状态、期望文本或截图差异
shell_exec         # 本地或远程命令，走白名单
file_transfer      # 上传 / 下载，限定目录
wait_for           # 等待文本、元素或窗口出现
task_abort         # 中止当前任务并释放锁
```

长尾工具通过网关的工具搜索与渐进披露按需挂载；核心工具始终可见。

### 1.4 待确认项

- Windows-MCP 与目标业务软件的 UIA 覆盖率（已在本机完成 `tools/list` 与 `Snapshot` 只读验证）。
- 云端模型供应商与数据合规要求（截图是否允许出内网）。
- Computer-Use MCP 的 Windows 兼容性：nut.js 原生模块、截图缩放与 DPI 映射需在真实设备验证。
- Xshell MCP 的稳定性：实现方式已确认（bridge.js + 文件队列），但本机未安装 Xshell；待有环境的机器验证长命令、分页输出与断线恢复。
- Xshell 许可证是否允许自动化调用。
- `moxy`、`mcp-tool-router`、`mcp-mcp-locks`、`mcp-upgrade` 的许可证与 MCP 协议版本，接入网关前再评估。

### 1.5 能力契约与更新边界

Agent 不直接依赖具体 MCP Server 的工具名，而是依赖中间层提供的稳定能力契约：

```text
Agent → capability（稳定接口） → adapter（映射 / 标准化） → MCP Server（可替换）
```

v0.1 能力清单：

| 能力 | 输入 | 输出 | 可替换后端 |
|---|---|---|---|
| `desktop.observe` | 窗口 / 区域 / 是否截图 | 元素树 + 截图 + 活动窗口 | Windows-MCP（UIA）、computer-use-mcp（截图） |
| `desktop.act` | element_id 或坐标 + 动作（可选 button / clear / press_enter） | 执行回执 + 后置截图 | Windows-MCP、computer-use-mcp |
| `desktop.verify` | 期望条件 | 通过 / 失败 + 证据 | UIA 查询、视觉模型判图 |
| `terminal.exec` | 主机 / 命令 / 超时 | 退出码 + stdout / stderr | Xshell MCP、SSH MCP |
| `file.transfer` | 源 / 目标 / 方向 | 传输结果 | SFTP MCP、SSH MCP（xshell-mcp 不含 SFTP 工具） |
| `wait.for` | 文本 / 元素 / 窗口 / 超时 | 命中结果 | 各执行器原生能力 |

运行时在每次决策前自动执行 `desktop.observe`；模型若仍请求 `desktop.observe`，由 Runtime 重新观察并记为成功步骤，不会作为动作转发给后端（后端只实现 act / verify / wait）。

更新边界：

| 变化类型 | 是否改 Agent | 改哪里 |
|---|---|---|
| 新增一个 MCP Server | 否 | 中间层注册 + 能力映射 |
| 替换同类后端（Xshell → SSH） | 否 | 中间层后端配置 |
| 工具改名、参数增减、传输方式变化 | 否 | adapter 映射与参数转换 |
| 返回结构变化 | 通常否 | adapter 结果标准化；补契约测试 |
| 能力语义变化（如点击后不再保证焦点） | 是 | 契约版本 + Agent 验证逻辑 + 评测集 |
| 新增一种能力（如浏览器 DOM） | 是 | 新能力定义 + 工具描述 + 路由规则 |

结论：“更新 MCP 不改 Agent 代码”在**实现替换和协议适配**层面成立；在**能力语义变化**层面不成立。中间层的价值是把前者做到配置化，把后者显式暴露出来，而不是假装不存在。

配套要求：

- 每个能力带版本号与 JSON Schema，不兼容变更升级主版本。
- 工具描述由中间层生成，保证模型看到的接口稳定，不受上游描述变化影响。
- 每个后端必须通过同一组契约测试才能上线或替换。
- 版本锁定 + 冒烟测试 + 灰度 + 回滚，禁止控制面自动升级。

---

## 2. 硬件要求

### 2.1 运行机（Windows）

| 项目 | 最低 | 推荐 | 说明 |
|---|---|---|---|
| 操作系统 | Windows 10 22H2 x64 | Windows 11 23H2+ x64 | UIA、输入注入、截图 API 的兼容性基线 |
| CPU | 4 核 | 8 核 | 同时运行 MCP Server、目标应用、浏览器、Xshell |
| 内存 | 16 GB | 32 GB | 截图缓冲、多进程、目标应用占用 |
| GPU | 集成显卡 | 独立显卡（可选） | 不跑本地模型，无显存硬性要求；独显可加速截图与渲染 |
| 磁盘 | 20 GB 可用 | 50 GB SSD | 审计日志、截图、会话录像 |
| 显示 | 1920×1080 | 双屏 | 需支持多显示器与 DPI 缩放 |
| 网络 | 10 Mbps | 50 Mbps | 云端视觉模型调用、SSH、依赖下载 |
| 外设 | 物理键盘鼠标 | 物理键盘鼠标 | 支持人工随时接管 |
| 权限 | 普通用户 | 管理员 | 部分目标应用需提权才能读取 UIA 树 |
| 会话 | 本地控制台 | 本地控制台 | 避免 RDP 断开导致锁屏、输入失效 |

### 2.2 不建议的配置

- 8 GB 内存：多 MCP Server + 浏览器 + 目标应用容易触发换页。
- 仅 4K + 200% 缩放环境：坐标换算与元素命中需要额外适配，v0.1 先覆盖 100% / 125% / 150%。
- 无物理键鼠的无人值守环境：出现异常时无法人工接管。
- 同一桌面并发运行多个 Agent：焦点、键盘、剪贴板会互相抢占。

### 2.3 目标服务器

- 支持 SSH / SFTP 的 Linux 或 Windows 主机。
- 网络可达，凭据由运行机侧托管。
- 无需特殊硬件；如需图形界面，优先使用命令行替代。

### 2.4 可选隔离环境

高风险任务建议在 Windows 11 虚拟机中执行：

- Hyper-V 或 VMware
- 4 vCPU / 8 GB 内存 / 60 GB 磁盘
- 执行前创建快照，任务结束回滚
- 虚拟机内同样只运行一个 Agent

### 2.5 容量估算

- 1080p 截图：PNG 约 1–3 MB，JPEG 约 200–500 KB。
- 按每秒 1 步、每天 8 小时估算，原始截图约 5–15 GB/天，需配置压缩、采样与保留策略。
- 审计 JSONL 体积很小；截图与录像才是主要占用。
- 建议默认保留 7 天，重要任务单独归档。

---

## 3. 语言与运行环境

### 3.1 运行环境结论

| 组件 | 语言 / 运行时 | 版本 | 运行方式 |
|---|---|---|---|
| 主机 | Windows x64 | Windows 10 22H2+ | 本地控制台会话 |
| Agent Runtime | Python | 3.12+（3.13 可用） | `uv` 虚拟环境；CLI 或本地服务 |
| Windows 执行器 | Python（Windows-MCP） | 3.13+ | `uvx windows-mcp serve`，独立运行环境 |
| Xshell 执行器 | 取决于上游实现 | 待确认 | stdio 或 HTTP；需确认是 COM / GUI / SSH 封装 |
| 终端备选后端 | Python / Node / Go | 按上游 | SSH / SFTP MCP，stdio 或 HTTP |
| MCP 网关（本地） | Go / Node.js | Go 1.22+ / Node 20 或 22 LTS | 单二进制或 Node 进程 |
| MCP 网关（企业） | Python + Docker | 按镜像 | ContextForge，容器部署 |
| 脚本层 | PowerShell / Bash | PowerShell 7.4+ / Git Bash | 本地自动化与 CI |
| 模型接入 | 无本地运行时 | — | HTTPS 调用 OpenAI / Anthropic / Gemini 兼容端点；可选本机 Ollama |

补充说明：

- 不要求全栈统一语言：MCP 的价值就是按上游实现接入，只有 Agent Runtime 和适配层由我们维护。
- Agent 侧统一使用 Python，避免同时维护 Python 与 Node 两套编排逻辑。
- 所有本地 MCP Server 由 Runtime 以 stdio 拉起；远程网关走 HTTP / SSE。
- 用户 API Key 经 Windows DPAPI + 固定 entropy 加密后写入 SQLite，仅在运行时解密注入；不进入代码、配置文件、日志或模型上下文。环境变量仅作为未配置活动模型时的回退来源。
- 关键环境变量：`PYTHONUTF8=1`、`chcp 65001`、代理与 `NO_PROXY`、模型 API 地址与凭据。

### 3.2 开发语言栈

| 用途 | 语言 | 版本 | 说明 |
|---|---|---|---|
| 编排运行时 | Python | 3.12+（3.13 可用） | 使用 `uv` 管理依赖与虚拟环境 |
| MCP Server | Python / Node.js | 以现有项目为准 | 不重写上游 Server，只做适配 |
| 网关二次开发 | Go | 1.22+ | 仅在需要修改 mcpproxy-go 时使用；否则直接用二进制 |
| Windows 脚本 | PowerShell | 7.4+ | 避免依赖 Windows PowerShell 5.1 的编码问题 |
| 桌面 UI（后续） | TypeScript + Tauri | Node.js 20/22 LTS | Tauri 体积小；Electron 仅作为备选 |
| Shell 脚本 | Bash | Git Bash 自带版本 | 本地开发与 CI |
| 测试与静态检查 | Python | pytest / ruff / mypy | CI 必跑 |

注意：Windows-MCP 对 Python 版本有自身要求，通过 `uvx` 启动时由工具自行管理运行时，不与应用侧 Python 环境混用。

### 3.3 编码与本地化要求

- 全链路 UTF-8：设置 `PYTHONUTF8=1`，终端执行 `chcp 65001`。
- 路径可能包含中文和空格，所有命令与配置必须正确引用。
- 中文输入优先使用 UIA `ValuePattern` 或剪贴板粘贴，避免逐键输入触发输入法组合态问题。
- 兼容 GBK 遗留程序：读取输出时做编码探测与显式转换。
- 终端输出需剥离 ANSI 控制符后再交给模型。
- 工具描述使用英文，提升模型选择稳定性；系统提示与用户界面使用中文。

### 3.4 自然语言要求

- 支持中文与英文任务指令，默认中文交互。
- 破坏性操作的确认提示必须使用中文，并明确列出目标、路径、主机与影响范围。
- 模型必须支持视觉输入与工具调用；v0.1 不依赖本地小模型。

---

## 4. 框架方向

### 4.1 三平面架构

```text
┌────────────────────────────────────────────┐
│ 决策面：Agent Runtime                       │
│ 任务分解 / 通道选择 / 状态机 / 失败恢复      │
│ 只依赖能力契约，不依赖具体 MCP 工具名        │
│ 内置 lease lock（不暴露给模型）              │
└──────────────────┬─────────────────────────┘
                   │ capability
┌──────────────────▼─────────────────────────┐
│ 中间层：能力适配 + MCP Gateway              │
│ 能力映射 / 结果标准化 / 命名空间 / 策略 / 审计│
└───────┬──────────────────┬─────────────────┘
        │                  │
┌───────▼────────┐  ┌──────▼──────────┐  ┌──────────────┐
│ Windows 执行器  │  │ Xshell / SSH    │  │ 视觉 CUA 兜底 │
│ UIA + 截图验证  │  │ 远程命令与文件  │  │ 需视觉模型    │
└────────────────┘  └─────────────────┘  └──────────────┘
```

### 4.1.1 中间层的两层结构

中间层拆成两层，避免把“协议网关”和“业务适配”混在一起：

1. 协议网关：命名空间、传输转换、鉴权、限流、重试、熔断、审计、只读缓存。
2. 能力适配层：把各 MCP Server 的工具映射为稳定能力，统一参数、结果、错误码与工具描述。

推荐路径：

- v0.1：能力适配层直接在 Agent Runtime 内，用配置驱动；先不引入独立网关进程。
- P2：多客户端或多机器时，再把能力适配层下沉到网关，Agent 只连一个端点。
- 不建议一开始就叠 moxy + router + proxy 三层。

网关只负责工具面，不承担编排职责；编排器不直接操作 MCP Server 的底层细节。

### 4.2 Agent 状态机

```text
IDLE
  → OBSERVE    获取窗口、UIA 树、可选截图
  → PLAN       选择通道与下一个原子动作
  → AUDIT      策略判定；敏感操作交顶层审计 / 用户
  → ACT        执行单一动作，获取回执
  → VERIFY     校验元素状态 / 文本 / 退出码 / 截图差异
  → RECOVER    重试、换通道、回退或中止
  → DONE / ABORT
```

任务级状态（与步骤状态机正交，见 4.14）：

```text
QUEUED → RUNNING ⇄ PAUSED
           │  ↑
           │  └── PREEMPTED（被插队任务抢占，保留会话与现场）
           └── DONE / FAILED / CANCELLED
```

约束：

- 每步只执行一个原子动作，便于验证与回放。
- 最大步数、总超时、token 预算、单任务成本上限必须可配置。
- 支持用户随时取消；取消后释放锁并记录当前状态。
- 破坏性动作、跨主机操作、权限提升必须经过人工确认门。
- 暂停只在步骤边界（ACT 前、VERIFY 后）生效；正在执行的单个 MCP 动作不强行中断，正在等待的模型请求应尽快取消。
- 暂停期间保留租约锁与桌面现场；插队时由调度器在同一进程内移交执行权，不并发运行。

### 4.3 通道路由

| 任务类型 | 首选通道 | 兜底 |
|---|---|---|
| 远程命令、部署、日志查看 | `terminal.exec`（Xshell 后端） | SSH 后端 |
| 远程文件上传下载 | `file.transfer`（Xshell SFTP 后端） | SFTP 后端 |
| 标准 Win32 / WPF / UIA 应用 | `desktop.act`（Windows-MCP） | 视觉 CUA（需视觉模型） |
| Electron / 自绘 UI / 游戏 | 视觉 CUA（需视觉模型） | 无 |
| 文件、进程、服务管理 | PowerShell / 文件 MCP | 无 |
| 浏览器网页操作 | 浏览器自动化或 DOM 模式 | 视觉 CUA |

路由优先使用规则匹配；规则无法覆盖时再由模型判断，避免所有任务都走视觉。

面板中的 `auto` 后端把上述规则实现为运行时路由：有真实后端时默认出现。先按 SSH / Xshell / 远程 / 服务器 / Linux 等关键词判断是否走 `xshell_mcp`，否则优先 `windows_mcp`；同时把可用后端及用途交给执行模型选择，解析失败或请求异常时回退关键词结果。`computer_use_mcp` 只在执行模型可识图时进入可选集，且当 Windows-MCP 观测连续失败时才作为兜底切换，切换过程通过 `route` 事件写入任务流与历史。发送框的模型选择只作用于本次任务，随队列持久化；标题栏右侧的「审计模型」选择保存在本机数据库，默认「跟随执行模型」，删除对应厂商或模型时自动回退。

### 4.4 观测标准化

- 统一使用虚拟屏幕坐标，记录显示器原点与缩放比例。
- 进程启动时声明 DPI 感知模式，避免坐标漂移。
- UIA 元素 ID 带 TTL，界面变化后失效，不跨步骤复用。
- 截图上送模型前按比例缩放，并记录缩放系数用于坐标还原；压缩细节见 4.16。
- 每次观测同时记录活动窗口标题、进程名、前台焦点状态。

### 4.5 执行与验证

- 动作幂等性尽量保证：点击前校验元素可交互，输入前清空目标字段。
- 每个动作返回回执：动作类型、目标、时间、耗时、截图、执行结果。
- 模型可将 1–4 个确定性动作组成一个批次（见 4.16）：中间步骤只校验回执，末步统一验证；任一步失败即丢弃剩余批次并重新观测。
- 验证优先使用结构化条件：元素存在、文本匹配、窗口标题、命令退出码。
- 无结构化条件时使用截图差异，但需设置阈值，避免动画与光标干扰。
- 连续失败达到阈值时停止重试，转入人工接管或中止。

### 4.6 安全与权限

- 网关默认拒绝，按任务签发 capability，只开放当前任务需要的工具与路径。
- 命令执行使用白名单，不依赖 `rm -rf` 之类的黑名单。
- 删除、覆盖、格式化、权限变更、跨主机传输必须二次确认。
- 密钥、密码、私钥不进入模型上下文，由执行器侧注入。
- 屏幕内容视为不可信输入：网页、终端输出、邮件文本都可能包含提示注入。
- 全量记录操作审计日志，敏感字段脱敏后落盘。

### 4.7 可观测与回放

- 每个任务生成 trace_id，贯穿所有 MCP 调用。
- 步骤日志包含：观测摘要、模型决策、工具入参、执行回执、验证结果。
- 截图与动作按步骤归档，支持按 trace_id 回放。
- 关键指标：任务成功率、平均步数、误点击率、恢复率、单步延迟、成本。

### 4.8 目录结构建议

```text
agent/
  runtime/            # 状态机、路由、锁、预算控制
  gateway/            # 网关配置与策略
  executors/
    windows/          # Windows-MCP 适配与回执标准化
    ssh/              # SSH/SFTP 适配
    vision/           # computer-use-mcp 适配与视觉兜底
  policy/             # 白名单、capability、确认门
  audit/              # 日志、截图、回放
  ui/                 # 回环 HTTP API + 静态页面 + WebView 窗口
  ai/                 # 厂商目录、DPAPI 加密、模型列表、活动模型装配、用量
  resources/          # 第三方 MCP 版本清单、镜像下载、安全解压、构建与检测
  storage.py          # SQLite 用户资料、任务历史、AI 配置与 Token 用量
  bootstrap.py        # adapter / provider / policy 装配
  config/
  start.cmd           # Windows 一键启动桌面控制台
  tests/
    tasks/            # 固定任务集与期望结果
```

### 4.9 里程碑

| 阶段 | 周期 | 内容 | 出口标准 |
|---|---|---|---|
| P0 | 1–2 周 | Windows-MCP + 文件 MCP + 编排器骨架 + 审计 | 10 个固定任务可重复完成 |
| P1 | 2–3 周 | UIA 主通道 + 截图验证 + lease lock + 中断恢复 | 20–30 个任务成功率 ≥ 80% |
| P2 | 2–3 周 | 能力适配层配置化 + 单层网关 + 第二终端后端 + 多客户端接入 | 新增或替换终端后端不改 Agent 代码 |
| P3 | 2–4 周 | 视觉兜底 + 策略引擎 + 回放 + 稳定性加固 | 盲区任务可降级完成，破坏性动作零误触 |

### 4.10 验收指标

- 固定任务集成功率 ≥ 85%
- 误点击率 < 2%
- 失败恢复率 ≥ 80%
- UIA 单步 p95 < 3 秒
- 视觉单步 p95 < 10 秒
- 未经确认的破坏性动作 = 0
- 任务中断后可恢复，锁无残留
- 契约测试全部通过；新增或替换同类 MCP 后端无需修改 Agent

### 4.11 非目标

- 本地小模型推理（允许连接用户自建的 Ollama，不随应用部署模型）
- 多租户与权限体系
- 跨机器任务编排
- 同一桌面多 Agent 并发
- 同一桌面多任务并行
- 真正的系统级多桌面隔离（隐藏桌面/内嵌实时视图）
- Windows 多鼠标或独立第二光标
- 完全无人值守的高风险操作

### 4.12 AI 模型接入与用量统计

**厂商目录与协议**

`src/xm_agent/ai/catalog.py` 用代码维护厂商预设（Preset）与套餐（Plan）。每个 Plan 描述协议、Base URL、模型列表风格与是否需要密钥，因此同一厂商的不同套餐可以指向不同端点：

| 场景 | 协议 | 示例 |
|---|---|---|
| OpenAI 兼容 | `openai` | OpenAI、DeepSeek、Kimi、Grok、OpenRouter、硅基流动、Ollama |
| Anthropic Messages | `anthropic` | Anthropic、GLM Coding Plan、Anthropic 兼容网关 |
| Gemini | `openai` + 原生模型列表 | Gemini OpenAI 兼容端点 + `v1beta/models` |
| 自定义 | `openai` / `anthropic` | 任何兼容端点，可编辑 Base URL 与模型列表接口 |

覆盖的厂商预设包括 OpenAI、Anthropic、DeepSeek、智谱 GLM（国内/国际 × 标准/Coding Plan）、Kimi（国内/国际）、Gemini、xAI Grok、OpenCode Zen（普通/Go）、Ollama、OpenRouter、硅基流动、阿里云百炼、火山方舟、MiniMax、Mistral、Groq、Together AI 与自定义端点。

**密钥与活动模型**

- 用户密钥使用 Windows DPAPI（`CryptProtectData`，固定 entropy）加密后以 Base64 密文写入 `ai_providers.api_key_enc`；`api_key_hint` 只保存 `sk-…abcd` 形式的提示。
- 读取接口默认不返回密文字段；只有运行装配（`build_active_provider`）与测试请求在内存中解密。
- 当前 `provider_id + model_id` 保存在 `app_state`；`bootstrap.build_provider` 优先使用活动模型，没有活动模型时才回退到 TOML 的 `[model]` 或内置启发式。
- DPAPI 密文与 Windows 用户及机器绑定；迁移数据库后需要重新填写密钥。

**模型列表与视觉能力判定**

`src/xm_agent/ai/models.py` 按 `models_style` 选择官方接口：OpenAI `/models`、Anthropic `/models?limit=100`、Gemini `v1beta/models`、Ollama `/api/tags`。`models_style` 由套餐（Plan）决定并在界面锁定，只有自定义端点允许手选，避免用户选错风格得到空列表。部分网关会用 HTTP 200 包裹业务错误（GLM Coding Plan 的 Anthropic 端点返回 `{"code":500,"msg":"404 NOT_FOUND","success":false}`），因此解析前先检查 `success:false` / `error` / 非 0 `code`；套餐可声明 `fallback_base_url` 与 `fallback_models_style`，主端点失败或无结果时自动回退（GLM Coding Plan → 标准 `/api/paas/v4`）。空结果不会清除已有模型。拉取后过滤 embedding / rerank / tts 等非对话模型。

模型是否支持图像输入由后端 `src/xm_agent/model_caps.py` 自动判定，用户不提供、也不能修改这个开关。判定顺序：

1. 官方列表里的显式元数据最权威：OpenAI 兼容网关的 `architecture.input_modalities` / `input_modalities` / `supported_input_modalities` / `modality` / `supports_image_in` / `capabilities`，Anthropic 的 `capabilities.image_input.supported`，Ollama 的 `capabilities` 与 `details.families`（含 `clip`）。
2. 列表不提供模态信息时，按维护在 `model_caps.py` 的官方模型家族表从模型 ID 推导（GPT-4o/4.1/4.5/5、o1/o3/o4-mini、Claude 3/4、Gemini 1.5+、Gemma 3、GLM-4V/4.xV、Qwen-VL/Omni、Kimi Vision、DeepSeek-VL、MiniMax-VL、Grok Vision、Pixtral、LLaVA、InternVL 等）。
3. 无法识别的自定义模型保守判为纯文本。

`ai_models.vision_source` 记录来源：`provider` 表示官方元数据，读取时直接采信；`model` 表示按名称推导，读取时用当前模型表重新计算；`unknown` 表示未识别。历史记录里由旧版界面写入的 `vision` 值不会被信任，升级后读取时自动重算。活动模型的最终 `vision` 决定是否在观测消息中附加截图；Computer-Use 后端在装配阶段拒绝纯文本模型，提示改用支持图像输入的模型或 Windows-MCP。

提供商表单内提供「模型名称」下拉框与「获取模型列表」按钮：填写 Key 后点击按钮会先保存提供商，再从官方接口拉取模型，选项即为官方模型（含自动判定的可识图标注），选中后立即切换活动模型。下拉末尾的「使用其他模型」只需填写模型 ID，能力同样由后端判定。切换厂商或套餐会清空待选列表；协议、Base URL 或模型列表接口变更时，后端会清除旧的官方模型缓存，避免跨端点误选。下方「模型明细」表格用于查看能力、来源与逐模型测试/删除。

**预设端点的权威性**

预设（非自定义）的厂商与套餐本身就是完整配置：协议、Base URL、模型列表接口与是否需要 Key 全部由 `preset_endpoint()` 从 Plan 读取，`save_provider` 忽略前端提交的这些字段，`build_active_provider` 在装配时再次按套餐覆盖，因此历史记录里的错误协议或旧 URL 不会影响运行。`overview()` 读取时若发现记录与套餐不一致会就地修复。界面只在「自定义 / 其他」时展示协议、Base URL、模型列表接口与凭据开关；预设只保留厂商、套餐、API Key（可选显示名称）与模型选择。Anthropic 协议的 Base URL 约定包含版本段（如 `https://open.bigmodel.cn/api/anthropic/v1`），Provider 在其后追加 `/messages`。

**连通性测试与用量**

- 测试请求使用 32–2048 token 的短提示，返回文本、耗时与 usage；测试本身也会写入用量，`source='test'`。`usage` 只在响应通过形状校验（确实包含文本内容）后记录，避免把 HTTP 200 包裹的业务错误记成 0 Token 调用。
- 用量汇总只统计 `total_tokens > 0` 的成功调用；Token 数值直接取自厂商返回的 `usage`（Anthropic 为 `input_tokens`/`output_tokens`，OpenAI 兼容为 `prompt_tokens`/`completion_tokens`），不做本地估算。
- `ai_usage` 记录提供商、模型、prompt/completion/total tokens、来源（`test` / `ui` / `cli`）与延迟；OpenAI、Anthropic、Gemini 的 usage 字段在 `providers/common.py` 中归一化。
- `ai_usage_summary()` 按模型与提供商聚合，面板展示总量、今日、占比饼图、分模型表和最近调用。
- 只统计 Token，不做金额换算；费用取决于厂商套餐与价格表，后续可独立增加。

### 4.13 第三方 MCP 资源管理

第三方 MCP 不随仓库分发，由 `src/xm_agent/resources/` 按固定清单安装；清单同时是版本与供应链的单一事实源：

| 资源 | 版本 | 固定提交 | 安装目录 |
|---|---|---|---|
| Windows-MCP | 0.8.8 | `6613da7…` | `vendor/Windows-MCP-0.8.8` |
| computer-use-mcp | 1.8.0 | `a415230…` | `vendor/computer-use-mcp-1.8.0` |
| xshell-mcp | 0.1.0 | `709a5f5…` | `vendor/xshell-mcp-0.1.0` |

**版本化布局**

源码解压到 `vendor/<名称>-<版本>`，目录内写 `.xm-agent-resource.json`，记录 id、版本、提交、来源、安装阶段与时间。稳定入口 `vendor/<名称>` 优先使用 Windows junction 指向版本目录，因此 `config/agent.example.toml` 中的路径保持不变。检测同时兼容历史手工 clone：若稳定目录本身版本匹配且入口完整，则视为「本地已有安装」；需要切换版本时，旧目录先移动到 `.xm-agent/resource-backup/`，不直接删除。

**下载与镜像**

归档地址由 `repo + 固定 SHA` 生成，依次尝试 GitHub 官方、ghproxy.net、gh-proxy.com、ghfast.top；镜像只做 URL 前缀代理，不改变内容。下载先落 `.part`，通过 `zipfile.is_zipfile` 校验后原子替换缓存 `.xm-agent/resources/<id>-<version>.zip`，构建失败重试时复用缓存。

**解压安全**

自定义解压器拒绝绝对路径、盘符、`..`、NUL 与符号链接条目，限制条目数（20000）与解压后总大小（512 MB），并把每个目标路径 `resolve()` 后校验仍位于临时目录内。GitHub 归档必须包含单一顶层目录。

**构建**

- Python 资源：在版本目录内用当前解释器创建 `.venv`，依次尝试 PyPI 官方、清华、阿里云、腾讯云执行 `pip install -e .`。
- Node 资源：依次尝试 npm 官方与 npmmirror 执行 `npm install`，再执行 `npm run build`；Windows 下通过 `cmd /d /c` 调用 `npm.cmd`。
- 构建成功后校验 `entry` 与 `node_modules` 等运行文件，最后把标记更新为 `stage=ready`。

**触发方式**

- `start.cmd` 在 GUI 前执行 `xm_agent resources ensure`；已就绪时只做本地检查。
- CLI：`resources status` / `resources ensure [--only ID] [--force]`。
- UI：`GET /api/resources` 返回检测结果与当前任务状态，`POST /api/resources/sync` 在后台线程检测并安装；设置页悬浮窗轮询进度，显示每项资源的版本、阶段与错误。POST 与其他写接口一样要求 `X-XM-Token`。

### 4.14 任务式界面、流式输出与前台独占交互

**结论**

- 任务式界面、流式思考/输出、会话持久化：可行。需要把 Provider 改为流式、把面板轮询改为 SSE，并新增消息与事件表。
- “每个会话一个独立桌面、内嵌显示、用户同时操作”：Windows 客户端做不到。虚拟桌面共享同一会话且只有当前桌面渲染与接收输入；隐藏 `CreateDesktop` 桌面不渲染；RDP 单客户端只允许一个交互会话。真正隔离只能上 Windows Sandbox / Hyper-V，且宿主应用与文件不共享，与“用户先打开某个程序”的需求冲突。
- 因此采用**前台独占 + 单任务串行**：AI 运行期间占用当前桌面，用户不应同时操作；用户一旦操作即暂停或接管。不做多任务并行、不做 Windows 多鼠标、不做多桌面。
- F8 暂停、顶部提示、边缘发光、右上 HUD、暂停菜单、AI 注入、截图提问、串行队列与插队：均可实现，细节与限制见下。

#### 4.14.1 任务式界面

导航调整为「总览 / 任务 / AI 模型 / 用量 / 设置」，「历史记录」并入任务页，不再单独占一级入口。

任务页三段布局：

| 区域 | 内容 |
|---|---|
| 左侧 | 全局导航；底部显示当前活动模型与连接状态 |
| 中间 | 当前会话的对话流与输入框；顶部显示任务标题、后端（mock / Windows-MCP / Computer-Use）、状态与步数 |
| 右侧 | 全部任务列表：运行中、已暂停、排队中、已完成、失败、已取消；支持搜索、新建任务、切换会话；队列项显示排序与「插队」 |

对话流由以下消息类型组成，均落库并可回放：

- 用户目标与中途指令。
- `thinking`：模型思考，默认折叠，点击标题展开/收起；流式追加，折叠时只显示一行摘要与“思考中”动画。
- `action`：工具/动作卡片（能力名、参数摘要、回执、验证结果）；失败卡片标红并可展开原始错误。
- `assistant`：模型自然语言输出与最终答复。
- `system`：暂停、恢复、插队、结束操控、截图提问等系统事件。

输入框在一个任务运行中仍可发送：新目标进入队列；对当前任务的补充走「F8 暂停 → AI 注入」。任务列表项支持重命名、删除（仅元数据与本地事件，审计文件不删）与导出。

界面行为：流式期间自动滚动到底部，用户手动上滚后停止自动滚动并显示“回到底部”；Markdown 渲染必须转义原始 HTML；长任务虚拟滚动，只保留可视区节点。

#### 4.14.2 流式输出与持久化

**Provider 流式**

- `decide()` 增加 `on_delta(kind, text)` 回调，`kind ∈ {thinking, text}`；Runtime 原样转发为事件，不参与决策解析。
- OpenAI 兼容：请求 `stream: true`，按需带 `stream_options.include_usage`；解析 `delta.reasoning_content`（DeepSeek/GLM 等推理模型）与 `delta.content`；usage 从最后一个 chunk 或非流式回退中归一化。部分网关不支持流式参数时自动回退非流式一次。
- Anthropic Messages：请求 `stream: true`；解析 `content_block_start`、`thinking_delta`、`text_delta`、`message_delta.usage`。已确认 GLM-4.5 可能只返回 thinking 块，流式解析需同时兼容。
- 决策仍是“单个 JSON 对象”：流式只用于展示，结束时拼接完整文本再走现有 `parse_action_json`。解析器先尝试整体 JSON，再扫描字符串内平衡的首个 `{...}` 对象（容忍模型在 JSON 前后夹带解释、Markdown 围栏或思考文字）；仍失败时 Runtime 携带“只输出合法 JSON、缩短 rationale”的纠正指令重试，默认 2 次，401/403 等鉴权错误不重试。决策请求固定 `max_tokens=8192`，避免推理内容过长截断 JSON。
- 取消：暂停/插队/结束操控时关闭正在读取的 HTTP 响应，尽快抛出取消异常；已在执行的单个 MCP 动作不中途打断。
- 观察失败（MCP 子进程抖动、快照超时）重试一次，仍失败才结束任务；默认步数预算 30，避免中等长度任务在第 12 步被截断。

**事件通道**

新增 SSE：`GET /api/runs/{run_id}/events`（`text/event-stream`）。浏览器 EventSource 无法自定义请求头，因此采用一次性票据或回环地址 + token 查询参数，并保留 Host 校验；票据短时有效且绑定 run。

事件信封固定为 `{seq, ts, run_id, type, data}`，SSE `id` 使用 `seq`，支持 `Last-Event-ID` 断线续传。事件类型至少包括：

```text
run.started / run.finished / run.failed / run.cancelled
thinking.delta / message.delta
step.started / action / receipt / verification / recovery
pause.requested / paused / resume.requested / resumed
queue.updated / task.preempted / task.jump_started
screenshot.ask.started / screenshot.ask.answer
error
```

内存事件环形缓冲保留最近 N 条用于快速回放，完整事件写 SQLite；不再依赖进程内 300 条上限。心跳每 15 秒一次，慢客户端丢弃增量但保留关键状态事件。

**持久化表**

| 表 | 关键字段 |
|---|---|
| `conversations` | `id, title, adapter, status, created_at, updated_at` |
| `messages` | `id, conversation_id, role, kind, content, meta_json, created_at` |
| `run_events` | `run_id, seq, type, data_json, created_at` |
| `task_queue` | `id, conversation_id, position, status, created_at` |

保留现有 `runs` 表作为任务元数据索引；审计 JSONL 仍是步骤与动作的权威记录，SQLite 事件用于界面回放。截图只存路径与摘要。

#### 4.14.3 前台独占模型

AI 运行期间独占当前桌面，这是本版本明确的产品约束，而不是安全隔离：

- 同一时刻只允许一个 `RUNNING` 任务；其他任务只能是 `QUEUED` 或 `PAUSED`。
- 任务开始时，控制台窗口最小化或隐藏，把桌面让给目标应用；暂停、结束操控或任务完成后恢复窗口。用户仍可从托盘或任务栏重新打开控制台，但打开窗口会抢占焦点。
- 用户中途移动鼠标、点击或输入会改变 AI 正在依赖的界面状态。Windows-MCP 的 ControlCoordinator 会在检测到人类优先控制时返回 `USER_CONTROL` 并等待；Computer-Use 没有该保护。因此推荐策略是**检测到非注入输入即自动暂停**，至少要做到 F8 手动暂停 + 状态提示。
- 不在后台继续运行：暂停后不执行动作、不继续模型决策；恢复前必须重新观测，旧元素 ID 一律作废。
- 不使用 Windows 虚拟桌面、隐藏桌面或第二个 RDP 会话伪装隔离，避免“看似隔离、实际共享输入与焦点”的误判。

#### 4.14.4 F8 暂停、顶部提示与边缘发光

**F8 全局暂停**

- Runtime 进程安装 `WH_KEYBOARD_LL` 低级键盘钩子，仅在存在 `RUNNING` 任务时处理 F8；用 `LLKHF_INJECTED` 过滤 AI 自己注入的按键，避免自我触发。
- F8 在运行中请求暂停，在暂停中请求恢复；暂停请求不阻塞 UI，Runtime 在下一个步骤边界生效。
- 快捷键可配置，默认 F8；注册失败时降级为 HUD/托盘按钮，并在设置页提示。
- UAC 提权窗口或更高完整性级别的前台窗口可能收不到钩子事件；此时顶部提示仍显示，但 F8 可能无效，需要提供可点击的暂停入口（托盘菜单或控制台）。彻底解决需要可选提权辅助进程，列入后期。
- F8 与目标应用自身快捷键可能冲突；默认只在 Agent 运行/暂停态拦截，其他时间放行。暂停菜单的注入输入框打开时，F8 交由对话框处理，避免误恢复。

**顶部提示**

- 运行与暂停期间在屏幕顶部居中显示常驻提示：运行中为「AI 正在操作电脑 · F8 暂停」，暂停中为「已暂停 · F8 继续」。
- 窗口属性：置顶、无边框、`WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`，不抢焦点、不接收鼠标。
- 多显示器下默认显示在主屏顶部；可配置为跟随活动显示器。

**边缘发光**

- 运行期间在显示器四周绘制呼吸式发光边框，暂停时改为静态黄色，结束后消失。
- 与顶部提示相同：分层、点击穿透、不激活。多显示器按虚拟屏幕矩形铺满，或只描边活动显示器（可配置）。
- 发光层只做视觉提示，不参与点击命中测试，也不改变窗口 Z 序之外的任何状态。

**截图排除（必须实测）**

- 顶部提示、发光层、HUD 都调用 `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`，让用户可见、截图不可见。Windows 11 26100 支持该 API，但**必须分别验证 Windows-MCP 的截图后端与 Computer-Use 的 nut.js 截图后端是否遵守**。
- 验收方式：运行中截图，检查顶部提示与 HUD 区域像素不含其主题色，且目标窗口内容完整；同时验证发光层不会在截图边缘留下色带。
- 若某个后端使用 BitBlt/DXGI 而不遵守 WDA，回退方案依次为：截图前临时隐藏覆盖层（会有闪烁，仅作兜底）、把 HUD 移到不参与截图的区域、对截图做覆盖层区域裁剪/修复。不得假设 WDA 一定生效。

#### 4.14.5 右上角 HUD

HUD 是一个独立的置顶浮窗，显示当前任务的实时思考、动作与队列摘要：

- 内容：最近若干条 thinking/动作卡片、当前步骤号、耗时、模型与队列数量；默认折叠为窄条，可展开为可滚动面板。
- 滚动：鼠标悬停时用低级鼠标钩子接收滚轮事件并在 HUD 内部滚动，不把滚轮传给下层应用；不依赖窗口获得焦点。
- 缩放：拖动边缘调整宽高，位置与尺寸按显示器记忆；展开/折叠状态持久化。
- 交互与点击穿透的冲突：运行中 HUD 默认点击穿透，避免挡住 AI 的点击坐标；宿主低级鼠标钩子检测到光标位于 HUD 内并发生点击/拖动时，视为“用户接管”，立即请求暂停，暂停后 HUD 切换为可交互（可选择文本、滚动、缩放）。恢复运行后回到点击穿透。这样既满足“随时查看”，也不会让 HUD 吃掉 AI 的点击。
- HUD 同样使用 `WDA_EXCLUDEFROMCAPTURE`；若排除失效，按 4.14.4 的回退方案处理。
- 默认位置在主屏右上角；若目标应用在该区域有密集控件，用户可拖动或切换到副屏。HUD 内不显示任何 API Key、完整请求头或未脱敏路径。

#### 4.14.6 暂停菜单与 AI 注入

F8 暂停后显示暂停面板，提供四个入口：

| 入口 | 行为 |
|---|---|
| 继续 | 重新观测 → 作废旧元素 ID → 恢复步骤循环；也可再按 F8 |
| AI 注入 | 打开指令输入框，发送后把指令作为新的用户消息，模型据此调整下一步；发送前保持暂停 |
| 截图提问 | 截取当前屏幕并向视觉模型提问（见 4.14.7） |
| 结束操控 | 取消任务、释放租约、恢复控制台窗口、写入审计；默认保留 AI 打开的应用 |

**AI 注入规则**

- 注入内容作为 `role=user` 的中途消息写入会话，并与任务目标一起进入下一轮决策；不做关键词匹配，不绕过策略与确认门。
- 发送后先重新观测，再让模型决策；注入前正在等待的模型请求取消，避免基于旧画面继续。
- 输入框使用普通文本框，空格、中文输入法候选、回车均按正常输入处理；发送用「发送」按钮或 `Ctrl+Enter`，`Enter` 只换行。暂停面板打开时 F8 不触发恢复。
- 注入内容、发送时间与恢复后的首次观测写入审计；同一任务可多次注入，保留完整顺序。
- 若注入后模型判断目标已不可达，按正常失败路径结束，不自动回滚用户已做的操作。

**结束操控的现场策略（v1）**

- 默认保留 AI 打开的应用与窗口，只释放控制权；暂停面板列出本任务新建/激活的窗口，用户可手动关闭。
- 「自动关闭本次打开的应用」与「最小化本次窗口」列入后期，避免误关用户原有窗口。
- 结束操控必须释放租约锁；若释放失败，记录错误并在界面提示，禁止静默继续。

#### 4.14.7 截图提问（当前版本）与框选（后期）

当前版本在暂停面板提供「截图提问」：暂停中打开输入框，向执行模型提一个问题，答案只显示在面板里（实现细节见 4.20）。

1. 暂停生效后才允许提问；运行中截图会拍到变化中的画面，接口直接拒绝。
2. 用宿主截图（Pillow，`[runtime].screenshot_dir`，`ask-` 前缀）全屏抓取；截图前临时收起控制台，覆盖层已被 `WDA_EXCLUDEFROMCAPTURE` 排除。
3. 只有「仅提问」一种动作：回答作为旁路信息展示，不落库、不进入任务上下文，也不改变任务方向。
4. 需要执行模型被判定支持图像输入。纯文本模型时按钮禁用并提示切换模型；本版本不做本地 OCR。
5. 截图按现有保留策略清理（`xm-agent cleanup` / 启动时自动执行）。

后期再做：

- 缩略图预览、框选区域（多显示器负坐标与 DPI 缩放）；框选在暂停态显示全屏选择层，拖拽后裁剪走同一问答管线。
- 「作为指令发送」：把回答摘要与用户问题作为中途指令注入（复用 4.14.6），恢复前重新观测。
- 优先复用后端截图能力（Windows-MCP / Computer-Use），后端不可用时再用宿主截图兜底。
- 截图与问答写入会话与审计，便于回放；需要在写入前脱敏。

#### 4.14.8 串行队列、插队与恢复

- 运行中发送新目标 → 创建会话并入队，状态 `QUEUED`；右侧任务列表与 HUD 显示队列数量。
- 当前任务结束（完成/失败/取消）后，调度器自动启动队首；同一时刻只有一个任务持有租约锁。
- 「插队」：在当前步骤边界暂停当前任务，取消正在等待的模型请求，标记 `PREEMPTED`，立即启动所选排队任务。插队任务结束后默认自动恢复被插队任务（可配置为手动确认）。
- 恢复被插队任务前必须重新观测；若目标窗口已不存在或界面已被改变，按失败分类结束，不盲目重放旧动作。
- 队列持久化到 `task_queue`；进程重启后 `QUEUED` 保留，`RUNNING` 标记为 `INTERRUPTED` 并要求用户确认是否继续，避免静默重放。
- 不支持并行任务、任务分屏或后台执行；排队只是串行等待，不是并发。

#### 4.14.9 窗口归属与干扰控制

- 通过 Windows-MCP Snapshot 的窗口列表（`handle`、`process_id`、标题）在任务前后做差集，记录本任务新建或激活的窗口。
- 排除 Agent 自身进程窗口（控制台、HUD、提示、发光层），避免模型点击到自己的 UI。
- 暂停/插队时列出这些窗口；默认不自动最小化，避免改变应用状态。用户可选择收起，恢复时再还原。
- 被插队任务留下的窗口可能干扰插队任务；插队任务开始前重新观测，模型应优先使用现有窗口或明确新建。无法区分时按失败处理并提示用户。

#### 4.14.10 分阶段实施

| 阶段 | 内容 | 出口标准 |
|---|---|---|
| A | 任务页三段布局、会话/消息/事件表、Provider 流式、SSE、思考折叠、动作卡片、回放 | mock 下完整跑通流式对话与重启回放 |
| B | 前台独占、F8、顶部提示、边缘发光、HUD、WDA 截图排除、暂停菜单、AI 注入、截图提问（全屏） | Windows-MCP 与 Computer-Use 均完成“运行 → F8 暂停 → 注入 → 恢复 → 结束”，截图不含覆盖层 |
| C | 串行队列、插队、恢复、窗口归属跟踪、结束操控现场策略 | 排队自动衔接、插队抢占与恢复可重复验证，无并发执行 |
| D | 框选截图、运行中 HUD 悬停滚轮/缩放、用户输入自动暂停、多显示器与 DPI 打磨 | 主屏/副屏、100%/125%/150% 下坐标与覆盖层一致 |

阶段 A 不改变执行语义，可先于 F8/覆盖层落地；B 依赖 A 的流式与事件通道；C 依赖 B 的暂停/恢复；D 为体验增强。

#### 4.14.11 验收标准

- 任务页可新建、切换、排队与回放；思考默认折叠，展开后内容完整；流式输出在首 token 后 1 秒内可见。
- F8 在动作边界 1 秒内生效；暂停期间无新动作、无新模型请求；恢复前重新观测，旧元素 ID 不复用。
- 暂停面板四个入口可用；AI 注入后模型能依据指令改变下一步，注入记录可在审计中查到。
- 顶部提示、发光层、HUD 在 Windows-MCP 与 Computer-Use 的截图中均不可见；若某后端不遵守 WDA，回退方案生效且有明确日志。
- 截图提问在视觉模型下返回有效回答；纯文本模型下按钮禁用并给出原因。
- 运行中发送新目标进入队列；当前任务结束后自动启动；插队先暂停、再执行、结束后恢复，全程只有一个任务在跑。
- 结束操控后租约锁释放、控制台窗口恢复、审计完整；AI 打开的应用默认保留。
- 重启后会话、消息、事件可回放；中断任务不自动重放。
- 多显示器与 125%/150% 缩放下，顶部提示、发光层、HUD 与截图坐标一致。
- 既有能力契约不变；mock、Windows-MCP、Computer-Use 共用同一套界面与调度逻辑。

#### 4.14.12 待确认

- 结束操控时 AI 打开的应用默认保留、最小化还是关闭；是否需要“本次会话窗口”清单与一键处理。
- HUD 固定在主屏还是跟随活动显示器；副屏场景下的默认位置。
- F8 与目标应用快捷键冲突时的替代键；是否提供可选的提权辅助进程以覆盖 UAC 窗口。
- 用户非注入输入是否在 v1 就自动暂停；Windows-MCP 的 `USER_CONTROL` 与宿主低级钩子如何合并判定，避免误暂停。
- 插队任务结束后自动恢复被插队任务是否作为默认；失败恢复与人工确认的边界。
- 暂停是否设置最长时长（例如 30 分钟自动结束），以及超时后的现场保留策略。
- WDA 在两个截图后端的实测结果；若均不遵守，是否接受“截图前隐藏”的闪烁兜底。

### 4.15 顶层 AI 审计与用户审计模式

**定位**

顶层审计是风险过滤层，不是安全边界。确定性策略（能力白名单、路径根、命令白名单、受保护路径）永远第一优先；顶层 AI 只能收紧，不能放宽。底层执行模型只能**提议**操作，不能自己批准，否则只要不声明敏感即可绕过审计。

判定链：

```text
底层 AI 提议
  → 运行时规范化操作 + 规则检测
  → 确定性策略（deny / confirm / allow）
  → 顶层 AI 审计（敏感操作）
  → 用户确认（按模式与风险）
  → 执行前哈希复核
  → 执行
```

**角色与独立性**

- 顶层审计模型单独配置（建议与执行模型不同厂商/模型）；无工具调用、无 MCP 连接、无执行能力，只返回 JSON。
- 审计请求不包含任务目标、底层 AI 的 rationale、完整文件内容或密钥；只包含规范化操作、环境画像与目标元数据。
- 审计模型不可用、超时或输出无法解析时失败关闭：敏感操作升级为用户确认或拒绝，绝不自动放行。
- 顶层 AI 与执行模型可以是同一模型的不同无状态调用，但独立性较弱，界面需标注“同一模型”。

**操作描述符**

运行时从实际参数生成，不直接采用底层 AI 的自然语言描述；底层 AI 可以额外提交 `audit_note`，但只作为不可信备注，且与参数不一致时拒绝或重审。

```json
{
  "operation_id": "sha256:...",
  "capability": "terminal.exec",
  "action": "terminal",
  "args": {"command": "...", "cwd": "..."},
  "targets": [
    {"path": "D:\\project\\build", "exists": true, "kind": "dir",
     "inside_workspace": true, "inside_project": true, "protected": false,
     "size_bytes": 1823412, "git_repo": true}
  ],
  "effects": ["delete", "recursive"],
  "reversible": false,
  "risk_hint": "high",
  "audit_note": "..."
}
```

环境画像包含：操作系统版本、当前用户、工作区/项目根列表、受保护路径、是否管理员、活动窗口/进程、网络目标主机（如适用）。不包含 API Key、环境变量值、文件全文。

**敏感操作识别**

以下任一命中即进入审计，底层 AI 声明与规则检测取并集：

- 文件删除、覆盖、移动、重命名、批量修改；写工作区外路径；跨项目路径。
- 终端执行未知命令、脚本、下载后执行、提权、服务/注册表/启动项/计划任务/权限/防火墙/安全软件变更。
- 文件上传、`git push`、远程 API/数据库写入、外部消息发送。
- 凭据、密钥、浏览器数据、数据库、用户文档等受保护路径。
- 桌面 UI 中可识别的破坏性动作（窗口标题/元素名包含删除、卸载、格式化、清空回收站等）。
- 只读操作（observe / verify / wait / 工作区内普通查看）默认不审计；凭据路径、用户数据目录（文档/下载/桌面/图片等）与其他项目目录的读取仍需确认。
- 用户决定不可被审计模型绕过：只要操作涉及其他项目/工作区或用户数据，即使顶层 AI 返回 `allow`，也强制升级为用户确认。

**审计输出**

```json
{
  "verdict": "allow | ask_user | deny",
  "risk": "low | medium | high | critical",
  "categories": ["data_loss", "system_change", "privacy", "user_data", "external_effect", "cross_project"],
  "scope": {"inside_project": true, "cross_project": false, "user_data": false, "paths": ["..."]},
  "reversible": false,
  "purpose": "推断目的，仅供用户阅读",
  "impact": "可能影响",
  "steps": ["操作流程"],
  "safer_alternative": "可选"
}
```

映射规则：

| 结果 | 行为 |
|---|---|
| `allow` | 策略允许时执行；若规则检测到其他项目/用户数据，强制升级为 `ask_user` |
| `ask_user` | 展示目的、流程、影响、风险与可逆性，由用户决定 |
| `deny` | 本步不执行，返回“被审计阻止”的回执，模型可改用更安全的方案 |

**用户审计模式**

| 模式 | 行为 |
|---|---|
| 执行前询问 | 所有新增、修改、删除、上传、命令执行等写操作都询问；普通查看不询问 |
| AI 审计（默认） | 低风险写操作自动执行；敏感操作交顶层 AI；顶层 AI 判 `ask_user` 或高风险时弹窗 |

- 不提供“关闭审计”或“完全访问”开关；系统能力、路径与命令白名单始终生效。
- 审计模型未配置时，AI 审计自动降级为执行前询问，而不是放行。
- 用户只能批准当前步骤，不能下发永久全量授权；相同操作可缓存审计结果，但执行前仍需复核操作哈希。
- “自动确认”与 CLI `--yes` 只能跳过普通确认；涉及其他项目/用户数据的强制用户决定不会被自动批准。
- 系统目录、磁盘/分区、引导配置、安全软件、凭据存储等进入硬拦截清单，默认任何模式都不能执行；高级覆盖需显式配置并二次确认。

**执行前复核（TOCTOU）**

- 审计/用户批准后计算规范化操作哈希；执行前再次解析路径与参数，哈希变化则重新审计。
- 路径使用 `realpath` 校验，拒绝符号链接/junction 逃逸出允许根。
- 命令中的通配符、变量与环境展开结果若无法确定，升级为用户确认，不按原样自动执行。
- 桌面元素 ID 在暂停/恢复/插队后一律作废，必须重新观测。

**数据与审计记录**

- 新增 `audit_decisions` 表与 JSONL 记录：操作哈希、描述符、环境画像哈希、审计模型、耗时、Token、verdict、用户决定、执行结果。
- 敏感值按现有脱敏规则处理；不记录 API Key、完整凭据或未脱敏的文件内容。
- 审计调用单独计入 Token 用量（`source=audit`），便于观察成本。

**界面**

- 任务页输入区提供审计模式选择（AI 审计 / 执行前询问），写入会话配置。
- 任务标题右侧的「审计模型」下拉只列出可识图模型；未配置可识图模型时提示回退风险操作将强制由用户确认。
- 聊天流插入 `audit` 卡片：风险等级、类别、目的、影响、可逆性、顶层 AI 结论；回退风险操作额外显示「已识图审查 / 未识图」标记；折叠展示原始描述符。
- 用户确认弹窗展示操作原文、目的、流程、影响范围与“允许一次 / 拒绝”；高风险操作使用明显警示色。
- HUD 显示“审计中”“等待确认”状态；确认请求超时默认拒绝。

**配置**

```toml
[audit_ai]
mode = "ai"                 # ai | confirm_all
model_provider_id = ""      # 留空时跟随可识图的执行模型/活动模型；审计模型必须支持图像输入
model_id = ""
timeout_seconds = 12
fail_closed = true
protected_paths = ["C:/Windows", "C:/Program Files", "C:/Users/*/.ssh"]
always_confirm = ["file.transfer", "terminal.exec"]
```

**验收标准**

- 删除、覆盖、跨项目写入、上传、`git push`、卸载/清空类 UI 操作均触发审计或用户确认。
- 底层 AI 不声明敏感时，规则检测仍能拦截；声明与参数不一致时拒绝。
- 顶层 AI 超时、返回非法 JSON、模型不可用时，敏感操作不会自动执行。
- 用户拒绝后本步不执行，任务可继续提出替代方案；任务级结束记录原因。
- 审计模型与执行模型分离时，审计请求不含任务目标与执行模型 rationale。
- 批准后操作被修改（路径/命令/参数）会重新审计，不能复用旧批准。
- 只读工作区文件不弹窗；读取 `.ssh`、`.env`、用户文档目录或其他项目目录时仍弹出用户确认。
- 顶层 AI 对涉及其他项目/用户数据的操作返回 `allow` 时，系统仍必须要求用户决定。
- 回退风险 UI 操作必须由可识图的顶层审计模型对照截图审查；审计模型或截图不可用时强制用户确认，不自动执行。
- 截图未变化时复用 `screen_card`，不重复上送图片；批量计划的中间步骤只校验回执，最后统一验证。
- 审计记录可回放，包含模型、耗时、Token 与用户决定。

**已知限制**

- 顶层 AI 也是概率模型，可能误判或被提示注入；确定性硬拦截不可省略。
- UI 点击的语义很难可靠识别；Computer-Use 只有坐标，破坏性 UI 操作只能按窗口/元素名与用户确认兜底。回退风险审查依赖识图模型，截图本身也可能包含屏幕上的隐私内容。
- 视觉提速依赖模型配合批量计划与 `screen_card`；模型不返回摘要时，截图缓存只在相同哈希下复用元素树，省图效果有限。
- Shell 命令的别名、编码、脚本、通配符与下载执行无法完全静态分析，无法解析时升级用户。
- 审计增加一次模型调用的延迟与费用；相同操作哈希可缓存，但环境变化后必须失效。
- 操作路径与命令会发送给审计模型，属于额外数据暴露面；支持本地 Ollama 作为审计模型。
- 读操作也可能泄露隐私；“查看不询问”只适用于工作区内普通文件。

---

### 4.16 视觉提速与回退风险识图审查

视觉模型每一步都要重新截图、重新决策时，延迟与 Token 主要消耗在「重复观察同一屏幕」和「传输大图」上。运行时按下面四条组合优化，语义不变：

**批量计划**

- 模型可以在主动作之外返回 `steps`（最多 3 个后续动作，总计执行上限 `MAX_PLAN_ACTIONS = 4`），运行时会先执行完整批次的确定性动作，再统一验证。
- 计划只在决策时观测一次；中间动作只检查执行回执，标记 `deferred=true`，最后一步才调用 `adapter.verify`。
- 批次的 `task.finish` / `desktop.observe` 是终止动作，其后的步骤一律丢弃；任何一步回执失败或审计拒绝都会清空剩余批次并重新观测、重新决策。
- `max_steps` 计数的是实际动作数，而不是模型决策次数，避免批量执行被误判为超步数。
- 任务流出现 `plan` 事件（一次规划 N 步），步骤卡片用「待核对」标记延迟验证。

**截屏压缩**

- 截图默认缩到 1280 宽，JPEG q80 与 PNG 取体积更小者；`vision.scale_point` 按原始尺寸/发送尺寸比例还原模型给出的坐标。
- Pillow 是可选依赖（`.[gui]` 或 `.[vision]`）：未安装时保留原始截图字节，调用方无需改代码；坐标缩放系数退化为 1.0。

**屏幕摘要缓存**

- 观测返回的截图哈希与模型输出的 `screen_card`（当前屏幕的一句话摘要）一起缓存。
- 下一次观测哈希未变化时，不再重复发送图片，只发送缓存的 `screen_card`（`screenshot_cached=true`，任务流出现 `screen_cache` 事件）；哈希变化才重新识图。
- UIA 优先：元素树能表达状态时不依赖截图；纯视觉后端才在每步观测时截图。

**回退风险识图审查**

- 部分操作做错后难以撤销（提交/保存/确认/删除/卸载/覆盖、上传、`git push`、提权、系统设置，或模型显式声明 `rollback_risk=true`，以及高/严重敏感度），进入 `needs_rollback_review`。
- 其中 `desktop.act` 没有文本审计留痕，额外要求 `needs_visual_review`：顶层审计模型必须收到当前屏幕截图，对照“点击/输入可能造成的结果”判断是否可回退。
- 审计模型与截图必须同时可用；任一缺失都失败关闭，强制用户确认，绝不根据文字参数猜测屏幕状态。识图审查结果不进入审计缓存，每次重新看图。
- 顶层审计模型被强制限定为「可识图」模型：`/api/audit/model` 拒绝纯文本模型；未显式选择时依次回退到可识图的执行模型、可识图的活动模型、第一个已配置的可识图模型。界面中的审计模型下拉只列出 `vision=true` 的模型，并在任务标题下方提示识图审查用途。

### 4.17 登录阻塞与用户接管

模型遇到登录页时容易反复尝试「不登录也能看」的路径，并在无凭据时继续消耗步数。运行时应把这类情况当作必须由用户处理的中断点，而不是让模型自由发挥：

**终止能力**

- 新增终止能力 `task.await_user`，参数为 `kind`（`login` / `input`）、`prompt`、`summary`。模型无法继续、且需要用户完成登录或其他人工步骤时调用；它与 `task.finish` 一样不经过适配器执行。
- 运行结果状态为 `needs_human`，`TaskResult.await_user` 携带 `{kind, prompt, resume_hint}`，并随 `result` 事件、审计日志与会话消息持久化。

**提示词约束**

- 登录页且目标或用户指令中没有账号密码时，禁止绕过登录、猜密码、注册、找回密码或寻找其他入口，必须调用 `task.await_user`。
- 用户提供了凭据时正常输入，禁止编造；用户表示已登录并要求继续时，从当前屏幕继续原目标，不从头开始。

**确定性兜底**

- UIA 观测的窗口标题/元素名同时出现登录词（登录、登陆、sign in、login）与密码/账号控件，或出现「请先登录 / 扫码登录」时，视为登录页。
- 若目标本身没有提到登录/注册/密码/账号，且目标、任务上下文与用户注入指令中都没有 `密码：xxx` 形式的凭据，运行时在模型给出普通动作后强制改写为 `task.await_user(kind=login)`，不再执行该动作；模型主动调用或页面离开登录页后恢复正常。
- 视觉后端没有元素树时依赖提示词与 `screen_card` 判断；一旦卡片的摘要同时包含登录与密码/账号信息，也会触发同一兜底。

**用户继续**

- 界面在结果卡显示「任务已暂停 / 等待你登录」，并给出常驻提示条与「我已登录，继续」按钮；输入框占位符改为引导输入「继续」。
- 用户在同一会话发送「继续」（或点击按钮）时，服务端读取上一条 `needs_human` 结果的 `await_user`，用被阻塞任务的原目标重新启动一次运行，把用户这次输入作为会话消息保存，并在上下文里追加「用户已完成暂停事项（登录），请从当前屏幕继续原任务」。
- 恢复任务与普通任务共用队列、模型与审计设置；队列记录同时持久化用户消息与上下文备注。没有待处理事项时发送 `resume=true` 会被拒绝，避免把「继续」误当成新目标。

### 4.18 数据保留与清理

截图与审计日志是排查问题的证据，但不该无限增长：

**保留窗口**

- `[audit].keep_days`（默认 7，最小 1）是唯一保留窗口，同时约束审计 JSONL 与截图缓存。
- `[runtime].screenshot_dir`（默认 `.xm-agent/screenshots`）同时供适配器落盘与清理读取，避免两处路径漂移。
- 审计 JSONL 与截图目录都属于应用自有数据；用户文档、下载目录等不在清理范围内。

**清理边界**

- 审计目录只处理 `*.jsonl`；截图目录只处理 `.png` / `.jpg` / `.jpeg` / `.webp` / `.bmp`。
- 不递归子目录、不删除目录本身、目录缺失时不创建，避免清理动作产生副作用。
- `cleanup()` 返回 `removed` / `freed_bytes` / `kept`，`--dry-run` 走同一判定但跳过 `unlink`。
- `xm-agent cleanup` 可手动执行；`run` 与 `gui` 启动时各执行一次，异常被吞掉，不影响主流程。
- 没有常驻定时任务：长时间运行的 GUI 进程需要用户手动清理（见已知限制）。

**用量口径**

- `ai_usage.source` 区分 `run`、`audit`、`test`、`config`；`/api/ai/usage` 增加 `by_source` 聚合。
- 用量页单独显示「审计 Token」卡片与占总量比例，审计开销不再混入任务用量而无法归因。
- 总 Token、输入/输出、今日用量与分模型/分提供商统计口径不变，仍只统计 `total_tokens > 0` 的成功调用。

### 4.19 前台独占：控制台让出桌面

控制台窗口正好压在 Agent 要点击的桌面上，所以「前台独占」不只是串行，还要让窗口自己让开：

**让出与恢复**

- `DesktopHandoff`（`ui/desktop.py`）持有 pywebview 窗口，`release()` 最小化、`reclaim()` 恢复，二者幂等，窗口异常只记日志不外抛。
- 需要屏幕的后端（Windows-MCP、Computer-Use、auto 选中它们）在 `_execute_run` 建立适配器后调用 `_release_desktop(run_id)`；`finally` 中调用 `_reclaim_desktop(run_id)`，覆盖成功、失败、取消与等待接管。
- 以 run_id 集合计数：并发的屏幕任务全部结束才恢复，避免其中一个收尾就把窗口抢回来盖住另一个。

**交互例外**

- 暂停（F8 / 面板 / 排队插队前的用户暂停）会先恢复窗口——用户需要看暂停菜单与任务状态；`resume_run` 后再次让出。
- 用户确认弹窗（`request_confirmation`）同样恢复窗口，避免在最小化状态下等到 300 秒超时被拒；批准或拒绝后若任务仍在跑且未暂停，再次让出。
- 等待用户接管（`task.await_user`）不特殊处理：任务已结束，`finally` 已恢复窗口，用户直接在控制台回复「继续」。

**降级**

- `[runtime].release_desktop`（默认 `true`）可整体关闭。
- 浏览器模式（`--server-only`）没有窗口对象，所有调用返回 `False`；bootstrap 的 `desktop_handoff` 供界面判断是否可用。
- 窗口在最小化期间仍接收 SSE 并累积事件，恢复后完整回放；代价是运行中看不到实时步骤（见已知限制）。

### 4.20 截图提问

暂停时用户常想先问一句「这个弹窗在说什么」「该选哪个」。这不该让执行模型顺手猜，而是一次独立的视觉问答：

**触发约束**

- 只有暂停中可以提问：`ask_screen` 校验 run 存在、`RunControl` 存在且 `paused`；运行中截图会拍到正在变化的画面，直接拒绝。
- 使用本次任务选定的模型（`model_provider_id` / `model_id`）；`provider_vision` / `vision` 为假时返回「当前模型不支持图像输入」，前端同时禁用按钮，避免模型在无图情况下编造。
- 问题最多 300 字，单次 800 tokens；提示词要求只依据截图作答、看不清就说看不清，2–4 句，不输出 JSON。

**取图**

- `capture_primary_screen` 全屏截图（Pillow，落到 `[runtime].screenshot_dir`，前缀 `ask-`），失败返回「截图失败」而不是让模型凭空回答。
- 截图前用 `DesktopHandoff.release()` 临时收起控制台，等待 350ms 让合成器完成重绘，拍完立即 `reclaim()`；否则窗口正好盖住要问的内容。
- 覆盖层本身已被 `WDA_EXCLUDEFROMCAPTURE` 排除，不会出现在截图里。

**边界**

- 只看不动：接口不生成 Action、不写审计、不写入 `messages` 或任务上下文，答案只渲染在暂停面板。
- 答案仅供用户参考，不会自动变成下一条指令；把答案转成指令的做法留到后续。
- 纯文本模型宁可禁用也不降级为「无图问答」，否则答案会变成幻觉。

---

## 5. 桌面端形态与分发（后期）

### 5.1 结论

可以。当前架构天然适合做成桌面端：Agent Runtime 是本地进程，MCP Server 是本地子进程或远程端点，模型走云端 API。后期只需增加桌面壳、安装器和更新器，不需要重写核心逻辑。

需要区分两个进程：

- 桌面壳（UI）：窗口、托盘、设置、任务历史、人工确认弹窗。
- Agent Runtime（本地服务）：状态机、能力适配、MCP 进程管理、审计与锁。

不要把 Agent 逻辑塞进 UI 进程。UI 崩溃不应中断任务，Runtime 也应支持无界面运行，便于后续做 CLI、远程触发和企业部署。

### 5.2 推荐形态

| 方案 | 组成 | 适用场景 |
|---|---|---|
| Tauri 2 + Web 前端 + Python sidecar | Rust 外壳、Web UI、PyInstaller 打包的 Runtime | 推荐；安装包小，权限边界清晰 |
| Electron + Python sidecar | Node 外壳、Web UI、PyInstaller Runtime | 团队以 JS 为主时 |
| pywebview + Python | 单 Python 进程 + 本地 Web UI | MVP 最快，但 UI 能力有限 |
| .NET WPF + Python sidecar | 原生 Windows UI | 企业内网、需要深度系统集成时 |

当前 MVP 采用 **pywebview + WebView2**：`start.cmd` 启动 Python 进程，先拉起只监听 `127.0.0.1` 的本地 HTTP API，再打开桌面窗口。页面只依赖该 HTTP API，因此后期换成 Tauri 外壳时不需要改 Runtime 或页面逻辑；SQLite 保存用户偏好、任务元数据、AI 模型配置与 Token 用量，API Key 仅以 DPAPI 密文形式入库，接口不回显密文。

运行方式：

- Runtime 以托盘应用或用户级启动项运行，保持在交互式会话中。
- 不要用 Windows 服务承载桌面控制：服务运行在 Session 0，无法访问用户桌面，UIA 和输入注入都会失败。
- 本地通信用 `127.0.0.1` HTTP / WebSocket，每次启动生成随机端口和临时 token；只监听回环地址。
- 单实例互斥锁 + lease lock，防止重复启动导致焦点争抢。

### 5.3 安装包内容

| 组件 | 打包方式 | 说明 |
|---|---|---|
| 桌面壳 | Tauri NSIS / MSI | 优先 per-user 安装，普通用户即可完成 |
| Agent Runtime | PyInstaller / Nuitka onedir | 不建议 onefile；启动慢且易被杀软误报 |
| Python 运行时 | 随包内置 | 不依赖用户机器上的 Python |
| MCP Server | 预置 wheel / venv，或首次运行安装 | Node 类 Server 需内置 Node 或随包提供 |
| 网关 | Go 单二进制 | 直接嵌入，无需额外运行时 |
| 配置模板 | 默认策略、能力映射、工具描述 | 首次运行向导生成用户配置 |
| 更新器 | 签名清单 + 增量包（可选） | 壳与 Runtime 分开更新 |

预期用户流程：

```text
下载安装包 → 安装 → 托盘启动 → 首次向导（模型 API、权限、目录）
→ 打开主窗口 → 输入任务 → 执行并人工确认 → 查看历史与回执
```

### 5.4 Windows 平台限制

- Session 0 隔离：服务无法操作桌面，桌面控制必须运行在用户会话。
- UAC 提权：控制以管理员运行的目标程序需要同等权限；默认自动提权不可行，需设计提权辅助进程或提示用户以管理员启动。
- RDP 断开：会话锁屏后截图与输入会失效；要求本地控制台会话，或保持会话活跃。
- 杀毒与 EDR：输入注入、截图、自动化行为容易被拦截；需要代码签名、白名单说明和用户授权。
- SmartScreen：安装包必须签名；新证书需要积累信誉，必要时提供校验值和安装说明。
- 自启动：使用用户级启动项或计划任务，不使用系统服务。
- 凭据：API Key 经 Windows DPAPI 加密后存入 SQLite；SSH 密码、私钥优先使用 Windows Credential Manager，禁止明文落盘。
- 防火墙：只监听回环地址，不开放入站端口。

### 5.5 更新策略

壳与 Runtime 使用两条更新通道：

| 通道 | 内容 | 更新方式 |
|---|---|---|
| 桌面壳 | UI、托盘、安装器 | 签名安装包，用户确认后升级 |
| Runtime / MCP 层 | 能力适配、MCP Server、策略 | 签名清单 + 冒烟测试 + 灰度 + 回滚 |

能力契约版本化后，新增或替换同类 MCP 只需更新 Runtime / MCP 层，不必重新发桌面壳。控制面更新禁止静默自动升级，必须保留可回滚版本。

### 5.6 数据目录

- `%LOCALAPPDATA%\XmAgent\config`：用户配置与能力映射。
- `%LOCALAPPDATA%\XmAgent\logs`：运行日志与审计 JSONL。
- `%LOCALAPPDATA%\XmAgent\screenshots`：按 trace_id 归档，默认保留 7 天。
- `%LOCALAPPDATA%\XmAgent\cache`：可安全删除的缓存。
- `%LOCALAPPDATA%\XmAgent\app.db`：SQLite，用户偏好、任务历史、AI 提供商/模型与 Token 用量；开发期位于项目 `.xm-agent/app.db`。API Key 仅以 DPAPI 密文形式入库。
- `%PROGRAMDATA%\XmAgent\policy`：per-machine 安装时的共享策略。

### 5.7 分期

| 阶段 | 内容 | 出口标准 |
|---|---|---|
| D0 | CLI + 本地 Runtime | 不依赖 UI 可完成任务 |
| D1 | 托盘 + 主窗口 + 任务历史 + 确认弹窗 | 日常任务可在桌面端完成 |
| D2 | 签名安装包 + 首次向导 + 自启动 | 干净 Windows 虚拟机安装后 5 分钟内跑通首个任务 |
| D3 | 双通道更新 + 崩溃上报 + 企业分发 | 升级不丢配置，可回滚，可批量部署 |

### 5.8 验收标准

- 干净 Windows 虚拟机安装后，普通用户无需手动装 Python / Node。
- 卸载后无残留服务、启动项和锁文件。
- 升级保留配置、任务历史与凭据。
- 普通任务不需要管理员权限；仅目标程序提权时才提示。
- 安装包与更新包均已签名，安装过程无安全软件拦截。
- UI 崩溃或关闭后，Runtime 状态可恢复，任务不丢失。

## 6. 实施进度

详细清单、已知问题与运行命令见 [STATUS.md](STATUS.md)。本节只保留结论性状态。

### 6.1 已完成

| 模块 | 文件 | 状态 |
|---|---|---|
| 能力契约（8 项，v1.0，带输入/输出 schema） | `src/xm_agent/contracts.py` | 完成 |
| 数据模型（Observation / Action / Receipt / Verification / StepRecord / TaskResult） | `src/xm_agent/types.py` | 完成 |
| 运行时状态机（observe → decide → policy → act → verify → recover） | `src/xm_agent/runtime.py` | 完成 |
| 登录阻塞与用户接管（`task.await_user`、UIA 登录页确定性兜底、同会话「继续」沿用原目标） | `src/xm_agent/runtime.py`、`src/xm_agent/ui/server.py`、`src/xm_agent/storage.py`、`src/xm_agent/ui/static/` | 完成，运行时与面板链路测试覆盖 |
| MCP stdio 客户端（JSON-RPC 2.0、超时、stderr 尾部、线程安全关闭） | `src/xm_agent/mcp_stdio.py` | 完成 |
| 能力适配层（契约 → MCP 工具名/参数映射） | `src/xm_agent/adapters/mcp_tool.py` | 完成 |
| Windows-MCP 适配器（Snapshot 文本解析、坐标定位、WaitFor 条件） | `src/xm_agent/adapters/windows_mcp.py` | 完成，真实只读验证通过 |
| Computer-Use MCP 适配器（截图落盘、坐标动作、`clear`/`press_enter`、视觉验证标记延迟） | `src/xm_agent/adapters/computer_use_mcp.py` | 完成，`tools/list` 与截图探测通过；无视觉模型时拒绝启动 |
| 视觉完成核对（`task.finish` 前用新截图向视觉模型复核，无法核对即拒绝完成） | `src/xm_agent/runtime.py`、`src/xm_agent/providers/` | 完成，单元测试覆盖拒绝/通过/无判断器 |
| Xshell MCP 适配器（`xshell_exec`、超时换算、屏幕读取） | `src/xm_agent/adapters/xshell_mcp.py` | 框架保留，本机未安装 Xshell |
| Mock 执行器（无副作用，用于联调与测试） | `src/xm_agent/adapters/mock.py` | 完成 |
| 策略引擎（能力白名单、命令前缀、危险模式、路径根、需确认项） | `src/xm_agent/policy.py` | 完成 |
| 租约锁（TTL、陈旧接管、心跳、原子替换） | `src/xm_agent/lock.py` | 完成 |
| 审计日志（JSONL、trace_id、敏感字段脱敏、按天清理接口） | `src/xm_agent/audit.py` | 完成 |
| 顶层 AI 审计（操作描述符、敏感识别、盲审、失败关闭、TOCTOU、缓存） | `src/xm_agent/guard/` | 完成 |
| 运行控制（暂停/继续/取消/插队抢占/指令注入） | `src/xm_agent/control.py` | 完成 |
| Provider 流式输出（OpenAI `reasoning_content` / Anthropic thinking，usage 归一化） | `src/xm_agent/providers/` | 完成 |
| Provider（脚本化 / 启发式 mock / OpenAI 兼容 / Anthropic Messages，可选视觉消息与 usage 回调） | `src/xm_agent/providers/` | 完成（本地模拟端点验证，真实厂商待联调） |
| 厂商目录与套餐（18 个预设，协议/端点/模型列表风格） | `src/xm_agent/ai/catalog.py` | 完成 |
| DPAPI 密钥加密（固定 entropy，密文入库） | `src/xm_agent/ai/crypto.py` | 完成 |
| 官方模型列表（OpenAI / Anthropic / Gemini / Ollama）与视觉标记 | `src/xm_agent/ai/models.py` | 完成 |
| 活动模型装配、连通性测试、usage 入库 | `src/xm_agent/ai/factory.py`、`src/xm_agent/ai/service.py` | 完成 |
| 配置（TOML，多适配器，工具名与参数重命名，子进程 env/cwd，`model` 回退） | `src/xm_agent/config.py`、`config/agent.example.toml` | 完成 |
| CLI（`run` / `demo` / `gui` / `cleanup`，UTF-8 输出，交互确认，自动读取活动模型） | `src/xm_agent/cli.py` | 完成 |
| 装配层（adapter / provider / policy，活动 AI 优先，CLI 与 UI 共用） | `src/xm_agent/bootstrap.py` | 完成 |
| SQLite 用户资料、任务历史、AI 配置与用量、会话/消息、`run_events`、`task_queue`、`audit_decisions`（WAL） | `src/xm_agent/storage.py` | 完成 |
| 本地面板（回环 HTTP、随机 token、静态资源、SSE + 断线重放、会话 CRUD、串行队列与插队、确认代理、运行控制、AI 配置与用量 API） | `src/xm_agent/ui/server.py` | 完成 |
| 任务式前端（三段布局、流式思考/输出、折叠思考、审计卡片、确认弹窗、会话与队列列表） | `src/xm_agent/ui/static/` | 完成 |
| 任务级屏幕占用指示（点击穿透边缘高亮、呼吸动画、WDA 截图排除、按任务显示/隐藏） | `src/xm_agent/ui/overlay.py` | 完成，真实窗口与坐标验证通过 |
| 全局 F8 暂停/继续（`RegisterHotKey` 独立消息循环，窗口失焦仍生效） | `src/xm_agent/ui/hotkey.py` | 完成，注入按键验证回调触发 |
| 桌面窗口与启动器（pywebview + WebView2、`start.cmd`） | `src/xm_agent/ui/window.py`、`start.cmd` | 完成，启动前自动初始化资源 |
| 前台独占窗口接管（屏幕任务运行中最小化控制台，结束/暂停/确认时恢复，`[runtime].release_desktop`） | `src/xm_agent/ui/desktop.py`、`src/xm_agent/ui/server.py` | 完成，状态机与幂等测试覆盖；真机最小化需桌面会话确认 |
| 截图提问（暂停后全屏截图 + 可识图模型问答，纯文本模型禁用） | `src/xm_agent/ui/server.py`、`src/xm_agent/ui/static/` | 完成，问题/暂停/模型/截图失败分支测试覆盖 |
| 第三方 MCP 资源管理（版本清单、镜像下载、安全解压、venv/npm 构建、检测 UI） | `src/xm_agent/resources/`、`src/xm_agent/cli.py`、`src/xm_agent/ui/` | 完成 |
| 面板其余页面（总览 / AI 模型 / 用量 / 历史 / 设置，饼图与资源悬浮窗） | `src/xm_agent/ui/static/` | 完成 |
| 数据保留清理（审计 JSONL 与截图按天清理、`cleanup` CLI、启动调度） | `src/xm_agent/maintenance.py`、`src/xm_agent/cli.py` | 完成，dry-run 与保留期下限测试通过 |
| 只读探测脚本（`tools/list`、`Snapshot`、Computer-Use 截图、Xshell 状态） | `scripts/probe_windows_mcp.py`、`scripts/probe_computer_use_mcp.py`、`scripts/probe_xshell_mcp.py` | Windows 端到端只读已验；其余待构建/环境 |
| 测试（pytest，317 项） | `tests/` | 通过 |
| 无依赖假 MCP Server（覆盖 12 个工具，用于协议联调） | `examples/fake_mcp_server.py` | 完成 |

### 6.2 待完成

| 优先级 | 事项 | 出口标准 |
|---|---|---|
| ~~P0~~ | Computer-Use MCP 只读截图探测 | 已完成：`tools/list` 返回 `computer`，截图 1464×823 / 84 KB |
| P0 | Computer-Use 视觉闭环（需视觉模型） | “截图 → 决策 → 坐标动作 → 再截图 → 视觉复核”跑通，坐标与 DPI 一致；无视觉时明确拒绝而不是虚假完成 |
| P0 | Windows-MCP 主通道 + 真实模型联调 + 20 任务基线 | 成功率、平均步数、失败分类有数据；真实厂商 Key 打通模型列表与测试请求 |
| P0 | 截图、DPI 缩放、多显示器坐标归一化 | 坐标在主屏/副屏/125%/150% 下一致 |
| ~~P0~~ | 可复用确认接口（替代 CLI 文本确认） | 已完成：确认代理 + 面板弹窗；批准只对当前操作哈希有效 |
| ~~P0~~ | 契约测试集（同一批用例跑所有后端） | 已完成：`tests/test_contract.py` 参数化 mock / windows / computer_use / xshell，新增后端只需加一条构造 |
| ~~P0~~ | 任务式界面（三段布局、任务列表、历史合并） | 已完成：任务页含对话流、任务/队列侧栏与历史消息回放 |
| ~~P0~~ | 流式思考/输出 + SSE + 会话与事件持久化 | 已完成：首 token 可见、思考默认折叠、SSE 断线重放；流式增量不落库 |
| P0 | 前台独占、F8 暂停、顶部提示、边缘发光、右上 HUD | 串行单任务、全局 F8、顶部状态胶囊、任务级边缘高亮与运行中最小化控制台已完成（4.19）；右上 HUD 未实现 |
| P0 | WDA 截图排除实测与回退 | Windows-MCP 与 Computer-Use 截图均不含覆盖层；失效时有回退与日志 |
| P0 | 暂停菜单（继续 / AI 注入 / 截图提问 / 结束操控） | 继续/AI 注入/结束/截图提问已完成；框选提问列入 P1 |
| P0 | 串行队列、插队与恢复 | 队列自动衔接、插队抢占已完成；被插队任务不挂起恢复，直接中止 |
| ~~P0~~ | 顶层 AI 审计（操作描述符、盲审、失败关闭、执行前复核） | 已完成：敏感操作先规则后盲审；模型不可用或异常时升级用户确认 |
| ~~P0~~ | 用户审计模式（AI 审计 / 执行前询问）与确认弹窗 | 已完成：两种模式可切换；批准仅对当前步骤生效 |
| ~~P0~~ | 登录阻塞与用户接管 | 已完成：无凭据时 `task.await_user` 中止；UIA 登录页确定性兜底；同会话「继续」沿用原目标并从当前屏幕恢复，队列透传用户消息与上下文 |
| P1 | 非标准登录页识别（验证码 / 二次验证 / 纯图形） | 视觉模型判定并请求用户接管；无法判定时不得绕过或猜测 |
| P1 | 凭据安全输入 | 密码不进入任务目标、会话记录与审计日志；接入系统凭据存储 |
| P1 | 审计决策面板、Token 统计与回放 | 缓存已实现；模型/耗时/用户决定的统计与回放待做 |
| ~~P1~~ | 破坏性 UI 操作识别规则 | 已完成：删除/卸载/格式化类元素名与危险快捷键触发审计 |
| P1 | 框选截图提问 | 多显示器/DPI 下裁剪与坐标正确 |
| P1 | HUD 运行中悬停滚轮/缩放 | 不抢焦点、不截获点击 |
| P1 | 用户非注入输入自动暂停 | 宿主侧低级钩子区分注入输入，误暂停可控；不再依赖已关闭的 Windows-MCP 所有权层 |
| P1 | 窗口归属跟踪与结束操控现场策略 | 可列出/收起本次窗口，不误关用户原有窗口 |
| P1 | Xshell MCP 真实会话验证（需装有 Xshell 的机器） | 在标签页启动 bridge 后，`terminal.exec` 跑通只读命令，含长命令与超时 |
| P1 | SFTP 文件传输后端（xshell-mcp 无此能力） | `file.transfer` 可用且与终端后端解耦 |
| P1 | UIA → 视觉通道切换策略 | UIA 无元素时自动降级，动作后强制截图 |
| P1 | 恢复策略库、审计/截图保留策略 | 失败可分类恢复，磁盘占用可控 |
| P2 | 单网关、多客户端、Tauri 壳与安装包、双通道更新 | 见第 5 章分期与验收标准 |

### 6.3 当前问题

| 问题 | 影响 | 处理方向 |
|---|---|---|
| 本机 `python` 命令失效（WindowsApps 别名），实际用 `py` + `.venv` | 启动脚本需避开 `python` | 安装器内置解释器，不依赖系统 Python |
| PyPI 官方 CDN 下载中断 | 直连安装会卡住 | 开发机使用镜像；安装包内置依赖 |
| 本机未安装 Xshell；xshell-mcp 无在线 bridge，且无退出码、无 SFTP | Xshell 真实会话暂不验证；`file.transfer` 需另配 | 框架保留；在装有 Xshell 的机器上启动 bridge.js 后再验证 |
| Computer-Use MCP 已构建，但首次初始化需要网络与 Node.js 20+ | 无网络或 npm 安装失败时视觉通道不可用 | 资源管理器多镜像重试；设置页显示错误并可重试，旧目录不直接删除 |
| Computer-Use 依赖 nut.js 原生模块 | Windows 预编译二进制不可用时启动失败 | 保留 UIA 主通道，视觉通道仅作可选后端 |
| Computer-Use 无元素树，语义验证需视觉模型 | 单步 `desktop.verify` 只能标记“待视觉核对”；若模型无视觉则会盲操作并虚假完成 | 无视觉模型时 `build_provider` 直接拒绝启动；`task.finish` 前用当步截图向视觉模型复核，判否即受阻并要求继续操作 |
| 视觉复核按次消耗 Token | 每次 `task.finish` 多一次带图请求 | 仅在结束前调用；拒绝后最多再试一次，超出按失败结束 |
| UIA 覆盖受页面结构影响：DOM 页面的 `word` 节点会占满 500 节点预算 | 某些页面只解析出 1 个可用元素 | 已过滤 `word/text`；后续提高预算 + 视觉通道兜底 |
| OpenAI / Anthropic / Gemini 尚未用真实 Key 联调；OpenCode 等网关可能无标准模型列表 | 限流、余额与模型下线的错误提示未验证；空列表时模型需手动添加 | GLM Coding Plan 已用真实 Key 验证并通过；界面支持自定义模型与手动视觉标记 |
| DPAPI 密文与 Windows 用户/机器绑定 | 复制数据库后密钥不可解密 | 迁移时要求重新填写 Key；企业环境可替换为 Vault |
| 用量只有 Token，没有金额 | 无法直接看到费用 | 需要时增加各厂商/套餐价格表做换算 |
| 租约锁接管存在极小竞态，无 fencing token | 极端并发下可能双执行 | 单机可接受；多客户端前引入 fencing |
| 审计清理未定时调度 | 长期运行磁盘增长 | 启动时与每日定时执行 prune |
| 策略是白名单式约束，不是沙箱 | 无法阻止白名单内命令的副作用 | 配合低权限账户与人工确认 |
| 自研最小 MCP 客户端 | 协议演进需自行跟进 | 稳定后迁移官方 SDK |
| 尚无桌面安装包 | 已有 pywebview 面板与 `start.cmd`，但没有安装器/托盘/更新器 | 按第 5 章 D0 → D3 推进；页面可复用现有 HTTP API |
| 面板历史不含完整事件 | 重启后只能看任务元数据；完整步骤在审计 JSONL | 4.14 的 `run_events` 持久化后解决；当前仍是 300 条内存事件 |
| 真正的多桌面隔离在 Windows 客户端不可行 | 无法做到“每个会话独立桌面、内嵌查看、用户同时操作” | 采用前台独占 + 单任务串行；真隔离只能上 VM/Sandbox，列入非目标 |
| `WDA_EXCLUDEFROMCAPTURE` 尚未在两个截图后端实测 | 覆盖层可能被视觉模型看到，干扰决策 | 分别用 Windows-MCP 与 Computer-Use 截图做像素验证；失效时截图前隐藏或裁剪 |
| F8 在 UAC/提权窗口可能收不到；也可能与目标应用快捷键冲突 | 暂停入口失效或误触 | 快捷键可配置；HUD/托盘保留可点击暂停；必要时后期加提权辅助进程 |
| 截图提问依赖视觉模型 | 纯文本模型无法使用该功能 | 活动模型被判定为纯文本时禁用并提示切换模型；本版本不做 OCR |
| 暂停/插队延迟受在途模型请求影响 | 用户按键后不能立即停 | 流式 + 可取消 HTTP 请求；只在步骤边界停，不打断单个 MCP 动作 |
| HUD 可交互与点击穿透冲突 | 可交互会吃掉 AI 的点击坐标 | 运行中点击穿透，点击 HUD 视为接管并暂停；暂停后才可交互 |
| 前台独占意味着用户不能同时操作电脑 | 用户中途操作会改变 AI 依赖的界面 | 明确提示 + F8 暂停 + 自动暂停（P1）；不承诺“互不影响” |
| 被插队任务留下的窗口可能干扰插队任务 | 恢复时目标窗口可能已变化 | 恢复前重新观测；无法区分时按失败处理并提示 |
| 中文输入法在注入框中使用空格/回车选词 | 误发送或误恢复 | 注入框 Enter 只换行，Ctrl+Enter 发送；对话框打开时 F8 不恢复 |
| 设计基线 3.12+，实测环境 3.12；Windows-MCP 0.8.5 要求 ≥3.12 | 版本矩阵已统一，不再需要分 venv | 继续锁定与 Runtime 同版本区间的 MCP 版本；升级前先验证 Python 下限 |
| 流式 `thinking` / `assistant` 增量只走 SSE，不落 `run_events` | 切换会话或重启后不能逐 token 回放，只能看最终消息截断的思考 | 后续把增量落库或改为快照；当前最终消息保留前 8000 字 |
| 未配置独立审计模型时复用活动模型 | 顶层审计与被审计模型相同，隔离仅靠提示词与无工具约束 | 生产环境在 `[audit_ai]` 指定不同厂商/模型；未配置时敏感操作升级确认 |
| 审计只看单步操作 | 无法识别跨步骤组合风险（如分多步删除多个目录） | 高风险类别继续由用户确认兜底；后续增加会话级组合检测 |
| 只读操作默认免审，依赖执行模型主动声明 | 模型不声明时只读动作不会送审 | 规则侧对敏感声明取并集；只读能力本身不产生副作用 |
| 审计缓存 120 秒（仅 AI allow/deny） | 环境变化未被键完全覆盖时可能复用旧判决 | 缓存键包含操作哈希、策略、模式与关键环境；高风险一律走用户 |
| 插队通过 `RunPreempted` 中止当前任务 | 被插队任务不会挂起后恢复，窗口状态可能已变 | 恢复前重新观测；后续可做挂起/恢复 |
| 全局 F8 已用 `RegisterHotKey` 实现；UAC/提权窗口或已占用 F8 的应用仍可能拦截 | 极少数前台场景暂停失效 | 注册失败时自动回退到窗口内 F8；HUD/托盘可点击暂停列入 P0 |
| 关闭了 Windows-MCP 自带所有权层（同意弹窗与输入钩子） | 用户在任务中直接操作桌面不会自动暂停，需按 F8；也没有其自带边缘提示 | 因为该层每次控制周期弹同意且低层钩子会吞 F8；改由任务级高亮 + 全局 F8 统一提示；宿主侧自动暂停列入 P1 |
| 用户确认最长等待 300 秒 | 超时按拒绝处理，任务可能因此中止 | 超时时间可配置；后续在 HUD 显示倒计时 |
| 删除会话把 `runs.conversation_id` 置空 | 任务历史与审计保留，但不再归属会话 | 有意的审计留痕；如需彻底删除应增加显式清理工具 |
| 登录页识别是文本启发式 | 纯图形、验证码墙、二次验证页面可能漏判；漏判时仍依赖模型主动调用 `task.await_user` | 增加视觉模型兜底判定；漏判时按失败结束并提示用户接管 |
