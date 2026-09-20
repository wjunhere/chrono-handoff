# 理论根基

> 本文件回答"为什么这样做是对的"，并为设计决策提供依据。

---

## 1. 时间触发 vs 事件触发

这是实时系统领域的经典二分法，也是本项目的理论起点。

### 1.1 定义

| 架构 | 触发条件 | 类比 |
|------|---------|------|
| **事件触发（Event-Triggered, ET）** | 外部事件到来时立即响应 | 门铃响了去开门 |
| **时间触发（Time-Triggered, TT）** | 到达预定的时刻时行动 | 每天 7:00 起床 |

### 1.2 特性对比

| 维度 | 事件触发 | 时间触发 |
|------|---------|---------|
| 灵活性 | **高** —— 能响应突发、异步事件 | 低 —— 只按计划行动 |
| 响应速度 | 对未预期事件快 | 对计划外事件无响应 |
| **延迟可预测性** | **差** —— 依赖总线负载、竞争 | **好** —— 同步到全局时钟后延迟/抖动恒定 |
| 抖动来源 | 队列长度、优先级反转、负载波动 | 时钟同步精度 |
| 资源利用 | 按需 | 可能浪费（空转等待） |
| 前提条件 | 无 | **必须有时钟同步机制** |

来源：Kopetz；Albert et al.（TUM）；混合架构研究（ScienceDirect）

### 1.3 ⚠️ 关键的反直觉结论

> **没有时钟同步时，时间触发系统的抖动反而可能比事件触发更差。**

来源：Albert et al., *Time-Triggered vs Event-Triggered*（TUM）

**为什么？** 时间触发的前提是"双方对 T 时刻的理解一致"。如果时钟不同步：

- 事件触发：延迟 = 处理时间（有界，可能较大）
- 时间触发：延迟 = **时钟偏差 + 处理时间**（偏差可能无限增长）

### 1.4 对本项目的直接推论

```
倒计时的全部价值 = 人机共享时钟的精度
```

如果一个"倒计时 5 秒"的提示，人类理解成 5 秒，而机器按 5 秒执行，但：

- 人类的"5 秒"实际是 5.0 ± 0.15 秒（纯视觉）
- 机器的"5 秒"是 5.000 ± 0.001 秒
- 人类的**感知延迟**（看到归零 → 大脑处理 → 手指动作）额外 100~250ms

那么即时机器做到纳秒级，**整体精度仍由人类决定**。

> **所以：本项目的工程重心不是"把机器做到更准"，而是"把人的那一环做得更准、更可预测"。**

这就引出了第 2 节。

---

## 2. 人类的定时精度

### 2.1 人类的误差来源分解

```
看到"归零"          感知延迟       ~50  ms   （视觉信号传导）
    ↓               认知延迟       ~100 ms   （判断"该按了"）
"决定该按了"         运动准备       ~50  ms   （运动皮层激活）
    ↓               肌肉执行       ~50  ms   （手指动作）
手指按下
    ↓
接触完成            ─────────────────────
                    合计           ~150 ~ 250 ms
```

### 2.2 不同提示方式下的实测精度

| 提示方式 | 人类时间误差 | 说明 |
|---------|-------------|------|
| 纯视觉（数字倒计时） | ±100 ~ 200 ms | 需要持续注视 |
| **视觉 + 听觉（beep）** | **±50 ms 左右** | ★ 显著改善 |
| 视觉 + 听觉 + 触觉（震动） | ±30 ~ 50 ms | 移动设备可实现 |
| 无提示（凭记忆/估时） | ±500 ms ~ 数秒 | 不可用 |

**结论：声音提示不是锦上添花，是把误差减半的关键手段。**

> ⚠️ **重要限定（实现后修正）**：上表的数字来自 **reaction-time（反应时）范式** —— 测的是"看到/听到信号 → 反应"。
>
> 但本项目的 cue 是**周期拍点栅格**，人进入的是 **anticipation（预期）** 模式。二者的时间特征不同：
>
> | | reaction | anticipation |
> |---|---|---|
> | 任务 | 等信号出现再动 | 与已知节拍同时到达 |
> | 典型偏差 | **正**（晚）150~250 ms | **负**（早）数十 ms，即 NMA |
> | 可预测性 | 取决于信号 | 高（周期性本身给出预测性） |
>
> **negative mean asynchrony (NMA)**：跟 500 ms 节拍敲击时，人的敲击平均**提前**；间隔超过 ~2 s 后策略退回 reaction，偏差转正（Repp 的 tapping 综述）。
>
> 对本项目的三个直接后果：
> 1. **不要把倒计时做成"信号灯"，要做成"节拍器"** —— 周期性才有预测性。
> 2. **`human_offset_ms` 必须允许为负**（字段名不能暗示恒正）。
> 3. **上表的具体数值不能直接搬用**，必须由 M1 实测替换（`chrono probe --trials 30 --conditions both,visual`）。

