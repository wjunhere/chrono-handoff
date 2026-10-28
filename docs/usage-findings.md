# 实测问题记录：在真实任务里用 chrono-handoff

> 首轮记录于 2026-10-27，版本 **0.1.1**（HEAD `80f8c1d`）。
> 场景：给嵌入式实验的板载 LED 拍实物照片，需要人在 6 个时刻依次按状态拍摄。
> 平台：Windows 11 · CPython 3.14 · Git Bash · 通过 MCP（`chrono mcp`）调用。
>
> 本文只写**实际踩到的东西**，每条都带复现方式与根因定位。五个问题现在都已修掉，
> 每节末尾的「修法」写的是最终改动，而不是当时的绕行。

---

## 问题 1 ★ `chrono daemon --background` 必崩（已修）

### 现象

```bash
$ chrono daemon --background
chrono: AttributeError: 'Namespace' object has no attribute 'visual'
```

100% 复现。`--background` 分支根本走不通。

### 根因

`daemon.py` 的参数解析器只定义了 `--no-visual`（→ `args.no_visual`），
但 `spawn_background()` 读的是 `args.visual`——argparse 从没建过这个属性。
同一个逻辑在 `cli.py` 的 `cmd_daemon` 里是对的。

顺带查出第二处：`--no-panel` / `--panel-port` / `--panel-open` 三个选项
**完全没有转发**给子进程。于是「不要面板」的 daemon 仍然起了面板并占住 8790，
第二个 daemon 随后绑不上端口——症状与真正的原因隔了两层。

### 修法

`args.no_visual` 取代 `args.visual`；补齐三个未转发的选项。

### 影响面

这是**最常用**的启动方式：`chrono doctor` 的两处诊断建议都写着
`start with: chrono daemon --background`，README 与 `docs/agent-setup.md` 也是这么教的。

---

## 问题 2 ★ MCP 工具的参数 schema 全部丢失（已修）

### 现象

第一次按文档调用：

```json
mcp({ tool: "chrono_cue", args: { "instruction": "拍照", "target_in_s": 20 } })
```

```
Failed to call tool: data must have required property 'args'
```

必须**再包一层**才对：`args: { args: { ... } }`。

### 根因

`TOOLS` 里每个工具都手写了完整 JSON Schema，但 `build_mcp_server()` 调用
`mcp.add_tool(fn, name=..., description=...)` 时把 `spec["schema"]` 丢掉了。

而 `FastMCP.add_tool` / `MCPServer.add_tool` **没有 `inputSchema` 参数**：
`Tool.from_function()` 固定走 `func_metadata(fn).arg_model.model_json_schema()`，
schema 只能从签名推导。工具函数的签名是 `def tool_cue(args: dict[str, Any])`，
推导出的 schema 自然就是「需要一个叫 `args` 的 object」。

### 后果

- 调用方（agent）**看不到任何参数文档**：`target_in_s`、`lead_s`、`ioi_ms`、
  `tail_beats`、`expect_source`、`prepare_only` 完全不可发现，只能去读源码。
- 每次调用都要多包一层无意义的 `args`。
- 那 200 行手写 schema 是**死代码**。

### 修法

选「让签名本身携带 schema」：每个工具改成显式参数，描述写在
`Annotated[..., Field(description=...)]` 上，嵌套结构用 `TypedDict`（其 docstring
会成为 `$defs` 里的 description）。`TOOLS` 表退化成 `{name: fn}`，schema 与实现
只有一份，改默认值会同时改 schema 与行为，不可能漂移。

新增 `published_tool_schemas()`：向 server 要它**实际发布**的 schema。
`e2e_panel.py` 断言 `chrono_cue` 有 13 个属性、必填项只有 `instruction`——
「schema 从签名推导」这句话已经错过一次而没人发现，问服务端是唯一不会撒谎的检查。

---

## 问题 3 `mcp({connect})` 之后仍报 "not connected"（上游文案，无功能影响）

### 现象

`mcp({ action: "connect", server: "chrono-handoff" })` 返回的文案说还没连上，
但紧接着调 `chrono_cue` 直接成功。

### 归属

