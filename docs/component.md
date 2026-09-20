# 交互式提示组件（demo）

> **一句话**：agent 需要人做一个**有时机要求**的物理动作时，调一个工具 → 浏览器弹出面板 → 面板用节拍栅格提示时机 → 人踩在重拍上操作 → 时刻被精确记录。

这是本项目对外的**产品形态**。"人类延迟校准"目前是**关闭的**（见 §5），先把组件本身做对。

---

## 〇、三种呈现方式

| 方式 | 命令 | 适用 |
|------|------|------|
| **独立窗口** ★ | `chrono window` | 常驻小挂件：无边框、可拖动缩放、置顶、自定义主题 |
| 浏览器标签页 | `chrono demo` / `chrono daemon` | 已开着浏览器、或手机同局域网 |
| 面板附加到 daemon | `chrono panel` | 窗口和 daemon 分开跑 |

### 独立窗口（推荐）

有两种跑法，区别很重要：

**A. 一体化**（一个进程同时是 conductor + 窗口）

```bash
python -m chrono_handoff.cli daemon --window --background
```

**B. 分离**（daemon 常驻，窗口只是显示器）

```bash
python -m chrono_handoff.cli daemon --background          # conductor + IPC
python -m chrono_handoff.cli window --attach 8791         # 只显示，随时可关
```

| | A 一体化 | B 分离 |
|---|---|---|
| 进程数 | 1 | 2 |
| **关掉窗口后 cue 还能打吗** | ❌ 不能（进程就是它） | ✅ **能** |
| 窗口崩了影响 conductor | 会 | 不会 |
| 适合 | 演示、手工试用 | 真用：agent 会一直调 |

> **B 是生产形态**：窗口是"可牺牲的显示器"，conductor 是"必须活着的时钟"。
> 分离模式下窗口被关掉，cue 照样按时触发、照样记录——只是没人看见。反过来重开窗口又能立刻接上。

单独起一次提示（不需要 daemon）：

```bash
python -m chrono_handoff.cli window --instruction "按一下 RESET" --in 6
python -m chrono_handoff.cli window --paused        # 等你按 Start
```

**窗口能力**：

| 能力 | 说明 |
|------|------|
| 无边框 + 自绘标题栏 | `--bordered` 可切回系统标题栏 |
| 拖动移动 | 拖标题栏或窗口任意处 |
| 缩放 | 拖窗口边缘（自由缩放）；`界面缩放` 可放大内容适配远看 |
| 置顶 | 默认开；标题栏 📌 一键切换 |
| 不透明度 | 40%~100% |
| 主题 | 深色 / 浅色 / 琥珀（暗房高对比） |
| **Windows 11 原生悬浮边框** | 圆角 + 亚克力（acrylic）毛玻璃 + 主题色描边，随主题联动 |
| 记住位置尺寸 | 关闭时写回配置 |
| 设置面板 | 标题栏 ⚙ 打开，改动即时生效并落盘 |
| 最小化 / 关闭 | 自绘按钮（无边框窗口没有系统按钮） |
| **关掉窗口不影响出提示** | 分离模式下（`--attach`）cue 仍按时触发并记录 |
| **conductor 失联会明说** | 标题栏变红提示 "conductor unreachable"，不会假装在等 cue |
| 切换主题 | 同时更新**原生窗口**的圆角/毛玻璃/描边（DWM 属性是每窗口状态，不是 CSS） |

窗口用的是 **系统自带的 WebView2**（Edge Chromium 内核），所以保留 `AudioContext` 的样本级音频调度——这是不能用 tkinter 之类的替代方案的原因：**没有精确音频，整个组件的价值就没了**。

> 没装 `pywebview` 时会**自动降级**成浏览器标签页，不会报错停摆：
> `pip install pywebview`（Windows 需 WebView2 运行时，Win11 自带）

### 1. 一次性 demo（浏览器标签页）

```bash
cd path/to/chrono-handoff && export PYTHONPATH=src

python -m chrono_handoff.cli demo --paused
```

会在 `http://127.0.0.1:8790/` 起面板并自动打开浏览器，进入 `prepared` 状态。**在面板上按 Start** 才开始倒计时——这样你可以先坐好。

```bash
# 不做交互等待，直接倒计时
python -m chrono_handoff.cli demo --in 6 --lead 3 --ioi 500

# 不自动开浏览器（手动访问上面的 URL）
python -m chrono_handoff.cli demo --no-open
```

