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
cd <repo> && export PYTHONPATH=src

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
→ { run_id, status: "running", queued_behind: null,
    hint: "the cue runs without you; poll chrono_observe ..." }
```

**关键：它立刻返回，不等人类。** agent 应该结束当前 turn，过一会儿用 `chrono_observe(run_id)` 拿结果。阻塞在那里就退化成了原来的"事件触发"模式，本项目的全部价值就没了。

`queue` 默认为 true：屏幕上还有 cue 在跑时，新的一条记为 `pending`、
`queued_behind` 指向前面那条，等它的拍子播完自动开始。参数写在工具签名上，
agent 直接看得到每个字段。

`expect_source` 不传就是 `none`：**这条只提醒**。没人观测操作者，什么也不记录，
面板上不出现任何可点的东西。想让人自己把时刻记下来就传 `"http"`（面板观测），
或者传传感器名（`serial` / `console_key` / `stdin_line`），机器去看硬件。

要锤在墙钟某一刻（例如 10:00:00），传 `target_at_wall`（epoch 秒）代替
`target_in_s`。它在网格开始时才换算，所以排队不会把它推后；那一刻在排队期间
过去了就以 `verdict: "missed"` / `status: "error"` 收尾，不会报 ok。
内部字段 `target_at` 是单调时钟，提交 epoch 值会被直接拒绝（否则会变成一个
「57 年后」的时刻，`lead_s` 被忽略）。

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
│  给板载 LED 拍照                              │  ← 指令：整个面板最大的字
│  听脉冲，重拍就是 GO：在那一刻做上面那件事      │
│                                              │
│                    03                        │  ← 剩余拍数
│         beats to GO · 1.48 s                 │
│   ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬ ▬▬▬           │  ← 栅格，重拍高亮
│                                              │
│  ┌──────────┐                                │
│  │ 立即开始  │  重新校时                      │  ← 次要控制，不是任务
│  └──────────┘                                │
│  ─────────────────────────────────────────   │
│  [记下我做的时刻（可选）]  按一下会记下你的时刻 │  ← 仅当面板是观测面
│  cue beats   14 beats · ioi 500 ms           │  ← 遥测默认只留这一行
│  遥测 ›                                       │  ← 其余一点展开
└──────────────────────────────────────────────┘
```

### 3.0 面板的层级：指令是内容，倒计时是它的时基

这个组件只做一件事：**告诉人在某个时刻做什么**。倒计时是挂在那句话上的钟，
不是目的。所以界面必须让那句话最显眼，而这一条曾经整反了：

| 项 | 曾经 | 现在 |
|---|---|---|
| 指令字号 | 16 px（**比倒计时 58 px 小得多**） | `clamp(19px,3.6vmin,32px)` |
| 指令换行 | `white-space:normal`，多行时间线**被压成一句流水话** | `pre-line`，保留作者换行（实测 4 行 → 4 行） |
| 主按钮 | 51 px 高、占满整行的 `GO 时按下`，是面板最大的可交互面积 | **没有主按钮**。只有一行可选的「记下我做的时刻（可选）」，且仅在面板是观测面（`http`/`inject`）时出现 |
| 遥测表 | 97 px（**是指令的 4.6 倍高**） | 默认折到 1 行（52 px），其余点开 |
| 结束文案 | `已结束 · timeout` | `已结束 · 已完成 / 没收到动作 / 已取消 / 错过了` |
| 观测通道名 | `机器会在 console_key 上确认。`（内部枚举，折成 3 行） | `机器在控制台按键上确认。`（人话，1 行） |
| GO 后的读数 | 40 px（倒计时的 37%，仍是第二大的东西） | 20.5 px（19%），降为诊断性注脚 |
| 时间线场景的倒计时 | 107 px，抢在四行指令前面 | 按指令行数缩放：1 行 1.0 / 2 行 0.8 / 3 行 0.7 / 4+ 行 0.6 |

四个具体的错位，值得单独写下来：

1. **"GO 时按下" 把项目讲错了。** 面板上最大的按钮在说"按我"，而 agent 让人做的是
   别的事。更糟的是传感器观测时按钮被隐藏但仍占位，人真正的任务被降级成
   「按上面说的做」——按钮才是主角，任务是附注。

   现在已经不是缩小它，而是它**不再是主按钮**：从 `.actions` 主位移走，
   改成面板底部的安静一行「记下我做的时刻（可选）」，旁边写「按一下会记下你的
   时刻；不按也照样跑完」。而且它只在 `expect_source ∈ {http, inject}` 时出现——
   传感器观测时、以及默认的 `none`（只提醒）时整行不渲染。
