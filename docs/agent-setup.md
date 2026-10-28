# 接入 agent

> **一句话**：三步 —— 装好 → 起 daemon → 把 MCP server 注册进 pi。之后 agent 就有一个 `chrono_cue` 工具，调用时会弹出一个圆角悬浮的倒计时窗口，人踩着重拍操作，时刻被精确记录。

---

## 一、装

项目零运行时依赖，但**两件事必须做对**：用哪个 Python，以及装进哪个解释器。

### 一.0 Python 用 3.13+（强烈建议，有实测依据）

代码在 3.10+ 都能跑（`pyproject.toml` 也是这么声明的），但对本项目这个
**靠计时精度立身**的工具，3.13 的线程调度抖动比 3.11/3.12 好很多。

同一台机器、同一次测试（`selftest` 的 under-load 项：2 个 CPU 密集线程抢占下的 40 拍落点）：

| Python | 突破 20ms 门槛 | median | worst 最大 |
|--------|--------------|--------|-----------|
| 3.11.15 | 3/20 | 2.834 ms | 32.08 ms |
| 3.12.13 | **4/20** | 3.005 ms | 26.96 ms |
| **3.13.12** | **0/20** | **0.345 ms** | **11.83 ms** |

三个都是 uv 管理的同族 build，排除了构建 flag 与安装方式的干扰；交替重跑结论一致。
3.12 有约 20% 概率让 `chrono selftest` 间歇性失败——**是真的抖动，不是测试问题**。

```bash
cd /b/vibe_program/chrono-handoff
uv python install 3.13
uv venv .venv --python "$(uv python find 3.13)"
uv pip install --python .venv/Scripts/python.exe -e ".[all]"
```

之后**一律用 venv 里的解释器**（激活，或打全路径）：

```bash
.venv/Scripts/chrono doctor        # 应看到 panel window: native
.venv/Scripts/chrono selftest      # 10 项机制自检，应 10/10
```

> `uv venv` 重复执行会报错（不是幂等的），要换版本得先 `rm -rf .venv`。

### 一.1 本机 `uv` 的已知坑（MSYS2 python 抢 PATH）

本机系统 PATH 第 5 位是 `B:\MSYS2\ucrt64\bin`，里面的 `python3.exe` 带
`mingw_x86_64_ucrt_gnu` 平台标记。`uv` 在**需要自己发现 Python**时（即当前目录
还没有 `.venv`）会从 PATH 头部拿到它，然后直接失败：

```
error: Failed to inspect Python interpreter from first executable in the search path
  Caused by: Can't use Python at `B:\MSYS2\ucrt64\bin\python3.exe`
  Caused by: Unknown operating system: `mingw_x86_64_ucrt_gnu`
```

**实测无效的绕法**（不用试）：`UV_PYTHON_PREFERENCE`（四个取值全没用）、
`UV_NO_SYSTEM_PYTHON=1`、`UV_PYTHON=…`、`uv.toml` 里的 `python-preference`、
`.python-version`。因为 uv 是**在解析 `--python 3.13` 这个版本号时就去做发现**，
那个阶段不受这些配置控制。

**有效的做法**：

1. 让 uv 自己下载并管理一个解释器，**避开 PATH 发现**：
   ```bash
   uv python install 3.13          # 落到 `uv python dir` 下，不碰 PATH
   uv venv .venv --python 3.13
   ```
   若本机已经有 uv 管理的解释器（`uv python list --only-installed`），
   直接给它完整路径最省事：
   ```bash
   uv venv .venv --python "$(uv python find 3.13)"
   ```
2. **`.venv` 一旦存在，这个错误就不再出现** —— uv 会直接用它，不再去发现。
   所以后续 `uv pip install` 带不带 `--python 3.13` 都行。
