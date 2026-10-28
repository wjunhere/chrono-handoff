# chrono-handoff

> 时间锚定的人机动作交接组件 —— 让 AI agent 驱动人类在指定时刻执行物理动作。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#要求)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## 概述

LLM agent 无法直接作用于物理世界，而人类操作者的动作延迟不可预测。若采用
**事件触发**耦合（人完成动作 → 通知 agent → agent 推理 → agent 下发指令），
LLM 的单轮推理耗时（5–30 s）将完整地落在关键路径上，无法满足秒级时序窗口。

本组件将耦合方式改为 **时间触发**：agent 提交一份声明式的动作计划
（时刻，动作）序列，由独立于 agent 会话生命周期的 **conductor 进程**持有
单调时钟并按绝对时刻执行；倒计时面板作为人类操作者的时间基准，传感器回读
用于闭环确认。

核心约束：**conductor 不得阻塞等待 LLM**。agent 调用即在毫秒级返回，
计划提交完成后即可结束会话轮次。

## Quickstart

### 要求

- Python 3.10+（**建议 3.13+**：计时抖动更低，见安装说明）
- 可选：`pywebview`（独立原生窗口）、`pyserial`（串口观测）

### 安装

```bash
git clone https://github.com/wjunhere/chrono-handoff.git
cd chrono-handoff

# 建议 3.13+：实测抖动更低，3.12 会让 selftest 间歇性失败（见 docs/agent-setup.md §一.0）
uv venv .venv --python "$(uv python find 3.13)"
uv pip install --python .venv/Scripts/python.exe -e ".[all]"

.venv/Scripts/chrono selftest      # 机制自检，预期 10/10
```

> 用独立环境，别装进全局解释器：**装和跑必须是同一个解释器**，否则原生窗口会
> 降级成浏览器标签页。`chrono doctor` 的 `panel window` 行会核对这点，并在缺失时
> 直接给出该装的解释器路径。
> 没装 `uv` 就用自己的 conda：`conda create -n chrono python=3.13`。

**安装选项**

| 命令 | 内容 |
|---|---|
| `pip install -e .` | 仅核心（计时引擎 + CLI + 浏览器面板） |
| `pip install -e ".[mcp]"` | 加 MCP 接入 |
| `pip install -e ".[audio]"` | 加跨平台采样级音频（`sounddevice`） |
| `pip install -e ".[all]"` | 全部（MCP + 音频 + 原生窗口 + 串口） |

### 启动

```bash
chrono doctor                       # 环境探测：时钟 / 音频 / 控制台 / 串口
chrono daemon --window --background # conductor + 原生提示窗口 + IPC
```

`--window` 托管常驻置顶窗口；cue 触发时窗口自动前置。

### 触发一次提示

```bash
chrono submit --json '{
  "id": "reset-demo",
  "instruction": "按一下开发板上的 RESET 键",
  "lead_s": 3,
  "ioi_ms": 500,
  "tail_beats": 6,
  "target_in_s": 8,
  "duration_s": 25
}'
```

返回 `run_id` 并立即退出；计划在 conductor 中执行。

面板上最显眼的是 `instruction`——**做什么**——倒计时是挂在它上面的时基。
面板没有主按钮：它的工作是把「在 T 做 X」这句话送到人眼前，不是请人点它。
`expect_source` 缺省是 `none`，意思是**这条只提醒**：没有人观测你，什么也不记录，
面板上不会出现任何可点的东西。要让人自己把时刻记下来，显式写 `"expect_signal":
{"source": "http"}`，面板底部才会多一个「记下我做的时刻（可选）」的小按钮；
真实传感器观测时它同样不出现。

```bash
chrono status reset-demo            # 当前状态
chrono observe reset-demo           # 增量读取事件日志
chrono cancel reset-demo            # 中止
```

### 接入 Pi agent

在 `~/.pi/agent/mcp.json` 中注册：

```json
{
  "mcpServers": {
    "chrono-handoff": {
      "command": "chrono",
      "args": ["mcp"],
      "lifecycle": "lazy",
      "searchKeywords": {
        "*": ["human in the loop", "countdown timer", "operator cue", "timing"],
        "chrono_cue": ["ask human physical action", "pop up panel", "press button", "reset device"]
      }
    }
  }
}
```