2. **`demo` 的默认文案在教错用法。** `chrono demo` 原来默认
   `Press the button on the GO beat`，新人第一眼看到的就是"按按钮"。现在改成
   落到具体物体上：`在重拍那一刻按一下开发板上的 RESET 键`。
3. **内部枚举被印在人读的那一行上。** `expect_source` 是 `console_key` /
   `stdin_line` / `serial` 这样的英文 snake_case，直接拼进 guidance 后变成
   `机器会在 console_key 上确认。`——还因太长折成三行，把倒计时往下挤。现在走
   `SOURCE_WORDS` 映射表（控制台按键 / 终端输入 / 串口 / 面板 / 模拟源），
   未收录的值回退原字串（宁可显式无名，不要静默错标）。
4. **倒计时和指令抢注意力。** 四行时间线 + 107 px 数字，眼睛先落在数字上。
   现在时钟按**实测行数**缩放（`fitInstruction()`，不是数 `\n`——长单行也会折行），
   分档而非连续：一个随文字宽度跳动的倒计时，正是这个组件最不能有的东西。

遥测折叠用的是真按钮（`aria-expanded`），不是悬停：这是台面上的工具，经常戴手套操作，
藏起来的入口不算入口。展开状态在一次会话内记住。

面板底部那张表是**给人排查用的**：能确认时钟对齐、机器侧调度是否正常。这些数字全部是
实测值，不是装饰——但正常使用时不需要看，所以默认收起。

### 3.0.1 这一层级由脚本盯着：`scripts/audit_panel.py`

上面那些数字不是一次性调出来的。这个面板**所有已知的严重缺陷都没被当时的测试抓到**：
套件断言的是“元素存在”和“没有溢出”，而实际缺陷是“那句话是卡片上最不显眼的字”。
两者不重叠。

所以另有一个脚本，把真实窗口逐阶段驱动一遍（idle / ready / 倒计时·面板观测 /
倒计时·传感器观测 / 四行时间线 / 遥测展开 / GO / GO 之后），量字号、行数、占比，
然后按**比例**判定：

| 判定 | 阈值 | 实测 |
|---|---|---|
| 指令字号 > 所有周边字（guidance / phase / meter / press） | ×1.40 | 19 vs 13 = 1.46 |
| 四行时间线渲染为四行且不裁 | =4 行 | 4 行 |
| 多行时倒计时让位 | scale < 1 | 107 px → 58 px |
| 倒计时仍 > 指令 × 1.5（还是最大的数字） | ×1.50 | 58 vs 19 |
| GO 后读数 < 倒计时的 1/3 | 0.333 | 20.5 / 107 = 0.192 |
| 遥测默认 1 行、展开后全行 | — | 1/5 → 5/5 |
| 传感器观测时按钮高 0 | 0 px | 0 px |
| guidance 不含枚举名、只有 1 行、不叫人按面板 | — | 全部通过 |

阈值全是**比例**，不是像素：面板本身是响应式的（`clamp()` + `vmin` + 行数分档），
写死像素等于断言“这台显示器”，而“指令比遥测大”才是设计断言。换台 DPI 不同的机器，
结论不变。

```bash
python scripts/audit_panel.py            # 表格 + 判定，退出码 0/1
python scripts/audit_panel.py --verbose  # 逐阶段打印明细
python scripts/audit_panel.py --json     # 机读
```

它不替代 `e2e_ui_layout.py`：那个断言**几何**（不裁切、导轨放得下、窗口控件可用），
这个断言**阅读顺序**。两个不同的断言，需要两套不同的数字。

### 3.0.2 `none` 是「只提醒」，不是「按面板」

组件最常见的用法是 **「在 T 做 X」**，没人观测人。这个形态必须有一个枚举值，
而之前没有：`none` 既被 `webui.PANEL_OBSERVED_SOURCES` 当成「面板点击就是证据」，
又被 `_verdict_for` 当成「不期待任何信号」——同一个值两套定义。

现在：

| `expect_source` | 谁在观测 | 面板上有什么 | 点了算什么 |
|---|---|---|---|
| 不传 / `none` | 没人 | 什么都没有 | 不记（仅入日志 `panel_press_not_evidence`） |
| `http` / `inject` | 面板 | 一行「记下我做的时刻（可选）」 | 记为证据，算 offset |
| `serial` / `console_key` / `stdin_line` / `sim` | 传感器 | 什么都没有 | 不记（同上） |

