# 协议与接口设计

## 1. 设计目标

| 目标 | 说明 |
|------|------|
| **把延迟从关键路径上移走** | 时间敏感的动作由独立进程执行，不经过 LLM turn |
| **让人类可跟上** | 明确的时间锚点（倒计时 + 声音），误差 ≤ ±50ms |
| **对失误有容错** | 窗口化动作、重复尝试、re-arm |
| **闭环可验证** | 传感器回读确认动作真的生效，而不是"我发了命令" |
| **可降级** | 窗口太窄时能切换到硬件执行器 |

## 2. 架构

### 2.1 组件划分

```
┌────────────────────────────────────────────────────────────┐
│  Planner（LLM，慢，5~30s）                                   │
│  ─ 分析现状、决定做什么                                       │
│  ─ **一次性提交** timeline（不阻塞、不等结果）                  │
│  ─ 后续通过事件日志异步获知进展                                │
└───────────────────────┬────────────────────────────────────┘
                        │ submit(timeline)  ← 单向、非阻塞
                        ▼
┌────────────────────────────────────────────────────────────┐
│  Conductor（独立进程，快，ms 级）  ★ 核心                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Clock        │  │ Cue UI       │  │ Actuator         │  │
│  │ 单调时钟       │  │ 倒计时+蜂鸣   │  │ 串口/GPIO/HTTP   │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Sensor       │  │ State machine│  │ Event log        │  │
│  │ 串口/GPIO 回读 │  │ re-arm/超时   │  │ 时间线+结果       │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└───────────────────────┬────────────────────────────────────┘
                        │ observe()  ← 异步读事件日志
                        ▼
                    Planner（下一轮）
```

### 2.2 关键约束（照抄 Two-Rate 模式的"禁止事项"）

> **Conductor 绝不阻塞等待 Planner。** 没有新 timeline 时，继续执行当前 timeline，或回落到安全状态（如保持在 PMON 提示符）。

这条约束是**实现上的硬要求**，不是建议。

## 3. 时间模型

### 3.1 时钟选择

```python
# ❌ 墙钟：会被 NTP 调整、可能跳变
time.time()

# ✅ 单调时钟：只前进，不受系统时间调整影响
time.monotonic()
```

**必须用单调时钟。** 倒计时期间若发生 NTP 校时，墙钟可能导致动作提前或推迟。

### 3.2 时间表示

```
绝对时刻：t0 + offset
          t0 = cue_origin（倒计时真正开始的那一刻，单调）
          ⚠️ 不是 tool call 收到的时刻：人可能 40 秒后才回到工位，
             锚提交时刻会让倒计时凭空消失
相对偏移：T+8s                 （人类友好）
窗口：    [T+8s, T+11s]        （容错区间）
节拍：    beat 10 + 2 beats     （tminus-dispatcher 风格，1 beat = 500ms）
```

**建议同时支持"秒偏移"和"节拍"两种写法**：节拍形式对音乐性/周期性任务更自然，也便于人类跟随。

### 3.3 时间精度的分配

| 环节 | 精度要求 | 实现手段 |
|------|---------|---------|
| Conductor 内部计时 | ±1 ms | 单调时钟 + 紧凑循环（不做阻塞 IO） |
| 执行器发送动作 | ±1 ~ 10 ms | 预创建连接、避免动态分配 |
| **人类跟节拍** | **±50 ~ 200 ms** | **倒计时 + 蜂鸣**（视觉+听觉双通道） |
| 传感器回读确认 | ±10 ms | 非阻塞读 + 环形缓冲 |

**瓶颈永远是人类那一环。** 所以工程重心应该放在"如何让人更准"，而不是把机器做到纳秒级。

## 4. 接口定义

### 4.1 `sync_action` —— 提交一次同步动作

```jsonc
sync_action({
  // ---- 给人类看的 ----
  "instruction": "按一下开发板上的 RESET 键",
  "countdown_seconds": 5,
  "cue": {
    "visual": "countdown",       // 大号倒计时
    "audio": "beep",             // ★ 声音（把误差从 ±150ms 压到 ±50ms）
    "haptic": null               // 可选：震动（移动端）
  },
  "human_action_at": "T+5s",     // 人类应当在此时刻动作

  // ---- 机器侧：等待物理世界的信号 ----
  "expect_signal": {
    "source": "serial:COM8",
    "pattern": "PMON2020|command-line",
    "timeout_s": 60,
    "on_timeout": "rearm"        // rearm | abort | continue
  },

  // ---- 机器侧：在窗口内执行的动作 ----
  "on_window": {
    "start": "T+8s",
    "end": "T+11s",
    "interval_ms": 300,          // 窗口内重复尝试（人给锚点，机器扫射）
    "actions": [
      { "type": "serial_write", "target": "COM8", "data": "c" }
    ]
  },

  // ---- 验证 ----
  "verify": {
    "source": "serial:COM8",
    "pattern": "PMON>",
    "timeout_s": 20
  }
})
```

