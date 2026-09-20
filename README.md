# chrono-handoff

> **时间锚定的人机动作交接** —— 让 AI agent 与人类在物理世界的动作上精确对齐

## 一句话

Agent 无法直接改变物理世界，而人类的动作延迟不可预测 —— 传统做法是"人做完 → 通知 agent → agent 反应"，这个链路慢到注定失败（LLM 一轮推理 5~30 秒）。**本项目的核心主张是：把耦合方式从"事件触发"改为"时间触发"** —— agent 提前提交一份带时刻的行动计划，由独立的高速进程精确执行，倒计时只是给人类的时间锚点，传感器回读用来闭环确认。

---

## 一、问题

### 1.1 根因：耦合方式错了

| 耦合方式 | 机制 | 结局 |
|---------|------|------|
| **事件触发**（现状） | 人类动作 → 通知 agent → agent 推理 → agent 发命令 | ❌ **必然失败** |
| **时间触发**（本项目） | 双方预先约定时刻 → 各自在 T 时刻行动 | ✅ 延迟被"提前约定"抵消 |

事件触发链条里的致命数字：

| 环节 | 耗时 |
|------|------|
| LLM 一次推理 turn | **5 ~ 30 秒** |
| 人类执行一个物理动作（按按钮） | 0.1 ~ 1 秒 |
| 物理世界留给你的窗口（例：PMON 引导菜单） | **3 秒** |

**LLM 的反应时间比物理窗口长一个数量级。** 这不是"优化一下就好"的问题，是架构性的不匹配。

### 1.2 真实案例（实机踩坑记录）

详见 [`case-studies/loongson-2k1000.md`](case-studies/loongson-2k1000.md)。

龙芯 2K1000LA 开发板的 PMON 引导程序 `bootdelay = 3`，只有 3 秒窗口能按 `c` 中断默认启动进入引导命令行。实际记录：

```
用户按 Reset
  ↓
板子启动（如果 3 秒内没按到 c，就启动到 Loongnix，窗口永久丢失）
  ↓
agent 通过串口发现板子已启动       ← 已经过去 30+ 秒
  ↓
agent 推理"我该做什么"             ← 又过去 5~30 秒
  ↓
❌ 太晚了
```

**结论**：agent 永远不可能"及时反应"于一次物理动作。

---

## 二、核心洞察

### 2.1 把不确定性搬到各自擅长的域

| 主体 | 擅长的 | 不擅长的 |
|------|--------|---------|
| **人类** | 跟随节拍（±50ms 级，尤其配声音提示） | 记住精确时刻、长时间保持警觉 |

> ⚠️ "±50ms" 来自反应时范式的文献值，本项目自己的 M1 实测待跑（见 [`docs/m1.md`](docs/m1.md)）。
| **机器** | 精确定时（±1ms 级）、无条件重复 | 理解意图、临场决策 |

所以：**让 LLM 提前决策（它擅长），让定时器掌控时刻（它擅长），让人跟随节拍（他擅长）。**

### 2.2 关键修正：倒计时不是给 LLM 看的 ⚠️

这是最容易做错的地方：

```
❌ 直觉版（错的）：
   倒计时归零 → LLM 收到通知 → LLM 思考(5-30s) → 发命令
   ← 延迟原封不动地回来了！

✅ 正确版：
   LLM 提前提交计划(timeline) ──► 独立定时进程 ──► 归零时立即发命令(ms 级)
                                      │
                                      └──► 用户看倒计时 → 归零时按 Reset
```

> **这个组件必须是一个独立于 LLM turn 的进程。** 漏掉这一点，倒计时只是个装饰品。

### 2.3 这个模式有正式名字

**`Two-Rate Cloud-Brain / Edge-Controller Split`**（Agent Patterns Catalog）

原文定义（精准命中本场景）：

> **A single model cannot be both the deliberate planner and the real-time motor loop.**
>
> 慢规划器（大模型，几 Hz）输出紧凑的 latent plan；快控制器（小模型，50~100 Hz）读当前 plan + 传感器，以原生控制率生产动作，**永不阻塞等待慢规划器**。

它明确写出的**禁止事项**：

> The fast controller **may not block waiting on the slow planner**; it must act on the last posted latent plan, and when no fresh plan has arrived it must **keep tracking the previous one or fall back to a safe hold rather than stall**.

#### ⚠️ 本项目必须修正它的一半（实现后确认）

这条规则的后半句 —— "无新计划时**继续跟踪旧计划**" —— **在本项目里不成立**。

原模式里"旧计划"是电机指令，重放它顶多多走一格；本项目里快控制器的输出端挂着**人**，而"上一个人类动作"就是"上一次按了 Reset"。重放它的语义是**让这个人再按一次复位键** —— 完全不同的物理事件，可能造成真实损坏。