所以默认的 cue 不会把「按这里」当成任务；想让人自己记时刻，得显式要。
这也让面板文案能说实话：`none` 时 guidance 写「这条只提示，不记录你的动作」，
传感器时写「机器在控制台按键上确认」。

### 3.1 导轨的宽度：一个曾把整张卡片挤破的坑

导轨一格一拍，所以它的**自然宽度随拍数无限增长**（20 s 前导 @ 500 ms 默认拍 = 40 拍，加上把 GO 居中所需的补位就是 81 格）。而格子的 `min-width` 和 `gap` 都不会缩：81 格需要 611 px，而卡片里只有约 390 px。

两层问题，缺一不可：

| 层 | 问题 | 修法 |
|---|---|---|
| 卡片 | `.panel` 是 grid 而没写 `grid-template-columns`，隐含列的 `auto` 最小值 = 最宽子项的 min-content（即导轨）。于是列变成 614 px，**每一个子项都继承了它**，而卡片仍是 439 px 并直接裁掉——GO 按钮和所有右对齐的数字一起消失 | `grid-template-columns:minmax(0,1fr)`（行的防护早就有了） |
| 导轨 | 即使列被限住，导轨自己的内容还是放不下，尾部被剪——更糟的是 GO 可能被推到屏外，而 **GO 是一个位置**，操作者得在整个倒计时里找得到它 | `fitRail()`：按可用宽度和格数算 `gap`，必要时连格子宽度也让步（下限 1 px） |

两条都实测过：**只留任一条，外壳渲染就是对的**；这就是为什么两条都留（列那条是结构性的，防的是以后任何人往 `.panel` 里塞一个宽东西）。

两个测量上的经验，值得写下来：

- **`documentElement.scrollWidth` 抓不到这个 bug。** 文档从来不溢出（`body.scrollWidth` 始终是对的），溢出发生在卡片内部。
- **`#grid` 的 `scrollWidth` 也抓不到。** 它 `overflow:visible`，对这种元素 `scrollWidth` 只是回显 `clientWidth`——基于它的断言永远不可能失败。真正能测的是**格子落在哪里**：最后一格的 right 边对比轨道的 right 边。
- 连 `.stage` 的 `scrollWidth` 都不够稳：同一份坏代码在外壳里报 630/439（抓得到），在 pywebview 里却报 439/439（抓不到）。所以 `scripts/e2e_ui_layout.py` 现在直接量格子位置，并在**外壳**那份 e2e 里另外断言页面没有发过“卡片在被裁剪”的告警——那才是出问题最严重的地方。

不确定界面到底长什么样的时候，直接截一张看：

```bash
python scripts/_shot_window.py chrono-handoff
```

默认落在 `logs/shots/`（gitignored）。细节与三个坑见 `docs/desktop-shell.md` §五。

---

## 四、三个实现要点（决定了它是否真的准）

### 4.0 多条 cue 串行，且页面被告知队列 ★

屏幕上永远只有一条 cue 在响。后到的 cue 由 conductor 记为 `PENDING` +
`queued_behind`，不 arm、不占屏幕；前一条的**刺激**（不是 verdict）结束时自动 `go()`。

两个细节值得单独记：

- **段间隔看拍子，不看 `duration_s`。** 一条不观测人（`expect_source` 为 `none`）
  且没有 `on_window`/`verify` 的 cue，在最后一个拍子落地就收尾——没有任何东西
  会再到来，等预算就是纯静默。实测：`target_in_s=2, duration_s=10` 的两条 cue，
  修前 B 在 A 后 **12.0 s** 才开始（中间 9.6 s 静默），修后 **2.6 s**。
- **已终态但还在响的前驱不能成为死胡同。** 这种前驱不会再 `_finalize`，它当初
  排的释放任务早于新 cue 存在，所以 `_wait_behind()` 在入队时自己补一次释放。
  实测漏了这步时，一条 cue 会永远停在 `PENDING`。

面板侧只需回答两个问题：

- **现在该显示谁** —— `_pick_current()`：当前那条还活着就继续显示；它到终态而后面那条已被
  conductor 启动，则交班；都不到终态就保留已结束那条的最后一帧（它的 tail 还在响）。
