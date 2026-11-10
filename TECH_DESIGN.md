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
| 测试 | pytest + 固定任务集 + 回放 | 自建 CDP 断言（复用 `browser` 模块，不引入新依赖） | 先建基线，再谈优化 |

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

### 4.21 内置浏览器通道（浏览器功能模块 · 规划中）

> **模块标记：浏览器功能（Browser Channel）**
>
> - 归属：**独立执行通道**，与「桌面操控」「终端」并列，不是桌面适配器的一个子模式。
> - 状态：**P1 已完成**。`browser/` 的 cdp / engine / session / snapshot / input / mirror 六个模块，加上 4.22 的通道抽象、`browser.*` 能力契约、审计规则、CLI 与面板接入、面板镜像、纯浏览器模式、空闲回收调度、扫码登录接管，均已落地并真机自测。内置 Chromium 已纳入资源管理（按需安装、双源下载、`--version` 探针）。**未完成**：资源更新检测（自动发现新版本）。这一项不得在 README 或 STATUS 的「已完成」中描述为已有能力。
> - 引擎：**仅支持 Microsoft Edge、Google Chrome，以及内置 Chromium 兜底**；其他浏览器不在支持范围。
> - 数据：三种引擎**各用独立 profile**，互不共享，也不读取用户日常浏览器的资料。
> - 驱动：**纯 Python 标准库直连 CDP**，不依赖 Playwright、Selenium 或任何第三方包。

#### 4.21.1 定位与边界

一句话：让 Agent 在**自有浏览器进程**里完成任务，不占用、不截取、不点击用户的 Windows 桌面。

| 维度 | 桌面通道 | 浏览器通道（本模块） |
|---|---|---|
| 可见性 | 与用户共享同一桌面 | 无系统窗口，用户只看到镜像 |
| 运行前提 | 必须让出桌面、收起控制台 | 控制台可最小化，AI 照常工作 |
| 观测 | 屏幕像素 + UIA 树 | 页面截图 + DOM/无障碍树 |
| 坐标 | 受 DPI 与多显示器影响 | 视口坐标，无 DPI 问题 |
| 登录 | 用户在自己浏览器登录，Agent 无法继承 | 独立 profile 持久化，首次扫码后可复用 |
| 并发 | 串行、独占 | 一个进程多标签 |

明确不做：不把浏览器窗口嵌进面板；不读取或复制用户日常 Edge/Chrome 的用户数据；不在该通道里模拟桌面快捷键或跨应用操作；不承诺绕过验证码与风控，不代填密码。

#### 4.21.2 形态与驱动选型

**形态：不做 WebView2 内嵌控件。** 三个死结：

1. WebView2 是 UI 控件，随主窗口最小化停止渲染，AI 无法后台工作。
2. 无法单独截取该控件，只能拍整个屏幕，等于退回抢占桌面。
3. pywebview 一窗口一 WebView2，覆盖层与 HUD 的层级、点击穿透会被破坏。

因此采用**引擎与显示解耦**：引擎是独立浏览器进程（可无窗口），显示是把画面镜像进前端 canvas。

**驱动：不走 Playwright / Selenium，直接说 CDP。**

CDP（Chrome DevTools Protocol）就是 Playwright 内部真正在用的那层协议。我们需要的每一个动作都有原生方法，而 Playwright 提供的额外价值（选择器重试、自动等待、测试夹具）对「AI 看图决策」这个场景没有用：

| 需求 | CDP 原生方法 | Playwright 中的对应物 |
|---|---|---|
| AI 看页面像素 | `Page.captureScreenshot` | 同（转发） |
| 用户看实时镜像 | `Page.startScreencast` | 无公开等价 API |
| 点击 / 拖拽 / 滚轮 | `Input.dispatchMouseEvent` | `page.mouse.*` |
| 中文与密码输入 | `Input.insertText` | `page.keyboard.insertText` |
| 键位控制 | `Input.dispatchKeyEvent` | `page.keyboard.*` |
| 结构快照 | `Accessibility.getFullAXTree` / `DOM.*` | `page.accessibility.*` |
| Cookie 读写 | `Storage.getCookies` / `Network.setCookie` | `context.cookies()` |

放弃 Playwright 换来的是四条**对分发至关重要的性质**：

1. **零第三方依赖**。WebSocket 客户端按 RFC 6455 用 `socket` + `hashlib` + `struct` 实现（约 150 行），全链路只用标准库。打包后不需要 `pip install` 任何东西。
2. **不需要下载内核驱动**。Playwright 必须 `playwright install chromium`（约 150MB，从 `cdn.playwright.dev` 拉取，国内经常超时），我们直接启动系统已装的 Edge/Chrome。
3. **不需要版本对齐**。Playwright 与浏览器版本有兼容窗口，系统自动升级 Edge 后可能失效；裸 CDP 只要协议版本兼容就一直可用。
4. **没有 `headless shell` 指纹问题**。Playwright 新版默认无头用的是精简 shell，更容易被风控识别；我们自己决定 `--headless=new` 还是带窗口。