这是 **pi 的 MCP adapter 文案问题**，不是 chrono 的 bug。`lifecycle: "lazy"` 下
`connect` 只刷新元数据缓存，实际连接在首次工具调用时才建立，但返回文案没区分两种状态。

### 影响

会让人误以为连接失败而重复调用 `connect`。实际无功能影响，本项目不改。

---

## 问题 4 `status` 恒为 `timeout`，容易被误判为失败（已修）

### 现象

拍摄流程完整走完后查状态：

```bash
$ chrono status cue_d5aa63
{ "status": "timeout", "t_rel": 253.454606, "signal_seen": false, ... }
```

### 分析

设计上这是**正确**的：`chrono_cue` 不要求人按任何键，`duration_s` 到点后
「提示期结束」就记为 `timeout`。但字段名在自然语言里等于「失败」，
第一眼极易误判——当时的作者是靠 `signal_seen: false` 加上板子实际状态才确认流程正常的。

### 修法

不动状态机（`status` 是契约，改名要同步 `panel.html` / `lib.rs` / 文档 /
`CONTRACT_VERSION`），而是在快照里加一个**语义字段**：

| `status` | `verdict` | 含义 |
|---|---|---|
| `satisfied` | `ok` | 观测到人的动作（或 verify 通过） |
| `timeout` + 该 run 等信号 | `signal_missing` | 人没做，或没被观测到——这是失败 |
| `timeout` + 该 run 不等信号 | `ok` | 提示期正常结束——这是成功 |
| `partial` | `unverified` | 收到信号但未确认生效 |
| `aborted` | `cancelled` | 被取消 |

`verdict_reason` 带内部原因（`human_signal` / `no_signal` / `panel_cancel`…）。
`chrono status <run>` 现在先打印 verdict 再打印原始 JSON；
`run_end` 事件与 `_finalize` 的日志行同样带上 verdict。

---

## 问题 5 面板同时只能显示一个 cue（已修：串行化）

### 现象

想给 6 个拍摄状态各下发一个 cue，发现后一个会覆盖前一个。

### 根因

`webui.py` 的 `_current_run` 是单值，`_on_armed()` 每次覆盖；conductor 也同时
在跑两条时间线。真正的问题不是「显示」而是**时钟**：人只有一双眼睛，
两条同时跑的 cue 不是两个提示，而是第二个把第一个从屏幕上顶掉。

### 当时的绕过

把整条时间线写进**一个** cue 的 `instruction`：

```
+0s   三颗全灭 → led_all_off.jpg
+30s  三颗全亮 → led_all_on.jpg
...
+150s 三颗闪烁 → 录 10 秒视频
```

机器侧按同一张表自动执行，实测对齐误差 **1.2 秒**。这个用法现在依然成立，
而且仍然是「单人单线」场景的推荐做法——人不用重新进入节拍。

### 修法：conductor 串行化

`chrono_cue` / `chrono_submit` 新增 `queue` 参数，**默认 true**：

- 后到的 cue 记为 `PENDING` + `queued_behind=<前一条>`，不 arm、不占屏幕。
- 前一条的**刺激**（不是 verdict——verdict 早于 tail）结束时自动 `go()`。
- 三级链条是链而不是扇入：C 排在 B 后面，而不是也排到 A 后面。
- `queue=false` 保留真正需要并行的场景（一人一条 cue）。
- 面板上的「立即开始」按钮同样受此约束——否则人自己就能把两条 cue 叠起来。
- 只对 `audio or visual` 的 plan 生效：机器侧独占的动作不会互相遮挡。

面板侧：

- `_current_run` 只在前一条到终态后才换人；`_pick_current()` 统一回答「现在该显示谁」。
- `/api/health` 与 `/api/wait-armed` 都返回 `queued: [...]`，页面在 guidance 行
  追加「还有 N 个提示排在后头，这个结束后会自动开始」——否则人做完第一件就走了。
- 排队中的 cue **不发布 schedule**（它还没有网格），页面按「已武装（未开始）」渲染。

### 顺带修掉的两个真 bug

1. **`/api/wait-armed` 从不解析 `since`。** 页面每次都带 `?since=<generation>`，
   服务端丢掉它 → 每次都立刻返回 `already: true` → 页面 400 ms 空转重取同一份
   schedule，队列数永远没有机会变化。
