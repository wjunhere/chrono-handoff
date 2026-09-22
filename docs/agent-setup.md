# 接入 agent

> **一句话**：三步 —— 装好 → 起 daemon → 把 MCP server 注册进 pi。之后 agent 就有一个 `chrono_cue` 工具，调用时会弹出一个圆角悬浮的倒计时窗口，人踩着重拍操作，时刻被精确记录。

---

## 一、装

```bash
cd /b/vibe_program/chrono-handoff
python -m pip install -e .
```

装完会有 `chrono` 命令（也把包加进了 Python 环境）。**先自检**：

```bash
chrono doctor        # 看 console / audio / pyserial 是否就绪
chrono selftest      # 10 项机制自检，应 10/10
```

> 没装 pywebview 也能跑（自动降级成浏览器标签页），但想要独立悬浮窗口请：
> `python -m pip install pywebview`（Windows 11 自带 WebView2 运行时）

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
"command": "python",
"args": ["-m", "chrono_handoff.mcp_server"],
"env": { "PYTHONPATH": "path/to/chrono-handoff/src" }
```

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
| 提示被忽略 | `duration_s` 兜底，到点返回 `timeout` 而非挂起 |

---

## 七、验证

```bash
chrono selftest                 # 机制自检（10 项）
python scripts/e2e_panel.py     # 面板 HTTP 全链路（20 项）
python scripts/e2e_window.py    # 原生窗口（17 项）
python scripts/e2e_ui_layout.py # 布局几何（18 项）
python scripts/e2e_resize.py    # 拖拽缩放（10 项）
python scripts/e2e_theme_drag.py# 主题对比度 + 拖拽隔离（16 项）
python scripts/e2e_daemon_window.py # 一体化模式（8 项）
python scripts/e2e_split_mode.py    # 分离模式（13 项）
```

---

## 八、排错

| 现象 | 原因 / 处理 |
|------|------------|
| `chrono: command not found` | 没装包。用 `python -m chrono_handoff.cli …` 或 `pip install -e .` |
| `a conductor is already answering` | 已有 daemon 在跑。`chrono status` 查看，或杀掉 pidfile 里的 pid |
| cue 弹了但窗口没抬起 | 升级到最新版（hosted 模式曾漏掉 armed 订阅） |
| 面板字体不对 | 确认 `http://127.0.0.1:8790/fonts/inter-latin.woff2` 能打开；中文需本机有 Noto Sans SC |
| 窗口开在屏幕外 | 已自动拉回；如仍发生删掉 `~/.chrono-handoff/settings.json` |
| 想换端口 | daemon/MCP 都认 `CHRONO_IPC_PORT`；面板用 `--panel-port` |

不想自动拉 daemon 的话：`CHRONO_AUTOSTART=0`，然后自己 `chrono daemon --background`。