代价也要写清：等待、重试、元素定位这些便利要自己写（在 `browser/session.py` 里做，约 200 行）；将来若要支持 Firefox 必须换 WebDriver 栈——本模块已明确不支持 Firefox，不构成问题。

#### 4.21.3 引擎选择与 profile 隔离

三种引擎能力等价（都是 Chromium 系，CDP 全能力），差别只在**可执行文件从哪来**：

| 引擎 | 定位方式 | 来源 | 适用场景 |
|---|---|---|---|
| Edge | `msedge.exe` 常见安装路径 + `App Paths` 注册表 | 系统已装 | 默认首选，指纹与编解码最完整 |
| Chrome | `chrome.exe` 常见安装路径 + `App Paths` 注册表 | 系统已装 | 用户 Chrome 习惯 |
| Chromium | `.xm-agent/browser/engines/chromium-<ver>/` | 资源管理下载（Chrome for Testing） | Edge/Chrome 缺失或损坏时兜底 |

**解析顺序**：用户在任务/设置里显式指定 → `[runtime.browser].engine` → `auto`（Edge → Chrome → Chromium）。指定引擎缺失时不静默换引擎，而是明确提示「未检测到 X，已回落 Y」并记入运行事件。

**启动方式**：直接 `subprocess.Popen` 拉起可执行文件，参数固定为

```text
--user-data-dir=<profile> --remote-debugging-port=0 --no-first-run
--no-default-browser-check [--headless=new] <url>
```

`--remote-debugging-port=0` 让内核自己分配端口并把端口与 ws 路径写入 `<profile>/DevToolsActivePort`。**必须先删除该文件再启动**，否则会读到上一次实例的残留端口而连接被拒（实测踩到，见 4.21.14）。

**profile 隔离**（本版明确采用独立 profile，不复用用户真实浏览器数据）：

- 路径：`.xm-agent/browser/profiles/<engine>/`，例如 `edge/`、`chrome/`、`chromium/`。
- 每个引擎一个 profile，Cookie、localStorage、IndexedDB、Service Worker 各自独立，互不串号。
- 首次使用某引擎需扫码一次，之后长期有效；换引擎等于换环境，需要重新登录。
- 同一 profile 加写锁，只允许一个引擎实例；并发任务排队而不是争抢。
- 空闲回收可关闭 context，登录态在磁盘，重开即恢复（**必须优雅关闭**，见 4.21.8）。
- 目录已被 `.gitignore` 覆盖，不进版本库、不进备份。

配置草案：

```toml
[runtime.browser]
engine = "auto"                              # auto | edge | chrome | chromium
profile_root = ".xm-agent/browser/profiles"
headless = "auto"                            # auto | always | never
keep_alive_seconds = 300                     # 空闲多久后关闭内核

[resources]
update_check = "notify"                      # off | notify | auto
check_interval_hours = 24
include_prerelease = false
```

#### 4.21.4 内置 Chromium 与资源完整性

内置 Chromium 不是手工安装，也不是 Playwright 的构建，而是 **Chrome for Testing** 的独立发行版——它就是 Google 官方给自动化场景出的 Chromium 构建，带完整编解码，有稳定的版本化 URL，纯 HTTP 直下：

```text
https://storage.googleapis.com/chrome-for-testing-public/<version>/win64/chrome-win64.zip
```

纳入现有资源管理：

- 新增资源类型 `kind="browser"`，走独立安装分支：下载 zip → 标准库 `zipfile` 解压到 `engines/chromium-<version>/` → 探针验证。
- 版本来源：`https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json`（JSON，取 `Stable`）。
- 完整性校验用**启动探针**而非文件哈希：拉起内核读 `/json/version` 拿真实版本，探不起来即为损坏并重装。这比哈希更贴近「能不能用」。
- 国内镜像：`https://registry.npmmirror.com/-/binary/chrome-for-testing/`，失败再回落官方源，与 MCP 资源共用同一套镜像策略。
- marker 记录版本、来源、安装时间与探针结果，供状态面板与更新检测共用。
- `start.cmd` 在现有 `resources ensure` 之后追加浏览器检查；缺失才下载，已存在不重复安装。

安全打包：`chrome-win64.zip` 是 Google 官方发行的二进制，签名由 Windows 校验；解压走现有 `resources` 的安全解压逻辑（拒绝绝对路径、`..` 穿越与符号链接），不新写一套。
- 面板「资源」页展示三种引擎的可用性：已装版本、profile 路径、登录状态（只显示「已登录/未登录」，不展示内容）。

#### 4.21.5 双视觉通路

- **AI 的眼睛**：`Page.captureScreenshot`，只在浏览器进程内合成，不经过桌面捕获 API；用户切到别的窗口也不受影响。
- **用户的眼睛**：`Page.startScreencast`（JPEG q≈60、`maxWidth` 1280）经 SSE 推到 canvas；面板收起即停流。
- 两条通路互不绑定：AI 可高频取图决策，用户画面可低频刷新。

#### 4.21.6 输入回传与中文

