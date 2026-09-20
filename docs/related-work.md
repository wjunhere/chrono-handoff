# 相关项目调研

> 调研时间：2026-09-20 · 方法：web 检索（多角度交叉验证）
>
> **重要说明**：以下条目的存在性与主要能力来自公开检索结果，**本项目的定位基于其文档描述**推断；标注 ⚠️ 处为已知局限或证据薄弱点。

---

## A. 请求人类做物理动作的 MCP 服务器

这一类的共同模式：**agent 调用 tool → 弹出 UI/消息 → 阻塞等人做完 → 返回**。

| 项目 | 核心工具 | 实现方式 | 局限 |
|------|---------|---------|------|
| **`human-intervention-mcp`**<br>(mhmdibrahimm) | `request_human_action`<br>`ask_operator` | 起一个 localhost 随机端口的 HTTP 页面；**tool call 一直挂着**直到表单提交或超时，然后销毁页面 | ⚠️ 无队列、无轮询 API、无后台恢复。**无时间语义** |
| **`call-a-human-mcp`**<br>(nishantmodak) | `ask_human`<br>`request_approval` | Slack / Telegram / macOS 弹窗；`threading.Event` + 后台守护线程；超时兜底 | 同上 |
| **`Human-In-the-Loop-MCP-Server`**<br>(GongRzhe) | 文本输入 / 选择 / 确认 | GUI 对话框；5 分钟超时 | 同上 |
| **`human-mcp`**<br>(LIghtJUNction) | 同步 `ask_human`<br>异步 `*_async` + `read_human_replies` | 默认 60s 超时；异步变体支持轮询 | 同上 |
| **`Pushary`** | `ask_user` / `wait_for_answer` | 托管服务 | 文档自陈："模型可以不调用这个工具" ⚠️ |

### ⭐ 关键发现

`human-intervention-mcp` 的文档**明确列举了手动操作场景**：

> logging in, entering a one-time code, approving a browser prompt, completing a CAPTCHA, clicking an interface element, connecting an account, or reviewing a visual result

这些**全都是"开放式人工干预"** —— "我不知道你何时能做完"。

**没有任何一个工具处理"我需要在 T 时刻精确发命令"。**

> **结论：本项目的方向在这一类里是空缺的。**

### ⚠️ 安全警告（来自 `Pushary` 文档）

> "A tool the model chooses to call is a suggestion, and a model that does not want to be interrupted can simply not call it."

即：**agent 可以绕开审批工具**。真正的强制门控必须放在执行前（如 `PreToolUse` hook），而不是靠工具自觉。这对本项目的安全设计（见 `design.md` §8）有直接影响。

---

## B. 倒计时 / 时间触发协调原语

这一类提供**时间语义**，但面向多 agent 协作，未接入物理执行与人类提示。

| 项目 | 语言 | 关键机制 |
|------|------|---------|
| **`T-minus`** | Rust | 倒计时事件 + **quorum（法定确认数）**：agents 确认/推迟，满足 quorum 才在 `tick` 触发。支持 DAG 排序的 campaign |
| **`swarm-tminus`** | Python | 同类原语移植：倒计时+quorum 事件、deadline 树、速率限制、**BPM 自适应时钟**、cron、DAG campaign。仅依赖标准库 |
| **`tminus-dispatcher`** | — | ⭐ **beat 制 cue 系统（1 beat = 500ms 默认）+ 正/零/负偏移** |
| **`ai-intervention-agent`** | Python | 长任务 `interactive_feedback` + **倒计时 + 自动重提交**（默认 240s）抗客户端硬超时；**"typing-hold"（打字可延长倒计时）**；SSE 重连 + 草稿自动保存 |
| **`pi-scheduler`** | — | agent 调度未来 shell 命令/提示（once/interval/cron + `maxRuns` 限界） ⚠️ **仅进程内定时器：Pi 关闭时不触发** |
| **`opencode-agenda-plugin`** | — | 类似的 agent 定时任务插件 |

### ⭐ 最有价值的三条

1. **`tminus-dispatcher` 的 "beat + 偏移"** —— 这是"倒计时"的**严格形式化版本**。不是"5 秒后"，而是"第 10 拍 +2 拍"。本项目的时间模型应支持这种写法（见 `design.md` §3.2）。