### 2. 常驻 daemon（推荐：agent 随时可用）

```bash
python -m chrono_handoff.cli daemon --background
# 输出：cue panel: http://127.0.0.1:8790/
```

面板和 IPC 一起常驻。之后 agent 调 `chrono_cue` 即可，**面板已在后台等着**，不用等浏览器启动。

### 3. 面板单独跑 / 附加到已有 daemon

```bash
python -m chrono_handoff.cli panel --port 8795
```

---

## 二、agent 怎么用（MCP）

给 agent 配好 MCP server（`python -m chrono_handoff.mcp_server`）后，主要就一个工具：

```
chrono_cue(
  instruction = "按一下开发板上的 RESET 键",
  target_in_s = 6,          # 从现在起 6 秒后该动手
  lead_s      = 3,          # 倒计时 3 秒
  ioi_ms      = 500,        # 拍距 500ms
  tail_beats  = 6,          # GO 之后继续 6 拍（见下）
  audio       = true,
  open_panel  = true,
)
→ { run_id, status: "running", hint: "the cue runs without you; poll chrono_observe ..." }
```

**关键：它立刻返回，不等人类。** agent 应该结束当前 turn，过一会儿用 `chrono_observe(run_id)` 拿结果。阻塞在那里就退化成了原来的"事件触发"模式，本项目的全部价值就没了。

典型 agent 回合：

```
1. chrono_cue("按一下 RESET", target_in_s=6)      → 立刻返回，面板弹出
2. （结束本 turn，或去做别的事）
3. chrono_observe(run_id) → 读到 human_signal / cue_audit / run_end
4. 继续推理（例如：人按了，现在从串口读引导日志）
```

---

## 三、组件长什么样

```
┌──────────────────────────────────────────────┐
│  ● cue armed                    cue_9891fc   │
│                                              │
│  按一下开发板上的 RESET 键                    │
│  等脉冲，踩在重拍上按下                       │
│                                              │
│                    03                        │  ← 剩余拍数
│         beats to GO · 1.48 s                 │
│   ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬           │  ← 栅格，重拍高亮
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │            PRESS ON GO                 │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  clock offset          +0.4 ms               │
│  round-trip delay       2.1 ms               │
│  audio scheduling    14 scheduled            │
│  your press vs GO      −38.2 ms (early)      │
│  cue beats scheduled 14 beats · ioi 500 ms   │
└──────────────────────────────────────────────┘
```

面板底部那张表是**给人看的**：能一眼确认时钟对齐是否正常、机器侧调度是否正常。这些数字全部是实测值，不是装饰。

---

## 四、两个实现要点（决定了它是否真的准）

### 4.1 时序不在服务端逐拍推送 ★

如果 daemon 每拍发一条消息，每拍都会带上「HTTP/IPC + JS 事件循环 + 渲染」的抖动（几毫秒到几十毫秒），**倒计时就真的只是装饰品了**。

正确做法（已实现）：

```
agent 提交计划
   ↓
daemon 把整份时刻表解析成**绝对历元毫秒**，一次性下发
   ↓
浏览器用 AudioContext 在**本地**按样本精度调度所有拍点
```

所以浏览器只和服务端做**一次时钟对齐**（NTP 式：取往返延迟最小的样本），之后所有拍点都靠本地音频时钟。JS 定时器、`requestAnimationFrame`、网络抖动全部不在关键路径上。

**这就是 Two-Rate 模式在 UI 层的复现**：daemon 是慢规划器，网页是快控制器。

### 4.2 按压时间戳用**客户端**的，不是服务端收到的时间

人的手指按下 → 点击事件 → `performance.now()` 采样，这一串在浏览器里只需微秒级。而 HTTP 往返 + 服务端事件循环会再加几毫秒。

所以面板把**点击处理器采到的时刻**回传，服务端只用它做一次时区换算：

```json
POST /api/press { "client_wall_ms": 1789904984635.3, "clock_offset_ms": 0.4 }
```

**并且不做键盘栈延迟修正**（`latency_s = 0`）。daemon 默认对**控制台按键**做 15ms 修正（那是 OS 输入栈的延迟），但浏览器点击处理器**不经过键盘驱动**，对它做同样修正会让每次测量系统性偏 −15ms。实测验证：注入 45.0ms → 记录 45.0ms。