- 鼠标：canvas 坐标按视口比例换算后 `Input.dispatchMouseEvent`（move/down/up 与滚轮）。
- 文本：`Input.insertText` 整段落入，绕开输入法逐键模拟；密码框同路径。
- 控制键：`Input.dispatchKeyEvent` 承载 Enter / Tab / Esc。
- 接管态：面板切到「手动」，同样的输入直接转发给页面，AI 暂停动作但保持观察。

#### 4.21.7 能力契约与通道抽象

- 新增命名空间 `browser.*`：`open` / `observe` / `act` / `verify` / `tab` / `wait`；`task.finish`、`task.await_user` 保持全局。
- `Channel` 需声明：能力集、观测模型、是否独占桌面、是否支持并发、界面表层。运行时按通道下发提示词，模型看不到当前通道不存在的能力。
- 路由：`auto` 按目标文本与用户选择决定通道；任务级与全局设置均可强制「内置浏览器」，并可再指定引擎。
- 生命周期解耦：仅当 `channel.uses_desktop` 为真，才触发现有 `indicator.show()`、`DesktopHandoff.release()` 与 `needs_screen` 分支。
- 契约测试扩到第 5 个后端（浏览器 mock page），新增通道或引擎不改用例。

#### 4.21.8 登录与扫码接管

1. `browser.open` 目标站点后做登录检测：复用 `_login_wall` 启发式，并扩展 DOM 规则（密码框、二维码 canvas、「扫码登录」文案、URL 特征）。
2. 命中且当前引擎的 profile 无有效会话 → `task.await_user(kind="login")`，界面自动展开镜像面板并提示「请在面板中扫码」，同时注明当前引擎。若判定为无头模式不利于扫码，自动以 `--headless=new` 之外的可见模式重开一次（`headless = "auto"` 的语义）。
3. 扫码期间面板处于接管态；检测到会话 Cookie 变化或跳转到登录后首页，自动续跑，或由用户点「我已登录，继续」。
4. 登录失败或被风控拦截：明确告知并停止，不做重试轰炸。

**登录态落盘必须优雅关闭**。实测结论（4.21.14）：直接 `TerminateProcess` 会丢掉内存中的 Cookie 与 localStorage，磁盘上虽然存在 `Default/Network/Cookies` 文件但内容未写入；必须走 `Browser.close` 让内核自己退出。因此：

- 所有关闭路径统一走 `Browser.close`，等待进程退出（超时 10s 再降级强杀并告警）；
- 兜底双保险：登录成功后主动 `Storage.getCookies` 导出，用现有 DPAPI 密钥加密存一份快照；下次启动若发现 profile 里没有会话，提示可用快照恢复（快照只存 cookie 与 localStorage，不含密码）；
- 崩溃恢复：检测到 `DevToolsActivePort` 残留且端口不可连，先清理再启动。

#### 4.21.9 界面形态

三种表层共用同一个镜像组件：

- **侧边抽屉**：任务页右侧可展开/收起，含地址栏、标签、引擎标识、自动/手动切换；不离开对话即可看页面。
- **全屏视图**：整个内容区变为浏览器，左侧一条可展开的对话列（复用会话流），适合长流程任务。
- **独立第二窗口**（可选）：pywebview 多窗口打开 `/browser`，适合双屏。

引擎切换放在设置页与任务页两处：任务级只影响本次任务，全局作为默认值。

#### 4.21.10 安全、审计与数据

- 浏览器动作与桌面动作走同一条 `guard` 管线；风险类别复用「提交订单 / 删除 / 公开发布 / 上传」，审计提示词改带 URL 与元素文本。
- profile 是 Chromium 明文格式，等价于账号凭据：仅当前用户 ACL、不进 git 与备份、界面明示存储内容、支持一键清除与按引擎清除。
- 因为不读取用户真实浏览器数据，账号泄露面仅限 Agent 自己登录过的站点；代价是每个引擎首次都要扫码。
- 不读系统密码管理器、不代填密码；密码键入走 `insertText` 且不写日志。
- 下载文件落 `.xm-agent/downloads/`，纳入 `maintenance.cleanup` 保留期。

#### 4.21.11 资源更新检测

面向第三方 MCP 与内置 Chromium，统一做版本发现与安全升级：

| 环节 | 做法 |
|---|---|
| 发现 | GitHub Releases（首选）或 Tags；带 ETag 与 `last_check` 缓存，默认 24 小时查一次 |
| 兼容门禁 | Python 版本要求、Node `engines`、资源声明的 `runtime_checks`、MCP `tools/list` 探针 |
| 安装 | 下载解压到新的版本化目录，绝不覆盖在用目录 |
| 激活 | 探针全过才切换 junction；任一失败保留旧版并记录原因 |
| 回滚 | 保留上一版本目录，切回只需改链接 |
| 策略 | `update_check = "off" / "notify" / "auto"`；`auto` 只跟稳定版，且必须无运行中任务 |
| 限流 | 未认证 60 次/小时，靠缓存摊薄；可选 `GITHUB_TOKEN` 提升上限 |