MCP 服务端为瘦客户端，首次调用时自动拉起 daemon（`CHRONO_AUTOSTART=0` 可关闭）。
自动拉起时的窗口形式由 `CHRONO_MCP_WINDOW` 控制：默认 `1` 显示窗口；
`hidden` 启动隐藏、cue 触发时再浮出；`0` 完全不建窗（同时不建控制台，因此放弃键盘输入与提示音，适合无人值守/自动化）。
完整步骤见 [`docs/agent-setup.md`](agent-setup.md)。

## 用法

### MCP 工具

| 工具 | 用途 |
|---|---|
| `chrono_cue` | 提示人类在指定时刻执行物理动作，立即返回 `run_id` |
| `chrono_submit` | 提交声明式动作计划（多动作 / 带窗口 / 带闭环） |
| `chrono_observe` | 增量读取运行事件日志 |
| `chrono_wait` | 有界等待，返回状态快照而非阻塞 |
| `chrono_status` | 单次运行状态或近期运行列表 |
| `chrono_cancel` | 中止在途运行 |
| `chrono_calibrate` | 读取 / 更新人类偏移估计 |

`chrono_cue` 调用示例：

```
chrono_cue(
  instruction = "按一下开发板上的 RESET 键",
  target_in_s = 6,     # 6 秒后动作应命中
  lead_s      = 3,     # 倒计时时长
  ioi_ms      = 500,   # 拍间距
  tail_beats  = 6      # 命中后脉冲延续
)
```

参数直接写在工具签名上，agent 看得到全部字段与默认值。
**一次只响一条**：如果屏幕上还有一条 cue 没跑完，新的会排在它后面，
等前一条的拍子播完自动开始（`queue=false` 可关掉）。人只有一双眼睛，
两条同时跑的 cue 不是两个提示，而是第二个把第一个顶掉。

返回值里 `status` 是状态机、`verdict` 是结论。`verdict: "ok"` 才是“按计划结束”——
一个不要求人做可观测动作的 cue 到期会记成 `status: "timeout"`，那是它的**成功**终态。

不传 `expect_source` 就是 `none`：只提醒，不观测。想让人自己把时刻记下来就传
`expect_source="http"`，面板才会多出那个可选的记录按钮；传传感器名（`serial`、
`console_key`）则机器看硬件，不看面板。

### CLI

| 命令 | 说明 |
|---|---|
| `chrono doctor` | 环境能力探测 |
| `chrono selftest` | 无人工参与的机制自检 |
| `chrono daemon` | 启动 conductor 常驻进程 |
| `chrono window` | 原生提示窗口（`--attach` 作为纯显示器） |
| `chrono submit` | 提交计划 |
| `chrono observe` / `status` / `wait` / `cancel` | 运行控制 |
| `chrono verify` | 标记动作已确认生效 |
| `chrono calibrate` | 人类偏移估计 |
| `chrono demo` | 交互式提示面板 + 一次提示 |
| `chrono panel` | 面板连接到运行中的 daemon |
| `chrono settings` | 查看 / 编辑设置 |
| `chrono mcp` | 运行 MCP stdio 服务端 |
| `chrono probe` | M1 实验：拍点栅格下的人类偏移测量 |

### 提示通道

| 通道 | 实现 | 适用 |
|---|---|---|
| 听觉 | 非阻塞音频，多种后端（见下） | 默认；不占用视觉通道 |
| 视觉 | 置顶窗口 / 浏览器面板 / 终端 | 噪音环境、听觉受限 |
| 触觉 | HTTP 源可对接移动端震动 | 视觉与听觉均不可用时 |

音频后端按精度分级，`chrono doctor` 会报告实际选中的后端：

| 后端 | 平台 | 实现 | 精度 |
|---|---|---|---|
| `sample` | 全平台 | 常驻 PortAudio 流，按帧偏移混音 | 采样级（`play()` 中位 0.006 ms） |
| `wav` | Windows | `winsound` 异步播放预渲染 WAV | 设备级（`play()` 中位 0.2 ms） |
| `cmd` | 全平台 | 外部播放器进程（`afplay` / `pw-play` / `aplay` / `ffplay`） | **近似**（进程启动 10–250 ms 抖动） |
| `bell` | 全平台 | ASCII BEL，由终端发声 | 未知 |
| `null` | 全平台 | 静音 | —— |

`--audio` 可强制指定后端；指定本平台不提供的后端会**报错而非静默替换**——
静默降级到 `cmd` 会让一次测量运行带上错误的精度等级。

## 部署拓扑

