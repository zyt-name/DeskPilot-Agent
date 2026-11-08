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
| `desktop.act` | element_id 或坐标 + 动作 | 执行回执 + 后置截图 | Windows-MCP、computer-use-mcp |
| `desktop.verify` | 期望条件 | 通过 / 失败 + 证据 | UIA 查询、视觉模型判图 |
| `terminal.exec` | 主机 / 命令 / 超时 | 退出码 + stdout / stderr | Xshell MCP、SSH MCP |
| `file.transfer` | 源 / 目标 / 方向 | 传输结果 | SFTP MCP、SSH MCP（xshell-mcp 不含 SFTP 工具） |
| `wait.for` | 文本 / 元素 / 窗口 / 超时 | 命中结果 | 各执行器原生能力 |

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
  → ACT        执行单一动作，获取回执
  → VERIFY     校验元素状态 / 文本 / 退出码 / 截图差异
  → RECOVER    重试、换通道、回退或中止
  → DONE / ABORT
```

约束：

- 每步只执行一个原子动作，便于验证与回放。
- 最大步数、总超时、token 预算、单任务成本上限必须可配置。
- 支持用户随时取消；取消后释放锁并记录当前状态。
- 破坏性动作、跨主机操作、权限提升必须经过人工确认门。

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

### 4.4 观测标准化

- 统一使用虚拟屏幕坐标，记录显示器原点与缩放比例。
- 进程启动时声明 DPI 感知模式，避免坐标漂移。
- UIA 元素 ID 带 TTL，界面变化后失效，不跨步骤复用。
- 截图上送模型前按比例缩放，并记录缩放系数用于坐标还原。
- 每次观测同时记录活动窗口标题、进程名、前台焦点状态。

### 4.5 执行与验证

- 动作幂等性尽量保证：点击前校验元素可交互，输入前清空目标字段。
- 每个动作返回回执：动作类型、目标、时间、耗时、截图、执行结果。
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

**模型列表与视觉标记**

`src/xm_agent/ai/models.py` 按 `models_style` 选择官方接口：OpenAI `/models`、Anthropic `/models?limit=100`、Gemini `v1beta/models`、Ollama `/api/tags`。`models_style` 由套餐（Plan）决定并在界面锁定，只有自定义端点允许手选，避免用户选错风格得到空列表。部分网关会用 HTTP 200 包裹业务错误（GLM Coding Plan 的 Anthropic 端点返回 `{"code":500,"msg":"404 NOT_FOUND","success":false}`），因此解析前先检查 `success:false` / `error` / 非 0 `code`；套餐可声明 `fallback_base_url` 与 `fallback_models_style`，主端点失败或无结果时自动回退（GLM Coding Plan → 标准 `/api/paas/v4`）。空结果不会清除已有模型。拉取后过滤 embedding / rerank / tts 等非对话模型，并用名称规则标记可能支持图像输入的模型（GPT-4o/4.1/5、Claude 3/4、Gemini、GLM-4V、Qwen-VL、Pixtral、LLaVA 等）。自定义模型可手动勾选「支持图像输入」。活动模型的 `vision` 决定是否在观测消息中附加截图。

提供商表单内提供「模型名称」下拉框与「获取模型列表」按钮：填写 Key 后点击按钮会先保存提供商，再从官方接口拉取模型，选项即为官方模型（含可识图标注），选中后立即切换活动模型。下拉末尾的「使用其他模型」可展开自定义模型 ID 与识图开关。切换厂商或套餐会清空待选列表；协议、Base URL 或模型列表接口变更时，后端会清除旧的官方模型缓存，避免跨端点误选。下方「模型明细」表格用于查看能力、来源与逐模型测试/删除。

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
| 能力契约（7 项，v1.0，带输入/输出 schema） | `src/xm_agent/contracts.py` | 完成 |
| 数据模型（Observation / Action / Receipt / Verification / StepRecord / TaskResult） | `src/xm_agent/types.py` | 完成 |
| 运行时状态机（observe → decide → policy → act → verify → recover） | `src/xm_agent/runtime.py` | 完成 |
| MCP stdio 客户端（JSON-RPC 2.0、超时、stderr 尾部、线程安全关闭） | `src/xm_agent/mcp_stdio.py` | 完成 |
| 能力适配层（契约 → MCP 工具名/参数映射） | `src/xm_agent/adapters/mcp_tool.py` | 完成 |
| Windows-MCP 适配器（Snapshot 文本解析、坐标定位、WaitFor 条件） | `src/xm_agent/adapters/windows_mcp.py` | 完成，真实只读验证通过 |
| Computer-Use MCP 适配器（截图落盘、坐标动作、视觉验证降级） | `src/xm_agent/adapters/computer_use_mcp.py` | 完成，`tools/list` 与截图探测通过 |
| Xshell MCP 适配器（`xshell_exec`、超时换算、屏幕读取） | `src/xm_agent/adapters/xshell_mcp.py` | 框架保留，本机未安装 Xshell |
| Mock 执行器（无副作用，用于联调与测试） | `src/xm_agent/adapters/mock.py` | 完成 |
| 策略引擎（能力白名单、命令前缀、危险模式、路径根、需确认项） | `src/xm_agent/policy.py` | 完成 |
| 租约锁（TTL、陈旧接管、心跳、原子替换） | `src/xm_agent/lock.py` | 完成 |
| 审计日志（JSONL、trace_id、敏感字段脱敏、按天清理接口） | `src/xm_agent/audit.py` | 完成 |
| Provider（脚本化 / 启发式 mock / OpenAI 兼容 / Anthropic Messages，可选视觉消息与 usage 回调） | `src/xm_agent/providers/` | 完成（本地模拟端点验证，真实厂商待联调） |
| 厂商目录与套餐（18 个预设，协议/端点/模型列表风格） | `src/xm_agent/ai/catalog.py` | 完成 |
| DPAPI 密钥加密（固定 entropy，密文入库） | `src/xm_agent/ai/crypto.py` | 完成 |
| 官方模型列表（OpenAI / Anthropic / Gemini / Ollama）与视觉标记 | `src/xm_agent/ai/models.py` | 完成 |
| 活动模型装配、连通性测试、usage 入库 | `src/xm_agent/ai/factory.py`、`src/xm_agent/ai/service.py` | 完成 |
| 配置（TOML，多适配器，工具名与参数重命名，子进程 env/cwd，`model` 回退） | `src/xm_agent/config.py`、`config/agent.example.toml` | 完成 |
| CLI（`run` / `demo` / `gui`，UTF-8 输出，交互确认，自动读取活动模型） | `src/xm_agent/cli.py` | 完成 |
| 装配层（adapter / provider / policy，活动 AI 优先，CLI 与 UI 共用） | `src/xm_agent/bootstrap.py` | 完成 |
| SQLite 用户资料、任务历史、AI 配置与 Token 用量（WAL） | `src/xm_agent/storage.py` | 完成 |
| 本地面板（回环 HTTP、随机 token、静态资源、运行线程、AI 配置与用量 API） | `src/xm_agent/ui/server.py` | 完成 |
| 桌面窗口与启动器（pywebview + WebView2、`start.cmd`） | `src/xm_agent/ui/window.py`、`start.cmd` | 完成，启动前自动初始化资源 |
| 第三方 MCP 资源管理（版本清单、镜像下载、安全解压、venv/npm 构建、检测 UI） | `src/xm_agent/resources/`、`src/xm_agent/cli.py`、`src/xm_agent/ui/` | 完成 |
| 前端页面（总览 / 运行 / AI 模型 / 用量 / 历史 / 设置，事件轮询、饼图与资源悬浮窗） | `src/xm_agent/ui/static/` | 完成 |
| 只读探测脚本（`tools/list`、`Snapshot`、Computer-Use 截图、Xshell 状态） | `scripts/probe_windows_mcp.py`、`scripts/probe_computer_use_mcp.py`、`scripts/probe_xshell_mcp.py` | Windows 端到端只读已验；其余待构建/环境 |
| 测试（pytest，100 项） | `tests/` | 通过 |
| 无依赖假 MCP Server（覆盖 12 个工具，用于协议联调） | `examples/fake_mcp_server.py` | 完成 |

### 6.2 待完成

| 优先级 | 事项 | 出口标准 |
|---|---|---|
| ~~P0~~ | Computer-Use MCP 只读截图探测 | 已完成：`tools/list` 返回 `computer`，截图 1464×823 / 84 KB |
| P0 | Computer-Use 视觉闭环（需视觉模型） | “截图 → 决策 → 坐标动作 → 再截图”跑通，坐标与 DPI 一致 |
| P0 | Windows-MCP 主通道 + 真实模型联调 + 20 任务基线 | 成功率、平均步数、失败分类有数据；真实厂商 Key 打通模型列表与测试请求 |
| P0 | 截图、DPI 缩放、多显示器坐标归一化 | 坐标在主屏/副屏/125%/150% 下一致 |
| P0 | 可复用确认接口（替代 CLI 文本确认） | 运行时只依赖回调，UI 与 CLI 共用；面板当前只支持自动确认/拒绝 |
| P0 | 契约测试集（同一批用例跑所有后端） | 新增后端不改用例即可回归 |
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
| Computer-Use 无元素树，语义验证需视觉模型 | 适配器只能返回“已截图、待视觉判断” | `model.vision=true` 时附加截图；后续接视觉验证回调 |
| UIA 覆盖受页面结构影响：DOM 页面的 `word` 节点会占满 500 节点预算 | 某些页面只解析出 1 个可用元素 | 已过滤 `word/text`；后续提高预算 + 视觉通道兜底 |
| OpenAI / Anthropic / Gemini 尚未用真实 Key 联调；OpenCode 等网关可能无标准模型列表 | 限流、余额与模型下线的错误提示未验证；空列表时模型需手动添加 | GLM Coding Plan 已用真实 Key 验证并通过；界面支持自定义模型与手动视觉标记 |
| DPAPI 密文与 Windows 用户/机器绑定 | 复制数据库后密钥不可解密 | 迁移时要求重新填写 Key；企业环境可替换为 Vault |
| 用量只有 Token，没有金额 | 无法直接看到费用 | 需要时增加各厂商/套餐价格表做换算 |
| 租约锁接管存在极小竞态，无 fencing token | 极端并发下可能双执行 | 单机可接受；多客户端前引入 fencing |
| 审计清理未定时调度 | 长期运行磁盘增长 | 启动时与每日定时执行 prune |
| 策略是白名单式约束，不是沙箱 | 无法阻止白名单内命令的副作用 | 配合低权限账户与人工确认 |
| 自研最小 MCP 客户端 | 协议演进需自行跟进 | 稳定后迁移官方 SDK |
| 尚无桌面安装包 | 已有 pywebview 面板与 `start.cmd`，但没有安装器/托盘/更新器 | 按第 5 章 D0 → D3 推进；页面可复用现有 HTTP API |
| 面板历史不含完整事件 | 重启后只能看任务元数据；完整步骤在审计 JSONL | 后续把关键事件也写入 SQLite 或直接索引审计文件 |
| 设计基线 3.12+，实测环境 3.14；Windows-MCP 要求 3.14+ | 版本矩阵分裂 | Runtime 与 MCP 分 venv，安装器分别打包 |