内置 Chromium 的更新口径不同：跟随 Chrome for Testing 的 `Stable` 版本，升级后重装内核并用 `/json/version` 复核实际版本，不与 MCP 资源混在同一条流水线。Edge/Chrome 由系统或用户自行升级，本系统只做**兼容探测**（启动 + `/json/version`），不主动更新用户的浏览器。

#### 4.21.12 已知风险与不承诺

| 风险 | 说明 | 应对 |
|---|---|---|
| 引擎缺失或升级 | 目标机器未装 Edge/Chrome，或系统升级后启动参数变化 | 回落内置 Chromium 并明示；不静默降级 |
| 自动化识别 | 部分站点会拦截无头浏览器 | 真实内核 + 持久 profile + 拟人节奏；提供「可见模式」与人工接管 |
| 验证码 | 滑块、点选类无法自动通过 | 转人工，镜像面板可见 |
| 内存 | 单页 150–300MB，三个引擎各占一份 | 空闲回收；同引擎并发排队 |
| 镜像延迟 | 实测约 72ms/帧（约 14fps），见 4.21.14 | 面板隐藏停流；AI 取图与用户画面独立 |
| 登录态丢失 | 非优雅退出会丢 Cookie（实测） | 统一 `Browser.close` + DPAPI 快照兜底，见 4.21.8 |
| 跨域 iframe | DOM 引用需带 frame 限定 | 视觉坐标兜底 |
| 合规 | 自动化可能违反站点条款 | 首次启用时明示，责任在使用方 |

#### 4.21.13 分阶段

| 阶段 | 出口标准 |
|---|---|
| P0 地基验证 | **已完成**，见 4.21.14 实测数据 |
| P1 引擎与适配器 | 引擎与观测动作层**已完成**（`browser/cdp|engine|session|snapshot|input|mirror.py`，32 个用例）；剩余：`browser.*` 能力契约、契约测试扩到第 5 个后端 |
| P2 通道抽象 | 浏览器通道运行时不截屏、不显覆盖层、不让出桌面；路由与引擎可强制指定 |
| P3 界面三表层 | 抽屉、全屏视图（含左侧对话列）、可选独立窗口均可用（后端帧流已完成） |
| P4 登录与接管 | 每个引擎首次扫码、接管、自动续跑全链路通过；profile 可按引擎清除 |
| P5 资源与更新 | 内置 Chromium 纳入 `ensure` 与完整性探针；MCP 资源更新检测可发现、可门禁、可回滚 |
| P6 加固 | 反爬兜底、可见模式、下载上传、性能 |

#### 4.21.14 地基实测（2026-11-10，本机 Edge 157 + 纯标准库 CDP）

验证脚本只 import 标准库，未安装 Playwright、Selenium 或任何 WebSocket 包。全部数字来自本机实测：

| 指标 | 结果 |
|---|---|
| 内核启动到可连 CDP | **0.16s**（`--headless=new`，Edge 157.0.4341.42） |
| 页面截图 `Page.captureScreenshot` | **133ms**，JPEG q70 = 7.0 KB，magic `ffd8` 正确 |
| 坐标点击（move + press + release） | **14ms**，命中注入按钮，`clientX/Y` 精确等于派发坐标 `[200,140]` |
| 中文输入 `Input.insertText` | 焦点在输入框时完整写入「第二个字段」；无焦点时丢弃（**必须先聚焦**） |
| 无障碍树 `Accessibility.getFullAXTree` | 5 节点（最小页面） |
| 实时镜像 `Page.startScreencast` | **6 帧 / 4 秒采样内间隔 72ms ≈ 14fps**，单帧 base64 约 13–14 KB |
| Cookie 持久化（硬杀 `terminate`） | **丢失**：磁盘有 20 KB `Default/Network/Cookies`，但内容未 flush |
| Cookie + localStorage（`Browser.close`） | **完整保留**：重开后 `sessionid=token-xyz-999`、`localStorage.user='tester'` 均在 |
| headful 可见模式 | 可启动并可截图（10020 字节），供扫码时给用户看真窗口 |

同时暴露三个必须写进代码的坑：

1. **`DevToolsActivePort` 残留**：profile 目录里该文件存在时，直接读会拿到上次实例的端口，连接报 `WinError 10061`。启动前必须先删除该文件，再等待新文件出现并**校验端口可连**，不能只判断文件存在。
2. **`Input.insertText` 依赖焦点**：无焦点时静默丢弃，不报错。输入前必须先 `Runtime.evaluate` 聚焦目标元素，或用 `DOM.focus`。
3. **镜像 ack 会在读线程里死锁**：`Page.screencastFrame` 的 handler 在读线程中执行，若在里面同步 `call("Page.screencastFrameAck")`，读线程会等一个只能由它自己置位的响应。必须提供「只发不等」的 `post()` 通道。
4. **静止页面不推帧**：screencast 只在画面可见变化时出帧，新建镜像会一直空白。启动时先截一张静态图作为首帧兜底。

验证脚本已入库：`scripts/probe_browser_cdp.py`（纯标准库，与 `probe_windows_mcp.py` 等并列）。复跑命令：