| 拓扑 | 命令 | 特性 |
|---|---|---|
| 分离（推荐） | `daemon --background` + `window --attach 8791` | 窗口仅作显示器，关闭不影响计划执行 |
| 一体化 | `daemon --window --background` | 单进程，适用于演示 |
| 浏览器面板 | `chrono demo` | 支持局域网内移动端访问 |

## 目录结构

```
src/chrono_handoff/
├── clock.py        单调时钟、亚毫秒落点、无漂移拍点网格
├── model.py        CuePlan / SignalSpec / WindowSpec / Event
├── audio.py        跨平台非阻塞音频（后端分级 + 采样级混音）
├── cue.py          提示通道（音频转出 + 终端倒计时）
├── inputs.py       观测源：控制台 / stdin / HTTP / 串口 / 合成
├── effectors.py    执行器抽象：人类 / 串口 / HTTP / 日志
├── calibrate.py    人类偏移估计（median + MAD + 截断）
├── conductor.py    快控制器：拍点网格、动作窗口、闭环、事件日志
├── daemon.py       常驻进程与单实例守卫
├── mcp_server.py   MCP 表面 + IPC 客户端
├── remote.py       跨进程 conductor 代理
├── webui.py        提示面板服务端
├── window.py       原生置顶窗口（WebView2）
├── winshell.py     Windows 11 原生外观（圆角 / 亚克力 / 主题描边）
├── settings.py     设置持久化
├── statics/        单文件前端 + 本地字体
├── probe.py        M1 实验协议
├── analyze.py      离线日志分析
└── cli.py          命令行入口
```

## 验证

```bash
chrono selftest                       # 核心机制，10 项
basedpyright src scripts desktop/sidecar   # 静态检查，要求 0 error（standard 模式）
python scripts/e2e_audio.py           # 音频后端：混音/选择/非阻塞不变量
python scripts/e2e_panel.py           # 面板 HTTP 全链路
python scripts/e2e_panel_attach.py    # 面板附加到 daemon（chrono panel）
python scripts/e2e_window.py          # 原生窗口
python scripts/e2e_ui_layout.py       # 布局几何
python scripts/e2e_resize.py          # 窗口缩放
python scripts/e2e_theme_drag.py      # 主题对比度与拖拽隔离
python scripts/e2e_daemon_window.py   # 一体化模式
python scripts/e2e_split_mode.py      # 分离模式
python scripts/e2e_state_paths.py     # 用户状态落点（与 cwd 无关）
python scripts/e2e_update_window.py   # cue 安全窗口：不安全时 409 拒，安全时 daemon 自退
python scripts/e2e_desktop_shell.py   # Tauri 桌面外壳（需先 cargo build）
cd desktop/src-tauri && cargo test    # 外壳几何算式（边方向/夹紧/累计位移/窗口位置与恢复）
python desktop/sidecar/build.py       # 冻结侧车并自检（--no-build 只自检，--stale-check 只比指纹）
```

这些在 CI 里自动跑（`.github/workflows/ci.yml`）：

| job | 平台 | 跑什么 |
|---|---|---|
| `core` | Ubuntu / Windows / macOS | `basedpyright`、`chrono selftest`、五个无头套件（`e2e_panel`/`e2e_panel_attach`/`e2e_state_paths`/`e2e_update_window`/`e2e_audio`）、以及**拆开 wheel 确认 UI 资源在里面** |
| `windows-ui` | Windows | `cargo build` + `cargo test`，以及需要桌面会话的六个窗口套件（含 `e2e_desktop_shell`） |
| `sidecar` | Ubuntu / Windows / macOS | 打包冻结侧车并跑它的**会失败**的自检：能力对等、PE subsystem、冻结内 `selftest`、真的把面板 serve 出来并**逐字节比对** |
| `shell-off-windows` | Ubuntu / macOS | 外壳能不能在 Windows 之外**编译**并通过几何算式测试 |

没覆盖的，写着以免误读：

- **外壳没有在 macOS/Linux 上“跑起来”过。** 最后一个 job 只证明能编译；真跑还需要各平台自己的侧车和真实桌面会话。
- **侧车自检只在 Windows 上验过。** Linux/macOS 那两个 job 的第一次运行就是它的第一次运行（PyInstaller 不能交叉编译，这恰好是它存在的理由）。
- **自检里的 `selftest` 是测时序的**，机器忙时会偶发失败（本机见过一次 `9/10`，重跑三次都 `10/10`）。失败会打出是哪一项 `[FAIL]`。
- 窗口套件的合成输入在某些机器上会掉事件，套件会把那类断言报成 `SKIP` 并汇总，**不会**报成通过——所以看到绿色也请看一眼有没有 SKIP。