### 4.2 返回值：事件时间线（不是"请等待"）

```jsonc
{
  "status": "satisfied",           // satisfied | timeout | aborted | partial
  "verdict": "ok",                 // 结论：ok | signal_missing | unverified | cancelled | error
  "verdict_reason": "verify",      // 内部原因（human_signal / no_signal / panel_cancel …）
  "t0": 1234567.890,               // 单调时钟基准
  "events": [
    { "t": 0.000,  "kind": "timeline_submitted" },
    { "t": 0.001,  "kind": "cue_started",     "detail": "countdown 5s + beep" },
    { "t": 5.001,  "kind": "cue_zero" },
    { "t": 7.842,  "kind": "signal_matched",  "detail": "PMON2020 ..." },   // ★ 人的动作被观测到
    { "t": 8.001,  "kind": "window_open" },
    { "t": 8.003,  "kind": "action_sent",     "detail": "serial_write c" },
    { "t": 8.301,  "kind": "action_sent",     "detail": "serial_write c" },
    { "t": 8.524,  "kind": "verify_matched",  "detail": "PMON>" },          // ★ 闭环成功
    { "t": 8.525,  "kind": "window_closed",   "detail": "early_exit" }
  ],
  "human_offset_ms": -42,          // ★ 实测：有符号。负=人类提前，正=晚
                                    //   拍点同步下通常为负（NMA）
  "artifacts": { "serial_log": "logs/sync_001.log" }
}
```

**关键字段 `human_offset_ms`**：有符号（负=提前）。这个值被记录并用于**自适应校准**。

> 方向很重要：如果实测下来人平均**提前**，那么"下次倒计时提前这么多"就是**错的**，应该**推迟**。
> 这正是它不能叫 `latency` 的原因 —— latency 暗示恒正。见 `theory.md` §2.2 的 NMA 说明。

**用法**：下一轮把 `offset_s = 上一轮 median(human_offset_ms)/1000` 提交，
由于 `zero_at = target_at - offset_s`，cue 会朝反方向偏移，人机在 `target_at` 相遇。
注意 offset 只移动**人类 cue**；机器动作始终锚在 `target_at`（`calibrate.py` 规则 5）。

### 4.3 辅助接口

```jsonc
// 只提示人类动作，不执行任何机器动作（纯协调）
notify_user({ "instruction": "...", "countdown_seconds": 5, "expect_ack": true })

// 等一个物理信号出现（不提示人类）
await_signal({ "source": "gpio:17", "edge": "rising", "timeout_s": 30 })

// 查询/取消正在进行的同步
sync_status({ "id": "sync_001" })
sync_cancel({ "id": "sync_001" })
```

## 5. 状态机

```
        ┌──────────┐
        │  IDLE    │
        └────┬─────┘
             │ submit(timeline)
             ▼
        ┌──────────┐   cue 播放中
        │ CUING    │──────────────────┐
        └────┬─────┘                  │
             │ cue_zero (T+5s)        │ 人类提前动作？
             ▼                        │
        ┌──────────┐                  │
        │ WAITING  │◄─────────────────┘
        │ (等信号)  │  信号可能早于/晚于 cue_zero
        └────┬─────┘
             │ signal_matched
             ▼
        ┌──────────┐
        │ ACTING   │  窗口内重复尝试
        └────┬─────┘
             │ verify_matched  → SATISFIED ──► 结束
             │ window 结束未验证 → PARTIAL ──► 可 re-arm
             │ 信号超时            → TIMEOUT ──► re-arm / abort
             └──────────────────────────────────
```

### 5.1 重要设计：不依赖 cue_zero 触发

状态机**不是**"cue 归零 → 开始行动"，而是"**等信号**"。

原因：人类可能**提前**动作（听错节拍、手快）。如果机器严格等 cue_zero，就会错过。