```bat
PYTHONPATH=src .venv\Scripts\python.exe scripts\probe_browser_cdp.py
PYTHONPATH=src .venv\Scripts\python.exe scripts\probe_browser_cdp.py --engine chrome
PYTHONPATH=src .venv\Scripts\python.exe scripts\probe_browser_cdp.py --headful
```

该脚本里的 `CdpClient` 与 `Engine` 已提炼为正式模块 `browser/cdp.py`、`browser/engine.py`，脚本保留作排障工具。等价断言写在 `tests/test_browser_engine.py`（18 项逻辑）与 `tests/test_browser_session.py`（14 项真机，无浏览器自动跳过）。

#### 4.21.15 打包与零环境依赖

目标：分发之后，目标机器上**不需要预装 Playwright、Node、驱动或任何 Python 包**，只要能联网（甚至完全离线也能用本机已装的 Edge/Chrome）。

依赖分三层，每层的来源都不同：

| 层 | 内容 | 来源 | 目标机器是否需要联网 |
|---|---|---|---|
| 运行时 | Python 解释器 + 本项目代码 + 现有第三方包 | PyInstaller 打包进发行包 | 否 |
| 驱动 | WebSocket / HTTP / 解压 / JSON | **Python 标准库**，无第三方包 | 否 |
| 内核 | Edge / Chrome / Chromium | 先找系统已装；都没有才下载 Chrome for Testing | 仅在无系统浏览器时需要 |

**为什么能做到零依赖**：浏览器通道不引入任何新的 Python 依赖。WebSocket 客户端 150 行、CDP 调用封装 200 行、zip 解压用 `zipfile`、HTTP 用 `urllib.request`——全在标准库里。所以 `pyproject.toml` 的 `dependencies` 不因这个模块增加一项，PyInstaller 也不需要打任何 `hooks`。

对比传统 Playwright 方案在分发时的成本：

| 环节 | Playwright 方案 | 本方案 |
|---|---|---|
| 安装 Python 包 | `pip install playwright`（约 40MB wheel + `greenlet` + `pyee`） | 不需要 |
| 安装内核 | `playwright install chromium`（约 150MB，`cdn.playwright.dev`） | 有 Edge/Chrome 则 0 字节 |
| 安装系统依赖 | Linux 需 `install-deps`，Windows 需 VC 运行库 | 不需要 |
| 版本漂移 | Playwright 版本与内核版本绑定，升级要重装 | 协议兼容即可，内核随系统升级 |
| 打包体积 | +150MB 内核 + 40MB 包 | 0（用系统浏览器时） |

**下载器的实现约定**（仅在内核缺失时走这条路径）：

1. 纯 `urllib.request` 流式下载到 `.xm-agent/browser/downloads/*.part`，边下边算 SHA-256，完成后原子重命名。
2. 断点续传：`Range` 请求 + `.part` 大小续接；服务端不支持就重下。
3. 源顺序：`[resources].download_sources` 配置的镜像列表 → 官方源。默认把 npmmirror 放第一位（国内可用性）。
4. 并发与超时：单线程，30s 连接超时、读超时 120s，失败重试 2 次并指数退避；总量上限 400MB 防误配。
5. 解压用现有 `resources` 的安全解压（拒绝绝对路径、`..`、符号链接、超大单文件）。
6. 下载完立刻跑启动探针；探针失败即删除该版本目录，不留半成品。
7. 全部失败时给明确出口：提示用户自行安装 Edge 或 Chrome（给出官方链接），不静默卡住。

**首次运行流程**（`start.cmd` 串行执行）：

```text
resources ensure（MCP，已有）
  → browser ensure（检查 Edge → Chrome → 本地 Chromium）
      → 命中：记录引擎与版本，直接进入
      → 全缺失：下载 Chrome for Testing（走镜像），探针验证
  → 启动面板
```

**离线场景**：系统有 Edge/Chrome 时，整个浏览器通道完全离线可用（CDP 是本地回环 socket，不出网）。只有内置 Chromium 缺失且无网络时才会失败，此时降级为「桌面通道」并明确告知原因。

**打包注意**：PyInstaller 需额外收集 `certifi` 的 CA 包（`urllib` 走 https 时需要）。若使用 `--onefile`，首次解压会增加启动延迟，建议 `--onedir`，内核下载到用户目录而不是包内目录（包内目录可能只读）。

### 4.22 通道抽象与能力路由（已实现）

> **模块标记：浏览器功能（Browser Channel）· P1 上半**
>
> 状态：**已实现并真机自测**。`src/xm_agent/channels.py`（通道契约与 Hub）、`src/xm_agent/browser/channel.py`（浏览器通道）、5 个 `browser.*` 能力契约、guard 规则、CLI 与面板接入、面板镜像（抽屉 + 全屏视图 + SSE 帧流）均已完成。扫码接管编排、资源管理、更新检测、纯浏览器模式仍属后续阶段。

#### 4.22.1 为什么必须先做这一层

浏览器模块的地基（CDP、引擎、会话）做完时，它**还调不通**。原因不是缺个函数，而是 Runtime 的结构把它挡在门外，共有四处硬编码：