3. 若本机 PATH 里有个抢在前面的 mingw python（见 §一.1），**不要随便去改系统 PATH**：
   同一目录（如 `B:\MSYS2\ucrt64\bin`）往往同时装着 `gcc`/`make`/`cmake`，
   而且它和 Anaconda 的 GCC 运行时**同名不同 ABI**。把 Anaconda 插到它前面，
   会让 MSYS2 编译的程序加载错版本的 `libstdc++-6.dll`，直接报
   `0xC0000139 STATUS_ENTRYPOINT_NOT_FOUND`（已实测复现；本机 131 个 MSYS2
   程序依赖该 DLL）。

   **关键机制：Windows 的生效顺序是 `系统级 + ";" + 用户级`** —— 系统级整体在前。
   所以把 python 配在**用户级**，无论怎么排序都胜不过**系统级**里的 MSYS2。
   验证：`[Environment]::GetEnvironmentVariable('PATH','Machine')`。
   要改就改系统级，且只能**追加到末尾**（追加仍安全，插到前面会崩）。

   但即使追加到末尾，裸 `python` 也仍然命中 MSYS2（它在系统级更靠前）。
   想让裸 `python` 指向别处，只能把那个 python 从 MSYS2 里卸掉，
   而那会连带卸载依赖它的库。**结论：不动它，用 venv。**

> 判定方法：`chrono doctor` 的 `panel window` 行。它直接打出**当前解释器的
> 绝对路径**，一眼能看出装的和跑的是不是同一个。

### 一.2 没装 pywebview 会怎样

仍能跑：自动降级成浏览器标签页，**并在面板顶部明示**（琥珀色提示条）＋
`chrono doctor` 报 `panel window: browser tab` ＋ stderr 给出该装的解释器路径。
不会像以前那样静默地看起来像个网页应用。

独立悬浮窗口需要 `pywebview`（Windows 11 自带 WebView2 运行时）。

---

## 二、起 daemon（常驻）

```bash
chrono daemon --window --background
```

这一条会启动**一个进程**，里面同时有：

| 组件 | 作用 |
|------|------|
| conductor（时钟） | 所有计时/触发的权威 |
| IPC 服务 | 8791 端口，给 MCP server 用 |
| **原生窗口** | 常驻悬浮挂件，cue 触发时自动抬起 |
| HTTP 面板 | 8790 端口，备用（也可手机同局域网开） |

启动输出会打印面板地址和 pid。**关掉窗口不影响出提示**（窗口只是显示器，conductor 是时钟——见 §四）。

先手动试一次：

```bash
chrono demo --instruction "按一下开发板上的 RESET 键" --in 6 --paused
```

> `chrono demo` 会同时起面板并装填一次提示（`--paused` 表示等面板上的 Start 键）。
> 若已有 daemon 在跑，用 `chrono submit` 提交计划，或 `chrono panel` 只开面板。

窗口弹出并进入 `prepared` 状态，点 **Start** 开始倒计时。

---

## 三、注册进 pi

编辑 `~/.pi/agent/mcp.json`，在 `mcpServers` 里加一段：

```json
{
  "mcpServers": {
    "chrono-handoff": {
      "command": "chrono",
      "args": ["mcp"],
      "directTools": false,
      "lifecycle": "lazy",
      "searchKeywords": {
        "*": ["human in the loop", "countdown timer", "operator cue", "timing", "schedule"],
        "chrono_cue": [
          "ask human physical action",
          "pop up panel",
          "press button",
          "reset device",
          "notify operator now"
        ],
        "chrono_submit": [
          "declarative timeline",
          "schedule serial write",
          "action window sweep",
          "expect signal pattern"
        ],
        "chrono_observe": ["read events journal", "poll run result", "check status progress"],
        "chrono_wait": ["bounded wait snapshot", "block until verdict"],
        "chrono_cancel": ["abort run stop"],
        "chrono_status": ["list runs health stats"],
        "chrono_calibrate": ["human offset learning", "latency compensation store"]
      }
    }
  }
}
```

保存后在 pi 里 `/mcp` 看连接状态，或 `mcp({ search: "countdown" })` 试检索。

**没装包时**（不想 `pip install -e .`），把 `command`/`args` 换成：

```json
"command": "<repo>/.venv/Scripts/python.exe",
"args": ["-m", "chrono_handoff.mcp_server"],
"env": { "PYTHONPATH": "<repo>/src" }
```

（把 `<repo>` 换成仓库的实际位置。这里不写现成路径：本机绝对路径不该进
公开仓库，而写一个人的反而最容易被照抄。）

> 指向项目 venv，别指向全局解释器：MCP server 装在哪，`pywebview` 就得在哪。
> 用错解释器时 agent 仍能出提示，只是窗口降级成浏览器标签页，且难以察觉。

### 为什么 searchKeywords 用纯英文