正确做法：**从 cue 开始播放的那一刻起就持续监听信号**，无论信号何时到来都能接住。

### 5.1.1 `status` 与 `verdict`：状态名不是结论

`status` 是上面这台状态机的终态，它必须稳定（改名等于改契约）。但它的字面意思
会误导人：一个 `expect_signal.source == "none"` 的 cue 本来就不要求人做任何可观测的
动作，`duration_s` 到点后它必然记成 `TIMEOUT`——而那是这条 cue 的**成功**终态。

所以快照里另有一个 `verdict`：

| `status` | `verdict` | 含义 |
|---|---|---|
| `SATISFIED` | `ok` | 观测到人的动作（或 verify 通过） |
| `TIMEOUT` + 这条 run 等信号 | `signal_missing` | 人没做，或没被观测到——失败 |
| `TIMEOUT` + 这条 run 不等信号 | `ok` | 提示期正常结束——成功 |
| `PARTIAL` | `unverified` | 收到信号但未确认生效 |
| `ABORTED` | `cancelled` | 被取消 |
| `ERROR` | `error` | 内部故障 |

`verdict_reason` 给出内部原因（`human_signal` / `no_signal` / `panel_cancel`…）。
调用方分支判断请用 `verdict`。

### 5.2 re-arm（重装填）

```
信号超时 → 重新播放倒计时 → 人类再试一次
```

要有**最大重试次数**（避免无限循环），以及**指数退避**（如果是设备还没准备好）。

### 5.3 多条 cue：串行而不是叠加

人只有一双眼睛，两条同时跑的 cue 不是两个提示——第二个会把第一个从屏幕上顶掉，
而第一个人还在跟着一个已经变了的节拍数拍子。所以后到的 cue 默认**排队**：

```
cue A 提交        → RUNNING，占屏幕
cue B 提交        → PENDING，queued_behind = A
A 的刺激播完      → B 自动 go()，接屏
```

三个细节：

- 释放时机是**刺激结束**（最后一个 tail 拍），不是 verdict。verdict 早于 tail 好几秒，
  那时开始下一条会把两串拍子叠在一起。
- 链条而非扇入：C 排在 B 后面，而不是也排到 A 后面（否则 A 结束时只释放一个，另一个永远
  PENDING）。
- 只对 `audio or visual` 的 plan 生效：机器侧独占的动作不会互相遮挡，不该被排队拖慢。

`queue=false` 保留真正需要并行的场景（一人一条 cue）。面板上的「立即开始」按钮同样受此约束。

## 6. 降级策略：窗口太窄时

```
if window_ms >= 500:
    方案 = "人类 + 倒计时"                 # 本组件的主场
elif window_ms >= 50:
    方案 = "人类 + 倒计时 + 高频扫射"        # 极限，需要训练过的操作员
else:
    方案 = "硬件执行器"                     # ❌ 不要人
              ├─ GPIO 继电器（控制 Reset / 电源）
              ├─ PicoHand 类夹具（模拟按键）
              └─ 机械臂 / 电磁铁
```

**这是本项目的边界所在。** 超过这个边界的场景，正确做法不是"把人训练得更快"，而是**换执行器**。

## 7. 持久化与恢复（生产环境考量）

搜索结果里反复出现的一条警告：

> 进程内阻塞等待在生产中是 anti-pattern：重启/扩缩容/部署会丢失内存状态、可能重复恢复、没有审计追踪。

推荐模式：

```
1. 把 timeline 持久化到 checkpoint（DB / 文件）
2. 进程可以退出
3. 外部事件（信号到达 / 超时）触发恢复
4. 从持久化记录恢复，幂等推进
```

**对这个项目的建议**：

| 场景 | 方案 |
|------|------|
| 本地开发会话 | ⚠️ **仍然用 daemon**：进程内会随 Pi 会话/配置重载而死（case study §5.3 已实测） |
| 长任务 / 无人值守 | 必须持久化 timeline + 可恢复 |

## 8. 安全考量

| 风险 | 对策 |
|------|------|
| Agent 让人类做危险动作 | 人类侧显式确认（`request_approval` 式门控）；动作白名单 |
| Agent 忘了自己在等什么 | 事件日志持久化 + 超时兜底 |
| 人类做了但 agent 没看见 | 传感器回读 + re-arm（不能只依赖"人类说完成了"） |
| 重复执行导致副作用 | 动作**幂等化**设计；带序号的状态推进 |
| 提示被忽略（用户离开） | 超时后明确结束，而不是静默挂住；`verdict` 说明这次到底算不算成功 |
| 两条 cue 同时上屏 | `queue` 默认串行化，后一条等前一条的拍子播完 |