2. **`ai-intervention-agent` 的 "倒计时 + 自动重提交"** —— 揭示了**长时等待的工程现实**：客户端的 tool call 有硬超时，所以必须"自动重提交"来维持等待。这与本项目 `case-studies` 里遇到的实际问题同源。

3. **`T-minus` 的 quorum 机制** —— "多个确认者达成一致才触发"。对本项目的启发：**多操作员场景**（如需要两个人同时按不同按钮）需要类似的仲裁机制。

---

## C. 硬件测试框架里的"操作员提示"（同领域，最实用）

**这一类和本项目问题域完全重叠 —— 硬件测试早就有这个刚需。**

| 项目 | 能力 | 亮点 |
|------|------|------|
| **`Jumpstarter`** | 脚本原语：`pause` / `expect` / `send` / **`reset`** | ⭐ **直接把"串口等待 + 复位"做成了脚本步骤** |
| **`litmus`** | `prompt` fixture：`confirm` / `choice` / `input` + `timeout_seconds`；浏览器 UI 或终端降级；`LITMUS_AUTO_CONFIRM=1` 供无头 CI | ⭐ 支持**无头模式自动确认**，是 "human-in-the-loop → CI" 的桥梁 |
| **`Axiom Remote`** | `remote.press` / `remote.turn` / **`remote.reset`** 驱动物理按钮/旋钮 | 物理按钮驱动 |
| **`Hyperion ESS` 测试脚本** | 倒计时 helper + `read -n1` 暂停等操作员，再触发动作 | 最朴素的倒计时实现 |
| **Hyperion / Wokwi** | Wokwi 场景 YAML 用 `set-control` + `delay` 模拟按键 | 仿真环境下的按键时序 |

### ⭐ 关键洞察

`Jumpstarter` 有 `reset` 原语，`Axiom Remote` 有 `remote.reset` —— 说明**"复位设备"在硬件测试里是一等公民操作**。

但它们**都没有和 LLM agent 结合**。这正是本项目的机会：**把硬件测试领域的成熟原语，包装成 LLM agent 能用的工具。**

---

## D. 从物理层消灭这个问题（最彻底）

| 项目 | 做法 | 说明 |
|------|------|------|
| **`PicoHand`** | 一个 Pi Pico，用**开漏 GPIO 模拟 BOOTSEL/RESET 按键**，通过 USB 串口暴露 | ⭐ 文档原话：**"zero human intervention"** |
| **`mcp-kr-controller`** | KR 系列继电器控制器 MCP（串口/TCP/UDP），23 个工具，blink/momentary 模式，DI 触发自动化 | ⭐ 含 **`wait_di_trigger`（上升沿触发后动作）** |
| **`UnitApi/MCP`** | Python 框架：GPIO（LED/按钮/传感器）、实时 GPIO 流、SSH/TCP/WebSocket 远程、仿真模式 | 交通灯 + 行人按钮回调示例 |
| **`MCP-U`（Arduino）** | JSON-RPC 2.0 over 任意 Arduino `Stream`，把 GPIO/PWM/ADC/I2C 暴露为动态发现的 MCP 工具 | 引脚类型直接映射到 `digitalWrite`/`analogWrite` |
| **`mcp-industrial-agent`** | 安全门控 MCP：3 层写保护（显式 `dry_run=False` + 操作员 ID/理由 + 服务级 `ALLOW_WRITES`）+ JSONL 审计 | 默认只读 + mock adapter |
| **`stamplc-mcp-server-io`** | M5StamPLC 的 MCP 服务：继电器控制、传感器读取、SSE 事件、OpenAI Realtime 语音助手 | — |
| **`injectHardwareButton`**（extentos） | 模拟智能眼镜的拍摄键（tap=暂停/继续，hold=停止），用于无人工/无硬件测试 | 虚拟化硬件按钮 |
| **`android-mcp`**（tomdwipo） | `Press-Tool` 按设备/模拟器的硬件键 | — |

### ⭐ 关键洞察

> **`PicoHand` 和 `mcp-kr-controller` 是这个问题的"根治方案"：不要人按，让机器按。**

这直接影响本项目的定位：

```
本组件不是终点，而是"没有硬件执行器时的最佳可用方案"。
```