pi 的 adapter 用 `split(/[^a-z0-9]+/)` 分词，**非 ASCII 会被直接丢弃**——写中文关键词等于没写。这套关键词只影响 `mcp({search})` 的排序，不进 prompt/schema，零上下文成本。

---

## 四、agent 怎么用（以及为什么这样设计）

agent 看到的是 7 个工具，最常用的是 `chrono_cue`：

```
chrono_cue(
  instruction = "按一下开发板上的 RESET 键",
  target_in_s = 6,           # 6 秒后该动手
  lead_s      = 3,           # 倒计时 3 秒
  ioi_ms      = 500,         # 拍距
  tail_beats  = 6
)
→ { run_id, status: "running", hint: "the cue runs without you; poll chrono_observe" }
```

**它立刻返回，不等人类。** agent 应该结束本 turn，稍后 `chrono_observe(run_id)` 拿结果。

参数直接写在工具签名上（`Annotated[..., Field(description=...)]`），
agent 看得到每个字段的说明与默认值，不需要额外包一层 `args`。

### 一次只响一条

`queue` 默认为 true：屏幕上还有 cue 在跑时，新的一条记为 `pending` 并排在它后面，
等前一条的拍子播完自动开始，返回值里 `queued_behind` 说明排在谁后面。
人只有一双眼睛，两条同时跑的 cue 不是两个提示——第二个会把第一个从面板上顶掉。
真正需要并行时（一人一条）传 `queue=false`。

**段间隔看拍子，不看 `duration_s`。** 不观测人的 cue 在最后一个拍子落地就收尾，
所以连续多段提醒的间隔由 `target_in_s` + `tail_beats × ioi_ms` 决定，而不是预算。
想要「每段固定间隔」，就让各段的目标时刻递推：第 n 段 `target_in_s = 上一段时长 + 间隔`。

### `expect_source`：默认只提醒

| 值 | 谁在观测 | 面板上有什么 |
|---|---|---|
| 不传 / `none` | 没人 | 什么都没有，只提示时刻 |
| `http` | 面板 | 一行可选的「记下我做的时刻」 |
| `serial` / `console_key` / `stdin_line` / `sim` | 硬件/终端 | 什么都没有，机器看硬件 |

组件的常见用法是「在 T 做 X」，不是「在 T 按一下」。所以默认不观测、不记录，
面板也不会把人引向自己。要拿到人的时刻就显式传 `expect_source="http"`。

### `status` 不是结论，`verdict` 才是

| `status` | `verdict` | 意思 |
|---|---|---|
| `satisfied` | `ok` | 看到人的动作（或 verify 通过） |
| `timeout` + 这条 run 等信号 | `signal_missing` | 人没做，或没被观测到 |
| `timeout` + 这条 run 不等信号 | `ok` | 提示期正常结束——**这是成功** |
| `partial` | `unverified` | 收到信号但未确认生效 |
| `aborted` | `cancelled` | 被取消 |

一个不要求人做可观测动作的 cue 到期会记成 `status: "timeout"`，那只是状态机
走到的终态；它是否按计划结束看 `verdict`。分支判断请用 `verdict`。

为什么这么设计 —— 这正是 README §一 的核心论点：

| | 事件触发（错误做法） | 时间触发（本组件） |
|---|---|---|
| 耦合 | 人做完 → 通知 agent → agent 反应 | 双方**提前约定时刻** |
| LLM 延迟（5–30s）| 在关键路径上，**注定错过秒级窗口** | 被提前提交的计划抵消 |
| 执行者 | agent（慢） | 独立进程（毫秒级） |

所以工具**绝不阻塞等人**。阻塞在那里就退化回了"通知 agent → agent 反应"，整个项目的价值归零。

### 提示如何送达人类

1. `chrono_cue` → daemon 的 conductor 解析出整份时刻表
2. `on_armed` 事件广播给**多个**订阅者（见下）
3. 原生窗口收到后**自动抬起**（即使最小化了）
4. 页面用 `AudioContext` 在本地按样本精度播拍点 —— 时序不在服务端逐拍推送

### 一个刚修掉的真 bug

`on_armed` 原来是**单槽**的：后来注册的会**静默覆盖**先注册的。加了窗口的自订阅之后，HTTP 面板就收不到时刻表了 —— 页面看起来"只是还没来 cue"，实际 cue 正在跑。现在改成了多订阅者列表，谁也挤不掉谁。

---

## 五、两种拓扑