所以本项目对 Two-Rate 是**选择性继承**：

| 规则 | 本项目 |
|------|--------|
| 不阻塞等待慢规划器 | ✅ 完全继承（这是整个项目的立足点） |
| 无新计划时跟踪旧计划 | ❌ **改为 safe hold**：提示人类"停止动作，等待新指令" |
| 回落到安全状态 | ✅ 继承 |

代码里这条以 `HumanEffector.caps().repeatable = False` 明确表达，而串口/GPIO 等机器执行器允许重放 —— 这恰好就是"在窗口内扫射"与"重放人类动作"的本质区别。

---

## 三、架构

### 3.1 三层职责分离

```
┌──────────────────────────────────────────────────────┐
│  第一层：慢规划器（LLM，5~30s 一次）                    │
│  职责：理解意图、分析现状、决定做什么、**提前提交计划**   │
│  输出：一份声明式的 timeline（时刻, 动作）序列           │
└────────────────────────┬─────────────────────────────┘
                         │ timeline（一次性提交，不阻塞）
                         ▼
┌──────────────────────────────────────────────────────┐
│  第二层：快控制器（独立进程，ms 级）  ★核心             │
│  职责：渲染倒计时 UI / 声音提示 / 按时刻精确执行动作      │
│  约束：**绝不阻塞等待 LLM**；窗口内重复尝试；缺计划时保持 │
└────────────────────────┬─────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌─────────────────────┐      ┌─────────────────────────┐
│  人类（±200ms 抖动） │      │  物理世界（串口/GPIO/网） │
│  倒计时归零 → 按按钮  │      │  传感器回读 → 闭环确认    │
└─────────────────────┘      └─────────────────────────┘
```

### 3.2 映射到本项目的实践

| Two-Rate 模式要素 | 本项目的对应物 |
|------------------|--------------|
| 慢规划器（云端大模型） | **LLM agent** |
| 快控制器（板上小模型） | **`boot_capture2.py`**（Python，0.3s 粒度轮询） |
| 物理身体 | 开发板 + 串口 + **操作员的手** |
| latent plan | 引导命令序列 |
| "不许阻塞等待" | 实测教训：**"猜时间等待"失败 → 改成抢占轮询** |
| "缺计划时保持旧计划" | ❌ **本项目不继承**：人类执行器不可重放，改为 safe hold（见 §2.3 修正） |

---

## 四、协议设计

### 4.1 接口形态（草案）

```jsonc
sync_action({
  "instruction": "按一下开发板上的 RESET 键",
  "countdown_seconds": 5,
  "cue": "beep",                                  // ★ 声音提示（见 §5.2 的实证依据）

  "expect_signal": {                              // ★ 闭环：等物理世界确认
    "serial": "COM8",
    "pattern": "PMON2020|command-line",
    "timeout": 60
  },

  "on_window": {                                  // ★ 提前注册的行动计划
    "start": "T+8s",
    "end": "T+11s",
    "interval_ms": 300,
    "actions": ["send c"]
  },

  "verify": {                                     // ★ 验证动作是否真的生效
    "serial": "COM8",
    "pattern": "PMON>",
    "timeout": 20
  }
})
// 返回：事件时间线 + 验证结果，而不是"请等待"
```

### 4.2 与"阻塞式等人"的根本区别

| 维度 | 现有 HITL MCP 工具 | chrono-handoff |
|------|-------------------|----------------|
| 解决的问题 | "我不知道你**何时做完**" | "我需要在**精确时刻**发命令" |
| 时间语义 | 无 | 有（时刻 + 窗口 + 偏移） |
| 执行主体 | LLM（阻塞在 tool call 上） | **独立进程** |
| 计划提交 | 事后 | **事前** |
| 动作触发 | 人类完成时 | **时间到达时**（不等人类） |
| 闭环 | 人类点击"完成" | **传感器回读验证** |

---

## 五、四条设计原则

### 原则 1：倒计时必须配"预先提交的行动计划"

LLM 在倒计时**开始之前**就把 `(时刻, 动作)` 序列注册好。这是整个设计的立足点。

### 原则 2：给"窗口"，不给"单点"

```
❌ at: "T+5.000s"                        窗口 0ms，人一抖就废
✅ at: "T+5s", window: "T+5s..T+8s"      留 3 秒容错
```

**人给时间锚点，机器在窗口内扫射** —— 两者互补，而不是让任一方承担全部精度要求。

实测有效做法：0.3 秒间隔持续抢占（75 秒内约 250 次尝试），把 3 秒窗口的命中变成必然。

### 原则 3：必须有反馈闭环

不能只"提示"，还要**验证动作真的发生了**：