## 9. 与现有实现的关系

本项目**不是**要取代 `human-intervention-mcp` 这类工具，而是补上它们缺的维度：

```
human-intervention-mcp  :  "请你做 X，做完了告诉我"          ← 无时间语义
chrono-handoff          :  "请在 T+5s 做 X，我会在 T+8s 行动"  ← 有时间语义
```

两者可以组合：`chrono-handoff` 负责时间敏感的协调，`human-intervention-mcp` 负责开放式的人工干预请求。

## 10. 实现状态（M1 已完成）

> 实现与本文档的**偏离项**（`t0` 的定义、`submit`/`go` 的拆分、`expect`/`verify` 的职责边界、
> Two-Rate「保持旧计划」的放弃、GIL 约束）集中记录在
> [`implementation-notes.md`](implementation-notes.md) §5 —— 那里是**实测之后**的版本，
> 与本文档冲突时以那份为准。

### 10.1 已实现

| 模块 | 文件 | 状态 |
|------|------|------|
| 单调时钟 + 亚毫秒落点 + 无漂移网格 | `clock.py` | ✅ 实测落点 0.015~0.285 ms |
| 计划 / 事件数据模型 | `model.py` | ✅ |
| 非阻塞音频 + 终端倒计时 | `cue.py` | ✅ 稳态调用 0.36 ms |
| 观测源（按键/stdin/http/串口/合成） | `inputs.py` | ✅ |
| 自适应校准（median+MAD+夹紧） | `calibrate.py` | ✅ 抗 5000ms 异常值 |
| 执行器抽象（人/串口/HTTP/日志） | `effectors.py` | ✅ |
| Conductor（网格/窗口/闭环/日志） | `conductor.py` | ✅ |
| 常驻 daemon + 单实例保护 | `daemon.py` | ✅ |
| MCP 表面（瘦客户端）+ IPC | `mcp_server.py` | ✅ |
| M1 实验 runner | `probe.py` | ✅ |
| CLI（doctor/selftest/probe/…） | `cli.py` | ✅ 自检 10/10 |

### 10.2 MVP 清单（原始）

- [ ] 单调时钟 + timeline 解析器（支持 `T+Ns` / 窗口 / 节拍）
- [ ] 倒计时 UI（终端大号数字 + ASCII 进度条）
- [ ] 声音提示（`winsound.Beep` / `\a` / 系统提示音）
- [ ] 串口执行器（复用已有 `pyserial` 代码）
- [ ] 串口传感器（非阻塞读 + 多模式匹配）
- [ ] 窗口内重复执行
- [x] 事件日志（JSON Lines）+ `human_offset_ms` 记录  ← M1 已实现
- [ ] 状态机 + re-arm
- [x] 单调时钟 + timeline 解析器  ← M1（`clock.py`）
- [x] 倒计时 UI（终端大号数字 + 进度条）← M1（`cue.py`）
- [x] 声音提示（预渲染 WAV + `PlaySound(SND_ASYNC)`，非阻塞）← M1
- [x] 串口执行器 / 观测源  ← M1（`effectors.py` / `inputs.py`，未接实机）
- [x] 窗口内重复执行（丢弃而非爆发）← M1
- [x] 事件日志（JSON Lines）+ `human_offset_ms` 记录  ← M1 已实现
- [x] 状态机 + re-arm（超时返回 `timeout`/`partial`，不再挂起）← M1
- [x] **自适应校准**：用历史 `human_offset_ms` 自动调整倒计时提前量  ← M1 已实现，合成人类 residual 0.0ms
- [x] 包装为 MCP server（`submit`/`observe`/`wait`/`cancel`/`status`/`calibrate`）← M1，**全部非阻塞语义**
- [ ] 接实机（M2）→ MCP Apps 倒计时 UI（M3）→ 硬件执行器（M4）→ 安全门控（M5）
- [ ] （可选）包装为 MCP server

**其中"自适应校准"是最有价值的一步**：第一次合作后，机器就学到了"这个人平均晚 200ms 按"，从此人机可以真正做到同时到达目标时刻。