---

## 四点五、设置项

设置存于 `~/.chrono-handoff/settings.json`，**面板改动即时落盘**。也可命令行改：

```bash
python -m chrono_handoff.cli settings                     # 查看
python -m chrono_handoff.cli settings --set ioi_ms=600    # 修改
python -m chrono_handoff.cli settings --reset             # 恢复默认
```

| 分组 | 项 | 默认 | 说明 |
|------|-----|------|------|
| 节奏 | `lead_s` | 3.0 | 倒计时长度 |
| | `ioi_ms` | 500 | 拍距。~500ms 最容易进入跟拍状态 |
| | `tail_beats` | 6 | GO 之后继续拍数（见 §5，别调成 0） |
| | `default_target_in_s` | 6.0 | 新提示的默认目标时刻 |
| 声音 | `audio_enabled` | true | |
| | `volume` | 0.6 | |
| | `accent_hz` / `tick_hz` | 1568 / 880 | GO 音 / 普通拍音 |
| | `accent_ms` / `tick_ms` | 90 / 35 | 时长 |
| 窗口 | `always_on_top` | true | |
| | `opacity` | 0.96 | |
| | `scale` | 1.0 | 界面缩放（0.7~2.0），远看调大 |
| | `theme` | dark | dark / light / amber |
| | `remember_position` | true | |
| | `scale` | — | **已移除**：缩放改为响应式，字号自动跟随窗口大小 |
| 校准 | `apply_learned_offset` | **false** | 见 §6，默认关闭 |

设置面板里有 **试听** 按钮，可以不触发提示直接听当前音色。

**关于"conductor 失联"**：分离模式下 conductor 是另一个进程。如果它挂了或重启了，所有 `/api` 调用都会失败——面板本来会看起来"只是还没来 cue"，这与"永远不会来"无法区分，而操作员会一直等一个不会到来的倒计时。所以面板每 5 秒探一次 `/api/health`，失联时标题栏直接变红。

**两个刻意的设计**：
1. **配置文件损坏不会影响出提示** —— 解析失败直接回落默认值。组件必须在关键时刻能用，不能因为一个 JSON 写坏就罢工。
2. **设置面板和 conductor 必须用同一份数** —— `prepare` 时会把设置里的 `lead_s/ioi_ms/tail_beats` 作为默认值合并进计划，否则"设置界面"就是个谎。

## 四点六、退出与重启

```bash
# 分离模式：窗口随便关，conductor 不受影响
python -m chrono_handoff.cli window --attach 8791

# 停 conductor（pid 在 ~/.chrono-handoff/daemon.json）
python -m chrono_handoff.cli status      # 看它还活着吗
```

**重启行为**：conductor 重启后端口立即可复用（用平台默认 bind 语义，见下）。
第二实例会**明确拒绝**并给出可执行的下一步，而不是静默把 IPC 流分裂到两个进程：

```
chrono-handoff: a conductor is already answering on 127.0.0.1:8791.
  Inspect it:  chrono status --port 8791
  Stop it:     kill the pid in ~/.chrono-handoff/daemon.json
```

> **为什么用平台默认的 bind 语义**（实测于 Windows 11）：
>
> | socket 选项 | 关掉后能否立即重绑 | 第二进程能否抢占 |
> |---|---|---|
> | 不加（默认） | ✅ 能 | ✅ 正确失败 |
> | `SO_EXCLUSIVEADDRUSE` | ✅ 能 | ✅ 正确失败 |
> | `SO_REUSEADDR` | ✅ 能 | ❌ **能抢占** |
>
> 所以 Windows 上默认已经是安全的，而 `SO_REUSEADDR` 才是危险的那个——它的 Windows 语义是"让我抢占已在使用的地址"，与 POSIX 正好相反。这正是曾经出现过**两个 conductor 同时监听同一端口、连接被随机分配**的原因。

## 五、`tail_beats` 为什么默认是 6（不是可选装饰）

这是 M1 实测暴露的问题，值得单独说。

如果**重拍就是最后一拍**：

```
嘀 嘀 嘀 嘀 嘀 GO          ← 然后没了
```

人只能"等 GO 出现再按"——这在结构上**必然是反应时**。真实验第一轮的数据正是如此：`both`(+114ms) 与 `visual`(+105ms) 几乎一样，且 **0% 提前**，是典型的 reaction 特征。