```
提示用户按 Reset → 倒计时 → 用户在 T+5s 按
                              ↓
                    从串口检测到 "command-line"   ← 确认成功
                              ↓
                    若 60s 内未检测到 → 重装填(re-arm) 或报错
```

### 原则 4：按窗口量级选层，不要用一种方案打天下

| 窗口宽度 | 方案 | 人能承担吗 |
|---------|------|-----------|
| **秒级**（如 PMON 的 3s） | 倒计时 UI + 抢占式容错 | ✅ 可以 |
| **毫秒级** | 必须上硬件（GPIO 继电器 / PicoHand 类夹具） | ❌ 不能，人做不到 |
| **任意** | 把关键动作设计成**幂等 + 可重试** | —— 让"错过"不致命 |

---

## 六、理论依据

### 6.1 时间触发 vs 事件触发

| 架构 | 优势 | 代价 |
|------|------|------|
| 事件触发 | 灵活，对异步事件响应快 | **延迟和抖动依赖总线负载** |
| 时间触发 | **时间可预测**，同步到全局时钟后延迟/抖动恒定 | 需要时钟同步机制 |

> ⚠️ **一条必须知道的反直觉警告**（Albert et al., TUM）：
> **没有时钟同步时，时间触发系统的抖动反而可能比事件触发更差。**
>
> 这直接决定本方案成败：**倒计时的全部价值，取决于"人机共享时钟的精度"**。

### 6.2 人类的定时精度（决定倒计时怎么设计）

| 提示方式 | 人的时间误差 |
|---------|-------------|
| 纯视觉倒计时 | ±100 ~ 200 ms |
| **加声音提示（beep）** | **±50 ms 左右** |

实证研究支持：

- **Adaptive Metronome (GEM)**：25~50% 自适应度增强同步；★ 更高自适应度（70~100%）**只在有听觉反馈时才有帮助**
- **TANDEM**（ICRA 2021）：神经生物学启发的适应/预期算法，机器人与人类**同步打鼓**，变速下达到人类水平
- **Robot Thereminist**：耦合振荡器模型使起始误差降低 **10~38%**
- **Shimon（机器人马林巴）**：**社交头部手势**显著改善变速下的人机同步（单纯手臂手势无效）

> **工程结论**：`cue: "beep"` 是**默认**手段 —— 但理由不是"视觉做不到"（周期性的视觉节拍同样能进入 anticipation 模式），而是**听觉不占用眼睛**：目标场景里操作员正盯着串口输出，视觉 cue 会与被观测对象抢同一个通道，听觉是平行通道。
>
> 所以声音不是必须的。助听/噪音环境可用视觉（需强 onset 的样式，见 [`docs/m1.md`](docs/m1.md) §4.5），不能看也不能听时用**触觉**（手机震动在同步精度上与听觉相当）。
>
> 上面的"±150ms → ±50ms"是 reaction-time 范式的文献值，本项目自己的数值待 M1 实测（见 [`docs/m1.md`](docs/m1.md)）。

---

## 七、相关项目（调研摘要）

完整调研见 [`docs/related-work.md`](docs/related-work.md)。