| 位置 | 原状 | 后果 |
|---|---|---|
| `AgentRuntime.__init__` | `adapter` 单个后端 | 一个任务只能用一种后端，桌面和浏览器不能共存 |
| `CAPABILITIES` | 8 项全是 `desktop.*` | 模型根本不知道浏览器能干什么 |
| `Observation` 的构造 | 由各个 adapter 自行拼装 | 浏览器页面没有 UIA 元素树，形状对不上 |
| `ui/server.py` | 「有 adapter 在跑」等于「正在占用屏幕」 | 起浏览器也要收起控制台、显示录制指示器——明明没碰桌面 |

只把浏览器硬塞进 `Adapter` 协议也能跑，但会留下后患：`observe()` 没有通道语义，模型分不清看到的是屏幕还是网页；`vision_only` 这类桌面专有属性会污染浏览器；将来加第三个通道又要改一遍 Runtime。所以先抽一层。

#### 4.22.2 契约：Channel 只有四个方法

```python
class Channel(Protocol):
    name: str                  # 实例名，Hub 内唯一
    kind: str                  # "desktop" | "browser"
    capabilities: frozenset[str]

    def observe(self, step, include_screenshot=False) -> Observation: ...
    def execute(self, capability, args) -> Receipt: ...
    def verify(self, expect) -> Verification: ...
    def close(self) -> None: ...
```

刻意只保留 Runtime 真正调用到的四个方法。截图落盘、窗口前置、空闲回收这些是通道自己的事，Hub 不关心——否则每加一种能力就要扩一次协议。

两个可选约定，用 `getattr` 探测而不是写进 Protocol：

- `available()`：现在能不能干活。浏览器未启动时返回 `False`，避免「上次用的是浏览器」导致隐式观测把它重新拉起来。
- `capture_screenshot()` / `screenshot_dir`：审计要图时的取图口，桌面后端历史上就有，浏览器对齐即可。

#### 4.22.3 路由规则

Hub 按**能力名**分发，不猜意图：

```text
browser.*                  → 浏览器通道
desktop.* / terminal.* /   → 桌面通道
file.* / wait.* / task.*
```

三条细则：

1. **精确声明优先**。通道若在自己的 `capabilities` 里列了某能力，就直接给它；否则按前缀选。这样浏览器只声明自己的 5 项，不必假装支持 `task.finish`。
2. **观测跟随动作**。Hub 记住 `last_kind`：浏览器动作之后观测网页，桌面动作之后观测屏幕。没有这条，每一步都要两边各截一次，既慢又会给模型看错的界面。`task.*` 是终止能力，不参与观测。
3. **不可用就回落**。`active()` 跳过 `available()` 为假的通道。这是 `browser.session close` 之后仍能继续跑桌面任务的原因。

能力清单也按通道裁剪：Runtime 在 `run()` 开始时把 `hub.capabilities()` 写进 provider，系统提示与每步的 `known_capabilities` 都只列真能调的项。配置里 `[runtime.browser] enabled = false` 时，模型完全看不到 `browser.*`，不会浪费步数去试。

#### 4.22.4 五个浏览器能力

| 能力 | 用途 | 关键参数 |
|---|---|---|
| `browser.open` | 启动内核并加载 URL，复用持久 profile | `url`、`new_tab`、`wait_loaded` |
| `browser.observe` | 读页面：URL、标题、带序号的元素、可见文本、可选截图 | `include_screenshot` |
| `browser.act` | 一个动作：goto / click / type / press / scroll / back / forward / reload / wait | `element_index`、`url`、`text`、`keys`、`amount` |
| `browser.verify` | 按精确文本校验：URL 片段、标题、正文、元素名 | `url_contains`、`text`、`element_text` |
| `browser.session` | status / handoff（把窗口给用户扫码）/ close | `action` |

元素序号用 `b0`、`b1`…，与 `browser.observe` 清单里的 `[b3]` 一一对应；`element_index` 也接受纯数字，`_as_index` 统一解析。**优先序号而不是坐标**：坐标会因滚动、弹窗、懒加载而失效，序号每次观测都重新编号。

`browser.act` 的 `click` 支持三种定位（序号 → 坐标 → CSS 选择器），按可靠性排序回落；`type` 可先点元素再输入，解决 `Input.insertText` 在没有焦点时静默丢弃的问题。

#### 4.22.5 与审计的衔接

浏览器动作同样是「看不见的命令行」，必须进同一条审计管线。做法是**复用**而不是另写一套：

- `browser.observe` / `browser.verify` 进 `READ_ONLY_CAPABILITIES`；`scroll` / `wait` 视为只读，不算 mutating。
- `browser.act` 的效果推导复用 `_ui_effects`：点击「提交 / 发布 / 支付 / 删除 / 发送」类文本会得到 `ui_destructive` 或 `ui_commit`，风险等级与桌面点击完全一致。`goto` 只判 `navigate`；非 `http/https/about` 协议（`file:`、`javascript:`、`data:`）额外标记 `external_write`。
- `needs_visual_review` 对 `browser.act` 也生效：高风险点击仍要过顶层视觉审计。
- 审计摘要把浏览器动作翻译成人话（「在网页上点击元素（可能提交、发布或跳转）」），用户在确认弹窗里看得懂。