### 2.3 实证研究支持

| 研究 | 关键结论 |
|------|---------|
| **Adaptive Metronome (GEM)** | 25~50% 的自适应度增强同步；★ **更高自适应度（70~100%）只在有听觉反馈时才有帮助** |
| **TANDEM**（Iqbal & Riek, ICRA 2021） | 神经生物学启发的适应/预期算法，人类水平的人机同步打鼓（含变速） |
| **Robot Thereminist**（IROS 2010） | 耦合振荡器模型使起始误差降低 **10~38%** |
| **Shimon（机器人马林巴）** | 社交头部手势显著改善变速同步；单纯手臂手势无效 |
| **Bayesian causal inference**（Proc. R. Soc. B） | 人类如何整合/分离冲突的节奏线索 |

### 2.4 学习效应：人类可以被"校准"

关键发现：**人类的定时误差不是随机噪声，而是有系统性偏置的。**

```
第一次合作：人类平均偏 X ms（早或晚，符号待实测）
第二次合作：机器把倒计时朝反方向调整 X ms
结果：人机同时到达目标时刻
```

这支撑了 `design.md` 里的 **`human_offset_ms` 记录 + 自适应校准** 设计。

> 这里有一个 reaction 与 anticipation 的**本质差异**：反应时任务的偏差主要来自**神经传导**，理论上不可消除；而拍点同步任务的偏差主要来自**预测策略**，因此：
> - 它**跨回合成稳定**，所以可以被实现地学到；
> - 但它也会**随拍距漂移**（>2 s 后退化成 reaction）。
>
> 所以校准必须**按任务签名分段**（拍距、提示通道、执行器都进 key），不能用一个全局数。实现见 `calibrate.py::task_signature`。
>
> ⚙️ **M1 已实现并在合成人类上验证**：注入 +80 ms 偏差，跑到 `residual = 0.0 ms`（校准完全抵消）。真人数据待跑。

> 这与 TANDEM 的"适应/预期算法"思想一致：**机器应该学习人的节奏，而不是要求人跟上机器。**

---

## 3. 两速率控制（Two-Rate Control）

### 3.1 模式原文

**`Two-Rate Cloud-Brain / Edge-Controller Split`**（Agent Patterns Catalog）

> Run a slow planner at low frequency that emits a compact latent plan, and a small on-device controller that tracks it at the robot's native control rate **without ever blocking on the planner**.

### 3.2 它解决的核心矛盾

> **_A single model cannot be both the deliberate planner and the real-time motor loop._**
>
> 如果一个高频率控制器每次都要等慢规划器给出新计划，**它的有效频率就塌缩到规划器的推理速度**，身体就会失衡或过冲。
> 如果强迫慢规划器跑到控制频率，**它就必须缩小到无法推理场景和指令的程度**。

### 3.3 Forces（约束力）

- 实时稳定性需要固定的高控制率；**错过截止时间是物理失败，不是"响应慢一点"**
- 场景理解 + 指令跟随需要大模型，其推理远慢于一个控制周期
- 板上算力/功耗有限，大模型常在云端，通过**变延迟链路**到达身体
- 两部分必须在"做什么"上一致，但**运行在两个不同的时钟上**

### 3.4 结构

```
慢规划器（几 Hz）                快控制器（50~100 Hz）
─────────────────              ─────────────────────
大模型                            小模型
读指令 + 近期观测                  读 latent plan + 本体感觉
输出紧凑 latent plan  ──────────►  输出电机指令
                                  ↑
                              ★ 永不阻塞等待慢规划器
                              ★ 无新计划时继续跟踪旧计划
                              ★ 或回落到安全保持（safe hold）
```

### 3.5 禁止事项（原文）

> The fast controller **may not block waiting on the slow planner**; it must act on the last posted latent plan, and when no fresh plan has arrived it must **keep tracking the previous one or fall back to a safe hold rather than stall**.

### 3.6 本项目对它的借用与扩展

| 模式要素 | 机器人场景 | 本项目场景 |
|---------|-----------|-----------|
| 慢规划器 | 云端大模型 | **LLM agent** |
| 快控制器 | 板上小模型 | **Conductor 进程** |
| 物理身体 | 机器人本体 | 开发板 + 串口 + **操作员** |
| latent plan | 紧凑目标表示 | timeline（时刻, 动作） |
| 本体感觉 | 关节编码器 | **串口/GPIO 回读** |