- **后面还有几条** —— `queued_runs()` 直接问 conductor（面板不自己记，否则是第二个真相源）。
  页面把数字追在 guidance 行上，否则人做完第一件就走了，而第二段的倒计时已经自己开始了。

排队中的 cue **不发布 schedule**：它还没有网格，页面按「已武装（未开始）」渲染。

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

### 4.2 记录时间戳用**客户端**的，不是服务端收到的时间

人的手指按下 → 点击事件 → `performance.now()` 采样，这一串在浏览器里只需微秒级。而 HTTP 往返 + 服务端事件循环会再加几毫秒。

注意这是**测量**，不是任务：只有当 `expect_source` 是 `http`/`inject`（面板就是观测面）
时页面才提供那个可选按钮；其它情况下点击只会进日志，不算证据。

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

状态文件带一个 `schema` 号（`src/chrono_handoff/schema.py`，`settings.json` 与
`calibration.json` 同一套）：

- **没有 `schema` 字段**的旧文件按当前 schema 处理，不是"未知"——那个标记是在布局已经
  定了之后才加的。
- 读到**比本构建更新**的 schema：数字用默认值，但**拒绝写入**，原因同时出现在
  `/api/health` 的 `writable`/`why_not_writable`、面板横幅和 `chrono doctor` 里。
  上面第 1 条只管**读**；拿它去**写**就是降级（回退版本、装旧包）时的静默数据丢失，
  而 `calibration.json` 里装的是一个人的实测数据，丢了算不回来。
- 真要迁移时会先留一份 `<文件名>.v<旧号>.bak`，迁移函数按序号依次作用（`MIGRATIONS`，
  目前为空）。

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

## 四点七、被更新叫停（cue 安全窗口）

更新要停掉 conductor 才能换文件，但 cue 是**对一个人的、带截止时刻的承诺**：在他被告知
“在 T 按”的中途重启，人就守着一个死掉的窗口——这是这个组件唯一要避免的失败。所以
“现在能不能停”由 conductor 回答，不由更新器说了算：

| 接口 | 语义 |
|---|---|
| `GET /api/update-ready` | `safe` / `reason` / `retry_in_s` / `active[]` / `journal_flushed` |
| `POST /api/update-prepare` | 不安全就 **409**；安全则转入 quiescent（拒绝新 cue）并让本进程退出 |

判定规则：**终态 run 不阻挡**。人已经应答（或预算耗尽）就意味着承诺结束；GO 之后的 tail
拍是帮人跟拍的余韵，不是承诺，可以截断——这是判断，不是疏漏。

`retry_in_s` 在“已提交但还没启动”时是 `null`：那时确实没有可算的结束时刻，编一个像样的
数字比说不知道更糟。

三个实现细节：

1. **不是 kill，是请它自己走。** `stand_down` 让 daemon 的 accept 循环返回，剩下的收尾
   复用 Ctrl-C / SIGTERM 那一套（一 teardown，而不是三条会各自漂移的）。
2. **先回话再走。** teardown 被延后 250 ms——调用方正踩在即将关闭的循环上收答复，
   同步拆会把这句答复一起切掉。
3. **时钟在别的进程时走 IPC。** 面板挂在另一个 conductor 上时，它转发 `update_ready` /
   `stand_down`，不自己猜（显示屏不该替别人的承诺作主）。若那个进程没有任何循环可退出，
   答复会说 `exiting: false` 并注明“调用方得自己停进程”，而不是假装已处理。

`journal_flushed` 恒为 `true`，**因为它是构造成立的**：`_emit` 在调用线程里 `open`/`write`
每个事件，所以一个已经结束的 run 早就落了盘，并没有什么东西可 flush。

### 四点八、绑端口不能查 DNS

`HTTPServer.server_bind` 会调 `socket.getfqdn(host)` 去填 `server_name` —— 一次**反向解析**，就发生在绑定路径里。解析慢或不通的机器上它按秒计：CI 的 macOS runner 实测 **35.0 s**（同机 `socket.getfqdn("127.0.0.1")` 单独跑也是 35.002 s），而那 35 s 全落在面板那一步的启动标记里 —— `chrono daemon` 起完之后 35 秒内什么都不服务，日志一出来却完全正常。Linux（`/etc/hosts` 立刻答）与 Windows（本机 14 ms）看不出来，所以这是个只在别人的机器上发作的 bug。