#### 4.22.6 实测（本机 Edge 157.0.4341.42，无头）

`scripts/probe_browser_channel.py` 起本地静态页，按模型的方式走完 open → observe → click → type → verify → close：

| 环节 | 实测 |
|---|---|
| 首次启动到可操作（含 profile 创建） | 722 ms |
| 页面观测（元素 + 文本） | 62 ms |
| 按序号点击 | 16 ms |
| 中文输入「上海天气」 | 通过（4 字符） |
| 点击后新元素出现、文本校验 | 通过 |
| 优雅关闭后 profile 保留 | 通过 |

Runtime 全链路（真实浏览器 + 桌面 MockAdapter，5 步任务）：

```text
1 browser.open   观测源=mock      ok
2 browser.act    观测源=browser   ok   ← 自动切到网页
3 browser.act    观测源=browser   ok
4 browser.act    观测源=browser   ok   verify 命中「结果:上海天气」
5 task.finish    观测源=browser   ok
总计 0.9 s；桌面只被观测 1 次
```

#### 4.22.7 镜像与界面（抽屉 / 全屏）

后端只认一个开关：**有人订阅就出帧，没人看就停流**。

```text
GET /api/browser         状态：引擎、URL、标题、是否镜像、累计帧数
POST /api/browser        open(url) / handoff / close
GET /api/browser/stream  SSE：browser_ready + browser_frame(data URL)
```

`BrowserChannel.subscribe(callback)` 记录订阅者，必要时调 `Page.startScreencast`；`unsubscribe` 到零就 `stop_mirror()`。帧在 CDP 读线程里回调，面板侧队列只留最新几帧——用户看的是「现在」，积压只会让延迟越滚越大。

界面上有两个形态，同一个 DOM，只换 CSS 布局：

| 形态 | 布局 | 用途 |
|---|---|---|
| 抽屉（默认） | 绝对定位覆盖在任务页右侧，宽 `min(52vw, 680px)` | 边聊边看，任务页不被挤压 |
| 全屏 | `.task-workspace` 改成两列（左对话 320–400px，右浏览器） | 长时间盯页面操作；Esc 退回抽屉 |

交互约定：

- AI 第一次用 `browser.*` 时自动展开抽屉一次；用户手动收起后不再自动弹出（尊重「我看完了」）。
- 头部三个按钮：显示真实窗口（handoff，扫码用）、全屏、收起。收起不影响 AI 继续运行。
- 空态给出地址输入框，可直接打开一个页面——用户手动动作是显式的，不会被当成 AI 行为。
- 静止页面不出新帧是 CDP 的正常行为（`screencastFrame` 只在画面变化时触发），所以开流时先塞一张静态截图，避免面板一片空白。

#### 4.22.9 纯浏览器模式（已实现）

选 `adapter=browser` 时，桌面通道根本不挂载：

- `build_hub(config, None)` 只注册浏览器通道，能力清单只剩 `browser.*`（`task.*` 由 Runtime 内部分支处理，不经通道）。
- 模型拿到的提示词由 `system_prompt(hub.capabilities())` 生成，`desktop.act` 不会出现，模型也就不会尝试调它。
- 不显示屏幕覆盖层、不让出桌面、审计要图只问当前通道（`_capture_for_audit` 依次问 adapter 与 hub.active()，没有桌面通道时直接返回 None）。
- 首帧观测的坑：纯浏览器模式下通道尚未启动，`available()` 为假会让观测直接失败。Hub 在「一个可用通道都没有且只注册了一个通道」时调用 `activate()` 唤醒它；混合模式不会走这条路，避免一次观测顺手拉起内核。

真机实测（本机 Edge，无头）：5 步 0.8s 完成「打开本地页 → 点输入框 → 输入 → 点搜索 → 校验结果」，全部观测 `source=browser`，审计目录没有一张桌面截图。

#### 4.22.10 空闲回收调度（已实现）

内核常驻会白占内存，`keep_alive_seconds` 定义「上次用完还能留多久」：

- 面板启动后拉一个后台线程，间隔 `clamp(keep_alive/3, 5, 60)` 秒巡检一次，调用 `_tick_browser_reaper()`。
- 正在跑任务（`_active` 被占）不回收；有人在看镜像（`mirror_active`）也不回收——用户正盯着页面，关掉等于打断他。
- `keep_alive_seconds < 0` 表示永不自动回收（调试或希望窗口一直开着）。
- 回收走优雅关闭，profile 与登录态留在磁盘；回收后广播 `browser` 事件，面板底部状态自动刷新。

#### 4.22.11 扫码登录接管（已实现）

网页登录十有八九要扫码，无头模式里用户根本看不到二维码。闭环如下：

1. `BrowserChannel.observe()` 在 `raw.login_wall` 里标记登录墙（密码框 + URL 关键词 + 提示文案三重判定）。
2. Runtime 的 `_login_wall_action()` 发现「浏览器来源 + login_wall」时，先调 `handoff()`：
   - 无头 → 带着同一个 profile 优雅关闭再以可见模式重开（实测 0.5s），并把原 URL 开回来；
   - 已可见 → `Page.bringToFront` 前置窗口，不重启（重启会丢当前页面状态）。