| 类别 | 代表项目 | 与本项目的差距 |
|------|---------|--------------|
| **A. 请求人类做物理动作的 MCP** | `human-intervention-mcp`、`call-a-human-mcp`、`human-mcp` | ❌ **无时间精确性**，只解决"不知你何时做完" |
| **B. 倒计时 / 时间触发原语** | `T-minus`、`swarm-tminus`、`tminus-dispatcher`（beat + 偏移） | 面向多 agent 协作，未接入物理执行与人类提示 |
| **C. 硬件测试框架的操作员提示** | `Jumpstarter`（`pause`/`expect`/`send`/**`reset`**）、`litmus` | ⭐ **同领域刚需**，但未与 LLM agent 结合 |
| **D. 从物理层消灭问题** | `PicoHand`（GPIO 模拟 RESET 键，zero human intervention）、`mcp-kr-controller`（`wait_di_trigger`）、`UnitApi/MCP` | **最彻底**：不要人按，让机器按 |
| **E. 理论** | Kopetz / Albert：时间触发 vs 事件触发；GEM / TANDEM：人机节拍同步 | 提供依据，未工程化 |

> **本项目的定位**：把 **A 的"人类接口" + B 的"时间语义" + C 的"硬件领域原语" + E 的"理论"** 缝合成一个给 LLM agent 用的组件，并明确 **D 是它的极限替代方案**。

---

## 八、目录

```
chrono-handoff/
├── README.md                       ← 本文件：核心想法
├── pyproject.toml
├── src/chrono_handoff/             ← ★ M1 已实现，可直接运行
│   ├── clock.py                    ← 单调时钟 + 亚毫秒 spin 落点 + 无漂移网格
│   ├── model.py                    ← CuePlan / SignalSpec / WindowSpec / Event
│   ├── cue.py                      ← 非阻塞音频（预渲染 WAV + PlaySound ASYNC）+ 终端倒计时
│   ├── inputs.py                   ← 观测源：按键 / stdin / http / 串口 / 合成
│   ├── calibrate.py                ← 人类偏移估计（median+MAD+夹紧，抗异常值）
│   ├── effectors.py                ← 执行器抽象：人 / 串口 / HTTP / 日志
│   ├── conductor.py                ← ★ 快控制器：网格、窗口、闭环、事件日志
│   ├── daemon.py                   ← 脱离 MCP 生命周期的常驻进程
│   ├── mcp_server.py               ← MCP 表面（瘦客户端）+ IPC
│   ├── webui.py                    ← ★ 提示面板服务端（HTTP + 设置 API）
│   ├── window.py                   ← ★ 独立桌面窗口（WebView2 + 原生窗口控制）
│   ├── winshell.py                 ← Windows 11 原生悬浮边框（圆角/亚克力/主题描边）
│   ├── remote.py                   ← 跨进程 conductor 代理（窗口作纯显示器）
│   ├── settings.py                 ← 设置持久化（损坏也不影响出提示）
│   ├── statics/panel.html          ← ★ 单文件前端：精确音频 + 响应式 UI + 自绘窗口把手
│   ├── analyze.py                  ← 离线分析 journal
│   ├── probe.py                    ← M1 实验协议：拍点栅格 + 偏移统计
| `cli.py`                      ← `python -m chrono_handoff.cli`（doctor/probe/selftest/daemon/…）
├── docs/
│   ├── component.md                ← ★ 交互式提示组件：怎么用、怎么接
│   ├── m1.md                       ← ★ 怎么跑 M1 真实验、怎么读结果
│   ├── implementation-notes.md     ← ★ 实现后才知道的坑与实测数字
│   ├── problem.md                  ← 问题定义与量化
│   ├── design.md                   ← 协议与接口详细设计
│   ├── related-work.md             ← 相关项目调研
│   └── theory.md                   ← 理论根基
└── case-studies/
    └── loongson-2k1000.md          ← 实机案例：龙芯开发板 PMON 引导
```

---

## 九、一句话总结

> **Agent 不该"及时反应"，应该"提前安排"。**
> 把人类当成一个有抖动、需要显式提示、**但偏移可学习**的执行器，纳入一个时间触发的协调协议 —— 这是本次交互中浮现出的核心想法。

---

## 十、现在可以跑什么

**交互式提示组件已可用** —— 独立桌面窗口（无边框/圆角/毛玻璃/置顶/可拖动缩放/自定义设置）或浏览器面板。
**接入 agent 的三步教程见 [`docs/agent-setup.md`](agent-setup.md)**；组件细节见 [`docs/component.md`](docs/component.md)。
详见 [`docs/component.md`](docs/component.md)。

```bash
cd path/to/chrono-handoff && export PYTHONPATH=src

# ★ 推荐：conductor 常驻 + 独立窗口只作显示器（关掉窗口 cue 照样打）
python -m chrono_handoff.cli daemon --background
python -m chrono_handoff.cli window --attach 8791

# 一体化（一个进程同时是 conductor + 窗口；适合演示）
python -m chrono_handoff.cli daemon --window --background

# 浏览器面板（也能手机同局域网用）
python -m chrono_handoff.cli demo --paused
```

M1 测量装置也已实现，自检 10/10 通过。

```bash
cd path/to/chrono-handoff && export PYTHONPATH=src

# `chrono` 是 `pip install -e .` 之后才存在的命令；
# 没安装时用 `python -m chrono_handoff.cli`（下面两种写法等价）
python -m chrono_handoff.cli doctor                 # 环境自检（console / 音频 / 时钟 / 串口）
python -m chrono_handoff.cli selftest               # 验证机器：10 项，含 3 条 bug 回归测试
python -m chrono_handoff.cli probe --trials 30 --conditions both,visual   # ★ 真实验

# 想直接用 `chrono` 命令：
pip install -e .            # 然后 `chrono doctor` 即可
```

先读 [`docs/m1.md`](docs/m1.md)（怎么跑、怎么读结果），
再看 [`docs/implementation-notes.md`](docs/implementation-notes.md)（实现后才知道的坑与实测数字）。

---

## 许可

代码：MIT。`src/chrono_handoff/statics/fonts/` 内的字体文件（Inter、JetBrains Mono）遵循
[SIL Open Font License 1.1](https://openfontlicense.org/)，允许再分发与捆绑。