面板与按压源因此都用 `chrono_handoff.local_http.LocalHTTPServer`：绑定路径不解析主机名，`server_name` 直接取字面 host。`scripts/e2e_panel.py` 会把 `socket.getfqdn` 换成一个抛异常的函数、再绑一次面板与按压源，任何平台都能证明这条规则没被破坏。

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
python scripts/e2e_ui_layout.py    # 布局几何 + 界面层级（40 项）
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
| GET | `/api/health` | 存活 + 当前 run + `queued`（排在后头的 cue） |
| GET | `/api/clock` | 一次时钟对齐样本 |
| GET | `/api/schedule?run_id=` | 整份时刻表（绝对历元毫秒；排队中的 cue 没有 beats） |
| GET | `/api/events?since=N&run_id=` | 增量事件（不传 run_id 时用当前显示的那条） |
| GET | `/api/wait-armed?timeout=25&since=G` | 长轮询：有新 cue 被武装或队列变化时返回 |
| POST | `/api/prepare` | 武装一个 cue（不启动） |
| POST | `/api/start` | 启动已武装的 cue |
| POST | `/api/press` | 记录操作者的时刻（仅当面板是观测面时算证据，否则只入日志） |
| POST | `/api/cancel` | 取消 |
| GET | `/api/settings` | 读设置 |
| POST | `/api/settings` | 改设置（落盘 + 应用到窗口） |
| POST | `/api/settings/reset` | 恢复默认 |
| GET | `/api/calibration` | 已学到的偏移（供设置页显示） |
| GET | `/api/next-cue-defaults` | 新提示的默认参数（来自设置） |
| GET | `/api/update-ready` | 现在能不能停（更新用；见 §四点七） |
| POST | `/api/update-prepare` | 转 quiescent 并让本进程退出；不安全时 409 |

`/api/wait-armed` 是让面板**提前打开也有用**的关键：它长轮询等 cue，被武装的瞬间就拉时刻表。这样"打开面板"和"需要提示"解耦了。

`since=G` 是页面已经展示过的 generation。**这个参数必须被解析**：不解析的话每次调用都
立刻返回 `already: true`，页面就会以 400 ms 间隔空转重取同一份时刻表，队列数永远没有
机会变化。同理 `/api/events` 的 `run_id` 也必须生效——不生效时问 B 的事件会拿到 A 的。

---

## 九、下一步（可选）

1. **手机触觉**：`HttpSource` 已就绪，同局域网手机打开面板即可。震动在时间同步上与听觉相当，明显优于视觉（见 `theory.md` §2.2）。
2. **串口闭环接入 demo**：现在是 `expect_source="none"`；改成 `"serial"` + `pattern="PMON2020"` 就是 case study §4.1 的完整流程。
3. **开启校准**：把 `chrono_calibrate` 的输出接进 `offset_s`，跑 `--calibrate-every` 验证 residual 收敛。
4. **MCP Apps 内嵌版**：现在面板是独立页面（可常驻、可复用、手机可用）。若希望它作为对话内联卡片出现，可另加一个 `_meta.ui.resourceUri` 版本。


---

## 音频后端

提示音由 `AudioSink` 提供，后端按精度分级，`chrono doctor` 报告实际选中项：

| 后端 | 平台 | 精度 | 说明 |
|---|---|---|---|
| `sample` | 全平台 | 采样级 | 常驻 PortAudio 流，需 `sounddevice` |
| `wav` | Windows | 设备级 | `winsound` 异步播放预渲染 WAV |
| `cmd` | 全平台 | **近似** | 外部播放器进程，启动抖动 10–250 ms |
| `bell` | 全平台 | 未知 | ASCII BEL，由终端发声 |
| `null` | 全平台 | —— | 静音，用于测量运行 |

选择顺序：Windows 为 `wav → sample → cmd → bell`，其余平台为
`sample → cmd → bell`。Windows 保持 `wav` 优先是因为它已实测
（0.25 ms 调用、3 ms 最差）且无需依赖。

```bash
chrono daemon --audio sample     # 强制指定
chrono daemon --audio null       # 出提示但静音（做纯计时测量）
chrono doctor                    # 查看实际选中后端与理由
```

**强制指定本平台不提供的后端会报错**，不会静默换成别的后端——静默降级
会让一次测量运行带上错误的精度等级而调用方毫不知情。

浏览器面板不受影响：它用 Web Audio API 在本地调度音频，与操作系统无关。