**本项目的扩展**：原模式只处理"机器—机器"两速率。本项目在快控制器的输出端**插入了一个人类执行器**，并为此增加了：

1. **人类提示通道**（倒计时 + 声音）
2. **人类动作的观测通道**（传感器回读，因为人不像电机那样可控）
3. **人类的延迟模型**（自适应校准）

> 也就是说：**本项目是 Two-Rate 模式在"执行器是人有抖动"情况下的推广。**

#### ❌ 必须放弃的继承："缺计划时继续跟踪旧计划"

原模式的禁止事项说：无新计划时"**keep tracking the previous one** or fall back to a safe hold"。

**前半句在本项目里是错的。** 机器人场景的"旧计划"是电机指令，重放顶多多走一格；这里的"旧计划"末端挂着**人** —— 重放它意味着**让人再按一次 Reset**，是另一个物理事件，可能真实损坏设备。

| 规则 | 处理 |
|------|------|
| 不阻塞等待慢规划器 | ✅ 完全继承（项目立足点） |
| 无新计划 → 保持旧计划 | ❌ **改为 safe hold**（提示人类停止动作） |
| 回落到安全状态 | ✅ 继承 |

实现上以 `HumanEffector.caps().repeatable = False` 表达；机器执行器（串口/GPIO）则允许重放 —— 这正是"窗口内扫射"与"重放人类动作"的分界。

---

## 4. 异步执行的延迟补偿（机器人学习领域）

### 4.1 observation–execution gap

一致的观察：**感知 + 推理 + 执行延迟**会导致**过期动作（stale actions）** —— 动作是基于"旧状态"算出来的，执行时世界已经变了。

### 4.2 各种补偿方法

| 方法 | 思路 |
|------|------|
| **DA-DP** | 把策略条件化在**测得的延迟**上，瞄准"执行时刻的状态"而非"观测时刻的状态" |
| **RAPAC-DP** | 云端/异步执行下的 **pending-action 补偿** |
| **ACNet** | 轻量级延迟感知适配器 |
| **ARLI** | 异步推理下非马尔可夫状态的 RL 微调 |
| **"Learning to Wait"** | LLM agent 通过 in-context learning 学会预测 `time.sleep(t)`，而非盲目轮询 |

⚠️ **对比研究的警告**：**没有单一方法占优**；某方法（VLASH）的结果依赖"特权未来状态访问"，实际部署表现可能不同。

### 4.3 对本项目的启发

- **"Learning to Wait"**：说明"让 LLM 学会等多久"是可行的 —— 但本项目选择**更彻底的做法：不等，而是提前安排**
- **DA-DP 的"瞄准执行时刻的状态"**：与本项目的 `expect_signal`（等信号而不是等时间）同源 —— **都承认"预测"不如"观测"可靠**
- **AgiLimb / IHDA**：机器控制人类驱动运动的时机（数字反射 ~60ms；EMS 约束释放）—— ★ 这是本项目的**极端形态**：不是"协调"人的动作，而是"驱动"人的动作

---

## 5. 幂等性与可重试设计

### 5.1 为什么必需

时间触发系统的一个固有风险：**如果执行了但没被观测到，会重复执行。**

```
机器: 发出动作
人类: 没看见 / 网络抖动 / 传感器漏读
机器: 以为失败 → 重试 → 动作被执行了两次
```

### 5.2 幂等化手段

| 手段 | 说明 |
|------|------|
| **状态检查再执行** | "如果已经是 PMON 提示符，就不要再发 `c`" |
| **序号推进** | 动作带单调递增序号，重复的旧序号直接丢弃 |
| **先观测后行动** | 执行前先读一次状态（read-before-write） |
| **可逆设计** | 动作用"切换"而非"设置"，或支持撤销 |
| **去重窗口** | 短时间内相同动作只执行一次 |

### 5.3 本项目的实际应用

`case-studies` 里的脚本已经体现了这一点：

```python
# 检测到已在 PMON → 跳过重启步骤（幂等）
if in_pmon(buf0):
    skip_reboot()

# initrd 失败 → 不启动内核（避免 panic 挂死；把"失败"变成"可重试状态"）
if not ok:
    sys.exit(2)
```

**把"失败"设计成一个可恢复的状态，而不是灾难** —— 这是幂等性在系统层面的体现。

---

## 6. 持久化与可恢复（生产环境）

### 6.1 阻塞等待的 anti-pattern

搜索结果反复出现的警告：