3. 然后返回 `task.await_user`（`kind=login`），任务停在 `needs_human`，提示「扫码登录后发送继续」。
4. 用户登录后回复「继续」，Runtime 复用原目标继续，此时 Cookie 已落盘，后续无头会话直接是登录态。

实测：无头下写入持久 Cookie → handoff → 重开后 `document.cookie` 仍是 `probe=kept`；无 `max-age` 的会话 Cookie 会随重启丢失，这是浏览器行为，不是缺陷。

注意：目标本身就是要登录（`登录/注册/密码/账号` 等关键词）时不触发暂停——那是用户明确要求的事；任务上下文里已给出密码时同样不暂停。

#### 4.22.12 内置 Chromium 纳入资源管理（已实现）

内核是 210MB 的二进制，不能塞进安装包，也不能每次启动都检查下载。做法：

- 清单里新增 `chromium` 资源：`kind="browser"`、`install_dir=".xm-agent/browser/engines"`、`entry="chrome.exe"`。
  `install_dir` 与引擎解析的 `bundled_root`（`profile_root/../engines`）对齐，装完不用改配置就能被 `find_engine_path` 找到（它按 `chromium-*/chrome.exe` 取最新版本）。
- 下载源写成显式列表（`Resource.downloads`）：Google 官方 `storage.googleapis.com/chrome-for-testing-public` 与 npmmirror 镜像；这两个都不是 GitHub，所以**不走 GitHub 代理**，直接按顺序试。
- 安装分支只做「下载 → 安全解压 → 校验入口」：不建 junction、不跑构建。Chrome for Testing 的 zip 顶层目录是 `chrome-win64/`，现有解压逻辑会把它剥掉，最终落在 `chromium-<version>/chrome.exe`。
- **默认不装**：`ensure()` 不传 `only` 时跳过 `kind="browser"` 的资源，避免「检测并修复资源」顺手拉 210MB。安装入口是面板里该项的安装按钮，或 `python -m xm_agent resources ensure --only chromium`。
- 启动探针：`ResourceManager.probe_browser()` 真的执行一次 `chrome.exe --version`。只看文件是否存在不够——解压不全的 exe 也会存在，一启动就退出。面板的资源探针按 kind 分流，不会把内核当成 MCP 服务去起。
- `start.cmd` 增加内核检查：有 Edge/Chrome 就直接过，都没有才提示安装内置内核。
- 版本号固定为 `157.0.8103.50`（与 2026-11 的 Stable 一致）；临时文件、下载缓存都在 `.xm-agent/resources` 下，已在 `.gitignore` 中。

**踩坑记录**：默认配置的 `profile_root` 是相对路径，Chromium 在相对 `--user-data-dir` 下会把 `DevToolsActivePort` 写到别处，启动探针 20s 后报「未就绪」。此前的实测都用临时绝对路径，所以没暴露；现在 `BrowserSession.start()` 一律 `resolve()`，并加了回归用例。

**实测**（内置 Chromium 157.0.8103.50，npmmirror 源 10.6 MB/s，约 20s 下完 200.9 MB）：安装后 `chrome.exe --version` 返回 `Google Chrome for Testing 157.0.8103.50`；`resolve_engine("chromium")` 命中 `source=bundled`；启动 1.41s、观测 example.com、按序号点击 b0 均成功。

**踩坑记录**：默认配置的 `profile_root` 是相对路径，Chromium 在相对 `--user-data-dir` 下会把 `DevToolsActivePort` 写到别处，启动探针 20s 后报「未就绪」。此前的实测都用临时绝对路径，所以没暴露；现在 `BrowserSession.start()` 一律 `resolve()`，并加了回归用例。

**实测**（内置 Chromium 157.0.8103.50，npmmirror 源 10.6 MB/s，约 20s 下完 200.9 MB）：安装后 `chrome.exe --version` 返回 `Google Chrome for Testing 157.0.8103.50`；`resolve_engine("chromium")` 命中 `source=bundled`；启动 1.41s、观测 example.com、按序号点击 b0 均成功。

#### 4.22.13 未完成

1. **资源更新检测**（含第三方 MCP）：查 GitHub Releases/Tags、版本比较、兼容门禁、旁路安装、探针通过才切换、失败回滚、`off/notify/auto` 三档。当前只支持「清单里写死的版本」，没有自动发现新版本。

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
| 测试（pytest，423 项） | `tests/` | 通过 |
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
| **浏览器**（新模块） | **浏览器功能：内置浏览器通道**（见 4.21 / 4.22） | 已完成：三引擎解析与独立 profile、CDP 六模块、通道抽象与路由、5 个 `browser.*` 能力、审计规则、CLI 与面板接入、面板镜像（抽屉 / 全屏 / SSE 帧流）、纯浏览器模式、空闲回收调度、扫码登录接管。内置 Chromium 已纳入资源管理（按需安装、双源、启动探针）。待完成：资源更新检测 |
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