2. **`/api/events` 忽略 `run_id`。** 客户端问 B 的事件，拿到的是「当前 run」的。
   `e2e_panel_attach.py` 因此变红——它问的正是那条被顶掉的 run。

---

## 附：这套用法里**没有**问题的部分

值得记下来，因为这些是设计确实有效的证据：

| 项 | 实测 |
|---|---|
| cue 与机器侧动作的对齐误差 | **1.2 秒**（cue GO 在 t+20s，脚本 t+21.2s 切第一拍） |
| 6 个拍摄状态 × 30 秒 | 人全部按表拍完，无遗漏 |
| daemon 稳定性 | 全程未崩（除 `--background` 路径，见问题 1） |
| 置顶窗 | `--window --window-hidden` 工作正常，arm 时弹出 |
| `CHRONO_MCP_WINDOW=0` | MCP 进程不自己开窗，由 daemon 负责，符合预期 |

---

## 汇总

| # | 问题 | 性质 | 严重度 | 现状 |
|---|---|---|---|---|
| 1 | `--background` 崩 `args.visual` | 代码 bug | **高** | 已修（并补上三个未转发的选项） |
| 2 | MCP 工具 schema 全丢，`args` 要包两层 | 代码 bug | **高** | 已修（签名即 schema） |
| 3 | `connect` 后仍报 not connected | 上游 adapter 文案 | 低 | 上游问题，无影响 |
| 4 | `status` 恒为 `timeout` | 命名问题 | 低 | 已修（新增 `verdict`） |
| 5 | 面板同时只能显示一个 cue | 设计限制 | 中 | 已修（conductor 串行化 + 面板队列提示） |
| 6 | 前端把「按时做事」讲成「按 GO 键」 | **产品表述错位** | **高** | 已修（指令为主角、按钮条件出现、遥测折叠） |
| 7 | 层级的三处实测残留（枚举名 / tail 过大 / 抢注意力） | 视觉层级 | 中 | 已修（人话映射、tail 降级、时钟按行数缩放） |

回归覆盖：`chrono selftest`（10 项）、`scripts/e2e_panel.py`（新增队列、
verdict、schema 三组断言）、`scripts/e2e_panel_attach.py`、`e2e_split_mode.py`、
`e2e_daemon_window.py`、`e2e_ui_layout.py`（新增界面层级 8 条）。

---

## 问题 6 ★ 前端把「按时做事」讲成了「按 GO 键」（已修）

### 现象

项目要表达的是 **agent 通知人「在 T 时刻做 X」**，GO 只是时间基准。但面板从头到尾
把它表达成 **「盯着倒计时，在 GO 上按这个按钮」**——人被训练成对刺激做反应，
而项目真正要的是**按指令行事**。

实测量到的层级（默认 460×524 窗口）：

```
instruction  16 px   21 px 高   ← 人真正要读的那句话
clock        58 px   55 px 高   ← 倒计时
#press       15 px   51 px 高   ← 一个没人要求他按的按钮
.meter     10.5 px   97 px 高   ← 遥测表，是指令的 4.6 倍高
```

**指令是卡片里最不显眼的内容之一。** 对一个"告诉人做什么"的工具，这个比例是反的。

### 根因（四处，同一个错位）

| 位置 | 文案/做法 | 问题 |
|---|---|---|
| `panel.html:514` | 主按钮 `GO 时按下`，51 px 占满整行 | 面板最大的可交互面积在说"按我"，无论 agent 让人做什么 |
| `panel.html:1023` | 传感器观测时 `按上面说的做` | 更露骨：人真正的任务被降级成"上面说的"，按钮才是主角 |
| `panel.html:1038` | `听脉冲，重拍就是 GO，踩在它上面按下。` | 唯一操作指导，两分支都以"按下"结尾 |
| `cli.py` demo 默认 | `Press the button on the GO beat` | 新人第一眼看到的就是"按按钮" |

### 顺带查出的两个真 bug