而 NMA（人会略早）只在**节奏持续**时才涌现。所以：

```
嘀 嘀 嘀 嘀 嘀 GO 嘀 嘀 嘀 嘀 嘀 嘀
                ↑ 节奏继续，人可以锁定脉动
```

配套：`cancel_cue_on_signal = False` —— 人一按就静音的话，他永远学不到"GO 后面还有拍"，下一轮又退回反应。**生产环境里"人一动就停提示"是对的；但 cue 本身是被研究对象时不能停。**

---

## 六、当前**关闭**了什么（以及为什么）

按你的要求，**人类延迟校准暂不参与**，作为后续提升项。

| 能力 | 状态 | 说明 |
|------|------|------|
| 节拍栅格提示时机 | ✅ 已实现 | 亚毫秒调度（实测 median 0.018ms） |
| 浏览器本地精确音频调度 | ✅ 已实现 | AudioContext，不依赖 JS 定时器 |
| 时钟对齐 | ✅ 已实现 | NTP 式，回环实测偏移 +0.35ms |
| 按压时刻精确记录 | ✅ 已实现 | 客户端时间戳，实测误差 <1ms |
| 闭环观测（串口/HTTP/按键） | ✅ 已实现 | `expect_signal` |
| 机器侧窗口动作（串口抢占） | ✅ 已实现 | `on_window` |
| **自动学习人的偏移并补偿** | ⏸️ **已实现但默认不启用** | 靠 `offset_s` / `chrono_calibrate` |
| 硬件执行器（继电器/PicoHand） | ⏸️ 待做 | `Effector` 协议已就位 |

**校准怎么开**（等你想要时）：

```
# 看当前学到的偏移
chrono_calibrate  →  { items: [...] }

# 提交时手动应用（把 cue 朝反方向挪）
chrono_cue(..., offset_s = -0.045)   # 若人平均晚 45ms
```

也就是说：**校准不是被删掉了，而是不挡路。** 组件先把"提示时机准不准"做对；等你要优化，把学到的 `offset_ms` 传进 `offset_s` 即可，不需要改任何结构。

---

## 七、验证

```bash
# 核心机制（10 项：时钟/音频/管线/校准/IPC/降级）
python -m chrono_handoff.cli selftest

# 面板 HTTP 全链路（20 项，模拟浏览器行为）
python scripts/e2e_panel.py

# 原生窗口（17 项，需桌面会话；无桌面时自动跳过）
python scripts/e2e_window.py

# 一体化模式：一个进程同时是 conductor + 窗口（8 项）
python scripts/e2e_daemon_window.py

# ★ 分离模式：窗口只是显示器（13 项）——验证"关掉窗口 cue 照样打"
python scripts/e2e_split_mode.py
```

另外三个只测 UI 的（都需要桌面会话）：

```bash
python scripts/e2e_ui_layout.py    # 布局几何 + 窗口按钮（18 项）
python scripts/e2e_resize.py       # 拖拽缩放 + 最大化/还原（10 项）
python scripts/e2e_theme_drag.py   # 主题对比度 + 拖拽隔离（16 项）
```

`e2e_ui_layout.py` 有意测量**几何**而不是"元素存在"：最初版本的标题栏被排到了内容**左边**（`body` 的横向 flex 泄漏进了外壳布局），出现了横向滚动条，而当时的测试全是"元素存在"断言，**全部通过**。现在它检查标题栏跨满宽度、位于内容之上、无溢出、时钟在视口内，以及关闭按钮**可命中**（`elementFromPoint`）。

`e2e_resize.py` 验证的是这类 bug 中最难发现的那种：

| 断言 | 对应曾经的真实 bug |
|------|-------------------|
| 拖右下角能放大 | 无边框窗口边缘本来无法缩放 |
| **快速拖动也能放大** | 早期用 Python 轮询光标，快速拖动（约 0.28s）在第一次轮询前就松手了，**完全没反应** |
| 拖左边同时改变原点和宽度 | 从左边缘缩放必须同时改 x 和 width |
| 最小尺寸生效 | 防止缩成 0 像素的不可恢复状态 |
| 最大化能变大 / 还原能变小 | — |

`e2e_theme_drag.py` 覆盖两个只有真窗口才暴露的集成 bug：