**一旦有了 GPIO 继电器 / PicoHand 类夹具，本组件的价值就退化为"人类参与的兜底路径"。**

---

## E. 理论根基

### E.1 时间触发 vs 事件触发

| 架构 | 优势 | 代价 |
|------|------|------|
| **事件触发** | 灵活，对异步事件响应快 | **延迟和抖动依赖总线负载** |
| **时间触发** | **时间可预测**；节点同步到全局时钟后延迟/抖动恒定 | 需要时钟同步机制 |
| **混合** | 可为两类任务分别设优先级，在抖动与响应性间权衡 | 复杂度 |

来源：Albert et al.（TUM）、Kopetz、ScienceDirect（混合架构）

> ⚠️ **反直觉警告**（Albert et al.）：**没有时钟同步时，时间触发系统的抖动反而可能比事件触发更差。**

### E.2 异步执行的延迟补偿（机器人学）

一致的观察：**observation–execution gap**（感知 + 推理 + 执行延迟）会导致**过期的动作**。

| 方法 | 思路 |
|------|------|
| **DA-DP** | 把策略条件化在**测得延迟**上，瞄准"执行时刻的状态"而非"观测时刻的状态" |
| **RAPAC-DP** / **ACNet** | 云端/异步执行下的 pending-action 补偿 + 轻量延迟感知适配器 |
| **ARLI** | 异步推理下非马尔可夫状态的 RL 微调 |
| **"Learning to Wait"** | LLM agent 可通过 in-context learning 学会预测 `time.sleep(t)`，而不是盲目轮询 |
| ⚠️ **Async VLA 对比** | 比较发现**没有单一方法占优**；VLASH 的结果依赖"特权未来状态访问"，实际部署表现可能不同 |
| **AgiLimb** / **IHDA** | 机器控制人类驱动的运动的时机（数字反射 ~60ms；EMS 约束释放） |

> **对本项目的启发**：`"Learning to Wait"` 说明 —— **让 LLM 学会"该等多久"是可行的**，但本项目更进一步：**不等，而是提前安排**。★ AgiLimb 的"机器控制人类驱动运动"是本项目的极端形态（机器掌控人的动作时机）。

### E.3 人机节拍同步

| 研究 | 结论 |
|------|------|
| **TANDEM**（Iqbal & Riek, ICRA 2021） | 神经生物学启发的适应/预期算法（dyadic/group），机器人与人类**同步打鼓**，变速下达到人类水平 |
| **Robot Thereminist**（Mizumoto et al., IROS 2010） | 耦合振荡器模型使起始误差降低 **10~38%**（取决于节拍器/人类输入） ⚠️ 节拍波动时性能下降 |
| **Shimon（机器人马林巴）** | **社交头部手势**显著改善变速下的人机同步；单纯手臂手势**无效** |
| **Adaptive Metronome (GEM)** | 25~50% 自适应度增强同步；★ 更高自适应度（70~100%）**只在有听觉反馈时才有帮助** |
| **Bayesian causal inference** | 解释了人类如何整合 vs 分离冲突的节奏线索 |

> ⭐ **最实用的工程结论**：**听觉反馈（beep）是把人的时间误差从 ±150ms 压到 ±50ms 的关键手段** —— 这直接支撑了 `design.md` 里 `cue.audio: "beep"` 的设计决策。

---

## F. 调研总结：本项目的生态位

```
                   时间语义
                      ▲
                      │
   tminus-dispatcher  │  T-minus / swarm-tminus
        (beat+offset) │   (quorum)
                      │
                      │        ★ chrono-handoff
                      │        （时间语义 + 人类提示 + 物理执行）
                      │               ↑ 本项目
  ────────────────────┼──────────────────────────────►  物理执行/人类
                      │
   human-intervention │  Jumpstarter / litmus
     -mcp / human-mcp │    (pause/expect/reset)
   (阻塞等人，无时间)   │
                      │
                      │  PicoHand / mcp-kr-controller
                      │   (不要人，机器直接按)
                      ▼
```

**四象限里，右上角（时间语义 + 人类参与 + 物理执行）是空的。**

这就是本项目要占的位置。

同时必须承认：**右下角（硬件执行器）是长期更优解** —— 当条件允许时，应该优先选择让机器直接动，而不是协调人类。
