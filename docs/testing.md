# 测试与 CI

这套测试的重点不是"元素存在"，而是**读数**：字号、行数、相位、像素差占比、毫秒。
断言方式一旦退化成"有个元素/没报错"，它就会在界面实际坏掉时保持绿色。

## 一、套件分工

| 套件 | 断言什么 | 需要桌面会话 |
|---|---|---|
| `selftest` | 时钟/调度/音频/IPC 机制，无人工参与 | 否 |
| `e2e_panel` | HTTP 全链路：时钟对齐、排程、事件、排队、verdict、墙钟锚定、MCP schema | 否 |
| `e2e_panel_attach` | 面板附加到别人的 daemon | 否 |
| `e2e_window` | pywebview 原生窗口：桥、设置、按压、任务栏图标 | 是 |
| `e2e_ui_layout` | **几何**：不裁切、导轨放得下、窗口控件可命中 | 是 |
| `audit_panel` | **阅读顺序**：指令字号 > 周边字、多行保留、倒计时让位、遥测折叠 | 是 |
| `e2e_resize` / `e2e_theme_drag` | 拖拽缩放、对比度（依赖合成鼠标输入） | 是 |
| `e2e_daemon_window` / `e2e_split_mode` | 一体化 / 分离模式 | 是 |
| `e2e_desktop_shell` | Tauri 外壳：CORS 契约、版本闸、更新 seam、几何持久化 | 是 |

`e2e_ui_layout` 与 `audit_panel` 是两套不同的主张：前者管"有没有被裁掉"，
后者管"人先看到的是不是那句话"。历史上所有严重缺陷都是后者能抓、前者抓不到。

## 二、四类假失败

### 1. 尺寸/DPI：不要断言像素

窗口携带的大图标取决于桌面 DPI，同一个图像会以**重采样**版本返回。
按字节比较 48×48 的位图在一台机器上通过、在 100% DPI 的 CI runner 上失败。

判据要写成**关系**而不是像素：多个尺寸各画一遍，按"颜色差 >48/255 的像素占比"取最优，
阈值放在实测的两端之间（本项目：有图标 0.00–0.02，掉回默认图标 0.66，取 0.05）。

### 2. 一帧滞后：DOM 文案比 JS 状态慢

`applySchedule` 立刻设置 `S.runId`/`S.sched`，但 `#clock`/`#phase` 的文案要等下一帧
`tick()` 重画。于是"等待页面进入某相位"的条件可以被**上一条 cue 的残留文案**满足，
采样就落在新 cue 的 lead-in 上——读数 58px，而判据期望 107px，失败信息却指向布局。

两个要求：

- 等待条件**跨帧成立两次**才算到达（本项目用 0.15s 二次读取）。
- 等待的返回值必须被**断言**。超时是测量失败，不是"继续往下测"。

### 3. 环境残渣：失败指向产品，其实指向机器

| 残渣 | 症状 |
|---|---|
| 旧版冻结 sidecar（`desktop/src-tauri/target/debug/chrono-core.exe`） | 外壳优先拉起它，contract 不匹配，版本闸拒绝，后续每条检查都在测另一个应用 |
| 还开着的 daemon 占着端口 | 新窗口被接管，读数出现 `clock='准备'` 之类 |
| 上次运行留下的 `logs/_home/`、窗口位置 | 几何、设置类检查在测残留状态 |

所以：本地跑窗口套件前先关掉自己的 daemon、清 `logs/_*`；Shell 套件遇到版本闸失败时
**先看 `[page] guard live ...` 那行的 note**，它写着谁被拒绝了、为什么。

失败信息必须自己说出原因。`(no line)` 不算信息——它把"版本闸拒绝了旧 sidecar"报成了
"某个断言没匹配"。

### 4. 硬编码派生值

测试里写死 `contract=2`，而 `CONTRACT_VERSION` 已经 3→4 两次，检查就因为一个无关改动变红。
**凡是代码推导出来的值，测试也从代码取**（`from chrono_handoff import CONTRACT_VERSION`）。

## 三、改断言的纪律：先校准尺子

"对看起来正确的代码测出错结果"时，第一嫌疑人是测量工具：

1. 在本地复现。复现不了就**反向验证**：故意把被测行为去掉（去掉 `icon=`、把双重读取改回单次），
   确认断言会红。红了才算这条断言真的在测东西。
2. 两端都量一遍（有/无），阈值放在两点之间，并写进注释。
3. 只有确认是产品缺陷才改产品；否则改判据。

同一个原则写在全局 `AGENTS.md` 的「调试纪律」里，这里的例子是它在测试上的具体形态。

## 四、CI

九个 job：`core`（三平台跑类型检查与无桌面套件）、`frozen conductor`（三平台冻结并自检）、
`shell compiles off Windows`、`windows window suites`（跑所有窗口套件）。

- 类型检查是硬要求：`basedpyright src scripts desktop/sidecar` 必须 0 error。本地就能跑，
  不要靠 CI 反馈。
- 时间敏感的断言在 CI 上用 `CHRONO_TOLERANT_TIMING=1` 打印数字而不判定（共享 runner 上
  定时器会晚 100ms+）。这类断言要写清"这是 runner 的精度，不是产品指标"。
- CI 上的窗口 runner 与本地差异最大的两点是 **DPI** 和**首帧时机**，上面第 1、2 条就是它们。

## 五、本地跑一遍

```bash
python -m chrono_handoff.cli selftest
python scripts/e2e_panel.py
python scripts/e2e_ui_layout.py
python scripts/audit_panel.py
python scripts/e2e_window.py
python scripts/e2e_desktop_shell.py     # 需要先 cargo build 外壳
basedpyright src scripts desktop/sidecar
```

窗口套件彼此抢端口（8850 起），逐个跑，不要并行。