| | A 一体化：`daemon --window` | B 分离：`daemon` + `window --attach` |
|---|---|---|
| 进程 | 1 | 2 |
| 关掉窗口后 cue 照样触发 | ❌ | ✅ |
| 窗口崩了影响 conductor | 会 | 不会 |
| 适合 | 演示、手工 | 真用 |

分离模式里窗口是 `RemoteConductor`（只读显示器），**不能** arm/cue —— 两个 conductor 会让每拍响两次。

---

## 六、安全与边界

| 风险 | 现状 |
|------|------|
| agent 让人做危险动作 | `instruction` 只是文字提示；**真正的门控应放在执行前**（如 harness hook / 动作白名单），组件本身不阻止 agent 写危险指令 |
| agent 绕过工具不调用 | 组件无法强制；这是所有 HITL 工具的共性（见 related-work §A 的 Pushary 警告） |
| 重复执行副作用 | `on_window` 扫射天然幂等设计要求；串口动作请自行保证幂等 |
| 提示被忽略 | `duration_s` 兜底，到点结束而非挂起；`verdict` 说明这是否算成功 |
| 两条 cue 叠在一起 | `queue` 默认串行化，后一条等前一条的拍子播完 |
| 把人引成“按按钮” | 默认 `none` 只提醒：不观测、不记录，面板上不出现任何可点的东西 |

---

## 七、验证

```bash
chrono selftest                 # 机制自检（10 项）
python scripts/e2e_panel.py     # 面板 HTTP 全链路（59 项）
python scripts/e2e_window.py    # 原生窗口（17 项）
python scripts/e2e_ui_layout.py # 布局几何 + 界面层级（40 项）
python scripts/audit_panel.py   # 界面读数审计：字号/行数/占比（20 项）
python scripts/e2e_resize.py    # 拖拽缩放（10 项）
python scripts/e2e_theme_drag.py# 主题对比度 + 拖拽隔离（16 项）
python scripts/e2e_daemon_window.py # 一体化模式（11 项）
python scripts/e2e_split_mode.py    # 分离模式（13 项）
```

`audit_panel.py` 与 `e2e_ui_layout.py` 管的是两件不同的事：前者断言**阅读顺序**
（指令是否比所有周边字大、多行是否保留、倒计时是否让位、结束读数是否降级），
后者断言**几何**（不裁切、导轨放得下、窗口控件可用）。前者专门盯一类后者永远
抓不到的缺陷：全部“元素存在”与“没有溢出”都通过，而指令却是卡片上最不显眼的字。

---

## 八、排错

| 现象 | 原因 / 处理 |
|------|------------|
| `chrono: command not found` | 没装包。用 `python -m chrono_handoff.cli …` 或 `pip install -e .` |
| `a conductor is already answering` | 已有 daemon 在跑。`chrono status` 查看，或杀掉 pidfile 里的 pid |
| cue 弹了但窗口没抬起 | 升级到最新版（hosted 模式曾漏掉 armed 订阅） |
| 面板字体不对 | 确认 `http://127.0.0.1:8790/fonts/inter-latin.woff2` 能打开；中文需本机有 Noto Sans SC |
| 窗口开在屏幕外 | 已自动拉回；如仍发生删掉 `~/.chrono-handoff/settings.json` |
| 面板红字写着"不是同一次构建"、拒绝起拍 | 窗口与 conductor 来自两个安装包（升级不清理旧文件）。重装一次，让两半来自同一个安装包。`chrono doctor` 会报出两边各是什么 |
| 改了设置又弹回、提示"保存失败" | `settings.json` 是更新版本写的（降级后会出现），本构建**拒绝覆盖**它。那条消息里写着 `schema n`。升回新版本，或把该文件移到一边 |
| 想换端口 | daemon/MCP 都认 `CHRONO_IPC_PORT`；面板用 `--panel-port` |

不想自动拉 daemon 的话：`CHRONO_AUTOSTART=0`，然后自己 `chrono daemon --background`。

自动拉起时不想弹窗（如 MCP 健康检查、`/reload` 触发）：`CHRONO_MCP_WINDOW=0`。
注意这会**一并放弃控制台**（无键盘输入、无提示音）—— daemon 需要真控制台才能读键和蜂鸣。
若仍要窗口但不想它一启动就抢焦点：`CHRONO_MCP_WINDOW=hidden`（cue 触发时再显现，控制台保留）。