| 断言 | 对应曾经的真实 bug |
|------|-------------------|
| 三套主题的**实测对比度** ≥ 4.5:1 | 不透明度实现把硬编码的暗色写进 inline style，压制了主题 → 浅色模式下深字压深底，完全不可读 |
| 按滑杆**不触发**窗口拖动 | `easy_drag=True` 让**任意**按下都变成拖窗口，滑杆、按钮全部中招 |
| 拖标题栏**确实**触发拖动（正向对照） | 没有这条，上一条可能因为"合成输入根本没到达页面"而假通过 |

它还用 **`elementFromPoint` 命中测试**确认探针点到的确实是滑杆本身，避免"测了个别的元素"。

> **一个不做的断言**：滑杆的**数值变化**只作信息输出，不作断言。原生 `range` 控件对合成鼠标输入的响应不属于本项目能控制的范围——断言它会让测试变得 flaky，却证明不了我们自己的代码。真正该断言的是"按下控件不会启动窗口拖动"，那是我们代码的性质。

`e2e_split_mode.py` 验证的是**生产形态的关键性质**，也是这个设计里唯一会静默出错的地方：

| 断言 | 为什么重要 |
|------|-----------|
| 没有窗口时 armed 的 cue 仍然触发并记录 | 窗口是可选显示器，不是时钟 |
| 显示器进程**不能** submit / cue | 第二个 conductor 会让每一拍响两次 |
| 窗口关掉后 daemon 仍存活、后续 cue 仍触发 | 时钟必须活得比 UI 久 |

`e2e_panel.py` 模拟浏览器行为：时钟对齐 → 取时刻表 → 校验拍点间距 → 用客户端时间戳按压 → 核对记录值。它覆盖的就是单元测试碰不到的两处：**单调钟↔墙钟换算**、**客户端时间戳路径**。

实测输出（节选）：

```
[PASS] clock sync converges              offset=+0.353ms rtt=2.095ms on loopback
[PASS] schedule delivers the whole grid   12 beats
[PASS] beats are absolute epoch ms ...    ioi preserved to <1ms
[PASS] tail beats follow the GO beat      tail=3
[PASS] recorded offset matches injected   注入≈60ms recorded=59.4ms
[PASS] cue grid played through its tail   3 tail beats fired of 3 planned
[PASS] cue grid landed sub-millisecond    median=0.018ms max=0.297ms
```

---

## 八、HTTP 接口（如果要自己接前端）

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/` | 面板页面（自包含单文件） |
| GET | `/api/health` | 存活 + 当前 run |
| GET | `/api/clock` | 一次时钟对齐样本 |
| GET | `/api/schedule?run_id=` | 整份时刻表（绝对历元毫秒） |
| GET | `/api/events?since=N` | 增量事件 |
| GET | `/api/wait-armed?timeout=25` | 长轮询：下一个 cue 被武装时立刻返回 |
| POST | `/api/prepare` | 武装一个 cue（不启动） |
| POST | `/api/start` | 启动已武装的 cue |
| POST | `/api/press` | 上报按压（带客户端时间戳） |
| POST | `/api/cancel` | 取消 |
| GET | `/api/settings` | 读设置 |
| POST | `/api/settings` | 改设置（落盘 + 应用到窗口） |
| POST | `/api/settings/reset` | 恢复默认 |
| GET | `/api/calibration` | 已学到的偏移（供设置页显示） |
| GET | `/api/next-cue-defaults` | 新提示的默认参数（来自设置） |

`/api/wait-armed` 是让面板**提前打开也有用**的关键：它长轮询等 cue，被武装的瞬间就拉时刻表。这样"打开面板"和"需要提示"解耦了。

---

## 九、下一步（可选）

1. **手机触觉**：`HttpSource` 已就绪，同局域网手机打开面板即可。震动在时间同步上与听觉相当，明显优于视觉（见 `theory.md` §2.2）。
2. **串口闭环接入 demo**：现在是 `expect_source="none"`；改成 `"serial"` + `pattern="PMON2020"` 就是 case study §4.1 的完整流程。
3. **开启校准**：把 `chrono_calibrate` 的输出接进 `offset_s`，跑 `--calibrate-every` 验证 residual 收敛。
4. **MCP Apps 内嵌版**：现在面板是独立页面（可常驻、可复用、手机可用）。若希望它作为对话内联卡片出现，可另加一个 `_meta.ui.resourceUri` 版本。