1. **多行 instruction 被压成一行。** `.instr` 是 `white-space: normal`，而
   `docs/usage-findings.md` 问题 5 推荐的主用法就是把整条时间线写进一条
   instruction。实测 4 行源文本渲染成 3 行，`+30s` 那一项**根本没有换行**
   （left=253，应等于 text_left=26）。人看到的是一条挤在一起的流水句。
2. **结束状态说 `timeout`。** 上一轮给 API 加了 `verdict`，但页面仍在用
   `e.detail.status`，人看到的还是最易误读的那个词。

### 修法

| 项 | 改动 |
|---|---|
| 指令 | `clamp(19px,3.6vmin,32px)` + `white-space:pre-line`（实测 4 行 → 4 行） |
| 主按钮 | 只在 `expect_source ∈ {none,http,inject}` 时渲染；传感器观测时 `body.sensor-owned` 使整块 `display:none`，指令占用那片空间 |
| 按钮尺寸 | 51 px → 42 px，字号降一档：它是靶子，不是控制台 |
| 遥测 | 默认折到 1 行（52 px），其余点「遥测」展开；真按钮 + `aria-expanded`，不是悬停 |
| 文案 | `在那一刻做上面那件事`，不再以"按下"结尾 |
| 结束态 | 用 `verdict` → `已完成 / 没收到动作 / 已取消` |
| demo 默认 | `在重拍那一刻按一下开发板上的 RESET 键`（落到具体物体，不再教人按面板） |

回归：`scripts/e2e_ui_layout.py` 新增 8 条断言（字号、`pre-line`、多行不裁、
折叠行数、`aria-expanded`、传感器态隐藏按钮、guidance 指向指令），
总数 18 → 33 项。同时修好了一条被削弱的旧断言：折叠行 `getBoundingClientRect().right`
恒为 0，`<= innerWidth` 永远成立——现在要求 `width > 0`。

---

## 问题 7 界面层级的三处实测残留（已修）

问题 6 改完之后把真实窗口跑起来截图，又量出三处：

| 项 | 实测 | 问题 |
|---|---|---|
| guidance | `机器会在 console_key 上确认。` 折成 **3 行** | 内部枚举直接印在人读的那行上；太长把倒计时往下挤 |
| GO 后读数 | `+22.8` 在 **40 px**（倒计时的 37%） | 动作早结束了，这个数字仍是卡片第二大元素 |
| 时间线场景 | 倒计时 **107 px** vs 指令 19 px | 四行时间线上方顶着个大数字，眼睛先落在数字上 |

### 修法

- **`SOURCE_WORDS` 映射表**：`console_key → 控制台按键`、`stdin_line → 终端输入`、
  `serial → 串口`、`http/none → 面板`、`sim → 模拟源`；未收录的回退原字串
  （宁可显式无名，不要静默错标）。句子同时缩短：`做完就行，机器在${word}上确认。`
- **`#clock.tail` 从 `clamp(26,9vmin,72)` 降到 `clamp(18,4.6vmin,34)`**：实测 40 px → 20.5 px
  （倒计时的 19%）。
- **`--clock-scale` + `fitInstruction()`**：时钟按**实测行数**缩放
  （1 行 1.0 / 2 行 0.8 / 3 行 0.7 / 4+ 行 0.6）。分档而非连续——一个随文字宽度跳动的
  倒计时正是这个组件最不能有的东西。按 `getBoundingClientRect().height / lineHeight`
  量，不数 `\n`：长单行折行也要算进去。

### 顺带修的工具缺陷

`scripts/_shot_window.py` **抓不到 daemon 的窗口**：它只按进程映像名匹配，而
`chrono daemon --window` 跑在 `python.exe` 里。窗口明明在屏幕上，工具答
「no visible window found for process 'chrono-handoff'」。

新增 `--pid`（pid 不可能被页面标题伪造）与 `--title`（**精确**匹配，
`chrono-handoff - Chrome` 正是必须永远不截的那个窗口）。按进程名仍是默认路径，
匹配不到时才回退到标题。

### 一条被削弱的旧断言

遥测折叠后 `getBoundingClientRect().right` 恒为 0，于是「密集导轨下每个遥测值仍在屏内」
那条检查的 `right <= innerWidth` 永远成立——它会在布局真坏时变绿。现在要求
`width > 0`，并在测量前先把遥测展开。