> 进程内阻塞等待在生产中是 anti-pattern：
> - 重启/扩缩容/部署会**丢失内存状态**
> - 可能发生**重复恢复**
> - 没有**审计追踪**

### 6.2 推荐模式

```
1. 持久化 checkpoint（Postgres / 文件 / 消息队列）
2. 进程可以退出（不阻塞占用资源）
3. 外部事件（审批通过 / 信号到达 / 超时）触发恢复
4. 从持久化记录恢复，幂等推进
```

参考实现：OpenAI Agents SDK、Cloudflare Agents、CopilotKit `useInterrupt` —— 都通过**序列化状态或 durable workflow gate** 实现暂停/恢复，而非阻塞循环。

### 6.3 本项目的取舍

| 场景 | 方案 | 理由 |
|------|------|------|
| 本地开发会话（本项目实际场景） | 进程内阻塞可接受 | 人就在旁边、会话生命周期短 |
| 长任务 / 无人值守 | **必须持久化 timeline + 可恢复** | 否则超时/崩溃即丢失 |
| MCP 集成 | 优先**异步**（`submit` 返回 id，`observe` 读结果） | 避免客户端 tool call 硬超时 |

> **`ai-intervention-agent` 的"倒计时 + 自动重提交"正是这个问题的工程化应对** —— 它承认了"tool call 会被客户端超时打断"，于是用自动重提交来维持等待。

---

## 7. 理论小结：五条设计原理

| # | 原理 | 理论来源 | 设计体现 |
|---|------|---------|---------|
| 1 | **时间触发优于事件触发**（当窗口 < LLM 延迟时） | Kopetz / Albert | 预先提交 timeline |
| 2 | **时间触发的价值取决于时钟同步精度** | Albert 的反直觉警告 | 必须用单调时钟；人机时钟对齐是核心问题 |
| 3 | **两速率分离：慢规划 + 快执行，永不阻塞** | Two-Rate Cloud-Brain 模式 | Conductor 独立进程（**但放弃"保持旧计划"**） |
| 4 | **听觉反馈把人的误差减半** | GEM / TANDEM | `cue.audio: beep` 不是可选项（**幅度待 M1 实测**） |
| 5 | **观测优于预测；幂等优于重试** | DA-DP / 分布式系统 | `expect_signal` 闭环 + 幂等动作设计 |
| 6 | **预测性优于反应性** | NMA / tapping 文献 | 用**周期拍点栅格**，而非一次性信号灯 |

---

## 8. 一句话

> **本项目把实时系统领域的"时间触发"、机器人学的"两速率控制"、和人机交互的"节拍同步"三个成熟理论，缝合到一个 LLM agent 能用的工具里。**

没有发明新理论，是把已有理论用在了新场景上。

---

## 参考来源

- Albert et al., *Time-Triggered vs Event-Triggered*, TUM — https://archive.air.in.tum.de/pub/Main/TeachingWs2013MSE/embedded_world_04_albert.pdf
- Kopetz, *Time-triggered real-time computing* — https://www.sciencedirect.com/science/article/abs/pii/S1367578803000026
- 混合架构 — https://doi.org/10.1016/j.sysarc.2019.101652
- Two-Rate Cloud-Brain / Edge-Controller Split — https://www.agentpatternscatalog.org/patterns/two-rate-brain-controller-split/
- DA-DP — https://arxiv.org/html/2512.07697v2
- RAPAC-DP — https://arxiv.org/html/2608.15924
- ACNet — https://arxiv.org/html/2606.25985v1
- ARLI — https://arxiv.org/pdf/2608.23831.pdf
- Learning to Wait — https://arxiv.org/html/2512.16262
- Async VLA 对比 — https://arxiv.org/html/2605.08168
- TANDEM (ICRA 2021) — https://cseweb.ucsd.edu/~lriek/papers/iqbal-riek-icra-2021.pdf
- Robot Thereminist (IROS 2010) — http://winnie.kuis.kyoto-u.ac.jp/members/okuno/Public/IROS2010-Mizumoto.pdf
- Shimon — https://www.frontiersin.org/journals/robotics-and-ai/articles/10.3389/frobt.2024.1461615/full
- Adaptive Metronome (GEM) — https://www.frontiersin.org/articles/10.3389/fnhum.2022.916551/pdf
- Bayesian causal inference — https://doi.org/10.1098/rspb.2014.0751
- Human-in-the-Loop without the Hacks (Render) — https://render.com/articles/human-in-the-loop-without-the-hacks-pausing-an-agent-mid-run-for-approval-workfl
- OpenAI Agents SDK HITL — https://openai.github.io/openai-agents-js/guides/human-in-the-loop/