## 性能

Windows 11 / CPython 3.13，详见 [`docs/implementation-notes.md`](implementation-notes.md)。

| 指标 | 实测 |
|---|---|
| 拍点落点偏差（中位 / p99） | 0.02 ms / 2.3 ms |
| 40 拍 @50 ms 累计漂移 | +0.01 ms（绝对格点，不累积） |
| 单拍音频调用开销 | `sample` 0.006 ms / `wav` 0.2 ms（非阻塞） |
| 跨平台后端节拍落点 | 中位 0.016 ms，与 Windows 原生后端等价 |
| 音频流欠载 | 0（60 s 连续运行） |
| 按压时刻记录精度 | 0.5 ms |
| 停滞网格行为 | 丢拍不爆发，不追帧 |

约束：GIL 是精度上限，CPU 密集任务须置于独立进程；音频设备输出延迟
（约 10–50 ms）尚未测量，该分量被吸收进校准偏移。

## 平台支持

计时核心为纯标准库实现，可移植。人类提示通道为 Windows 优先：

| 组件 | Windows | Linux / macOS |
|---|---|---|
| 计时核心、MCP、IPC | 完全支持 | 完全支持 |
| 浏览器面板 | 完全支持 | 完全支持 |
| 串口 / HTTP 观测 | 完全支持 | 完全支持 |
| 原生窗口外观 | 圆角 / 亚克力 / 主题描边 | 降级为系统默认外观 |
| 音频提示 | `wav` 或 `sample` | `sample`（需 `sounddevice`），否则 `cmd`/`bell` |
| 控制台按键观测 | 支持 | 降级为 stdin 行输入 |

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/agent-setup.md`](docs/agent-setup.md) | 接入 agent 的完整步骤 |
| [`docs/component.md`](docs/component.md) | 提示组件使用与集成 |
| [`docs/design.md`](docs/design.md) | 协议与接口设计 |
| [`docs/implementation-notes.md`](docs/implementation-notes.md) | 实现细节与实测数据 |
| [`docs/shell-contract.md`](docs/shell-contract.md) | 面板 ↔ 窗口宿主契约（换壳时唯一要改的地方） |
| [`docs/desktop-shell.md`](docs/desktop-shell.md) | Tauri 2 桌面外壳：现状、阻塞点与两条出路 |
| [`docs/update.md`](docs/update.md) | 自动更新：分工、cue 安全窗口、签名与发版 |
| [`docs/m1.md`](docs/m1.md) | M1 测量实验方法 |
| [`docs/theory.md`](docs/theory.md) | 理论依据（时间触发 / 人机节拍同步） |
| [`docs/problem.md`](docs/problem.md) | 问题定义与量化 |
| [`docs/usage-findings.md`](docs/usage-findings.md) | 实测踩坑记录（八个问题，均已有结论） |
| [`docs/related-work.md`](docs/related-work.md) | 相关项目调研 |
| [`case-studies/loongson-2k1000.md`](case-studies/loongson-2k1000.md) | 实例：龙芯 2K1000LA PMON 引导窗口捕获 |

## 设计要点

- **计划事前提交**：倒计时开始前，`(时刻, 动作)` 序列已注册在 conductor 中；
  倒计时归零不触发 agent 推理。
- **窗口而非单点**：计划可指定动作时窗并在窗内重复下发，由机器侧的重复
  尝试吸收人类抖动。
- **闭环确认**：`verify` 字段指定回读模式，确认动作实际生效。
- **人类执行器不可重放**：`HumanEffector.caps().repeatable = False`；
  缺失新计划时进入安全保持，而非重放上一次人类动作。

## 许可

代码 MIT（见 [LICENSE](LICENSE)）。`src/chrono_handoff/statics/fonts/` 下的字体文件
（Inter、JetBrains Mono）遵循 [SIL Open Font License 1.1](https://openfontlicense.org/)，
允许再分发与捆绑 —— 它们的声明放在 [THIRD-PARTY.md](THIRD-PARTY.md)，不插进 MIT 正文
（插进去会让 GitHub 认不出许可，而认不出的许可法律上读作“保留所有权利”）。
