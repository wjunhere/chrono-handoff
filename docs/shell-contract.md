# 面板 ↔ 窗口宿主契约

> **一句话**：面板页与承载它的原生窗口之间只有一个接口，且**只包含窗口 chrome**；
> 其他一切走 HTTP。这个接口就是换壳（pywebview → Tauri / Qt）时唯一需要重写的东西。

## 一、为什么需要两条通道

面板页由**持有窗口的那个进程**通过 HTTP 提供（`CueWindow.url == CuePanel.url`）。
所以页面既能 `fetch("/api/*")`，又有一个宿主注入的异步 JS 对象。两者不可互相替代：

| 通道 | 内容 | 为什么 |
|---|---|---|
| **HTTP** `/api/*`（`webui.py`） | 设置读写、健康、校准、计划、事件、按键、取消 | 是**数据**，与窗口无关；服务端就是数据的属主 |
| **shell seam**（本文件） | 最小化 / 关闭 / 最大化 / 拖拽 / 缩放 / 置顶 / 不透明度 / 原生主题 | 是**对 OS 窗口的操作**；HTTP 服务端并不拥有窗口，它做不到 |

判据只有一条：**「这个操作是在改数据，还是在动窗口？」** 动窗口 → seam；否则 → HTTP。
把业务塞进 seam 会让门户重新绑死在某个框架上，这正是这次重构要消除的东西。

## 二、seam 的形态

宿主在页面里注入一个**带异步方法**的全局对象。页面通过探针列表发现它
（`statics/panel.html` 的 `SHELL_BACKENDS`）：

```js
const SHELL_BACKENDS = [
  ["pywebview", () => (window.pywebview && window.pywebview.api) || null],
  ["tauri", tauriShell],
];
```

| 宿主 | 注入位置 | 状态 |
|---|---|---|
| pywebview | `window.pywebview.api.<name>` | ✅ 已实现（Windows WebView2 / macOS WKWebView / Linux WebKitGTK） |
| Tauri 2 | `window.__TAURI__.core.invoke`，由 `tauriShell()` 包成命名方法 | ✅ 已实现，见 `docs/desktop-shell.md` |
| Qt（QtWebEngine + QWebChannel） | 注册对象的代理 | ⬜ 待写适配器 |

页面侧入口，全部在 `panel.html` 的一个区块内：

| 符号 | 语义 |
|---|---|
| `SHELL_BACKENDS` | `[名字, 探针]` 列表；探针返回对象即命中 |
| `shellBackend()` | 返回当前宿主名，或 `null`（纯浏览器标签页）。测试与 `chrono doctor` 用 |
| `shellCall(name, ...args)` | 唯一调用点。宿主不可用时是 no-op，返回 `null`——**页面在浏览器标签页里必须仍然可用** |
| `SHELL` | `?shell=desktop`（查询参数）或 `__CHRONO_SHELL__ === "desktop"`（宿主直接声明）|

## 二之二、页面从哪来，conductor 在哪

这两件事决定接缝能不能用，所以它们也是契约的一部分：

| 场景 | 页面由谁提供 | `/api/*` 怎么到 conductor | `SHELL` 怎么定 |
|---|---|---|---|
| 浏览器标签页 / 手机 | conductor（`CuePanel`） | 同源相对路径，无需 CORS | URL 不带 `shell` → `SHELL=false` |
| pywebview 窗口 | conductor（窗口自己的 `CuePanel`） | 同源相对路径 | `?shell=desktop` |
| Tauri 外壳 | **外壳随包** | `apiURL()` 拼上 `__CHRONO_API_BASE__`；conductor 侧对白名单 origin 开 CORS 并应答 `OPTIONS` 预检 | `__CHRONO_SHELL__` |

由此得出两条页面侧约束：

1. **不允许裸写 `fetch("/api/...")`**，一律 `fetch(apiURL("/api/..."))`。
   `apiURL()` 在浏览器场景下是恒等函数，所以它不引入分支，只把 conductor 的位置
   写在一处。同理，页面不得自己声明 conductor 的地址。
2. CORS 白名单是**封闭列表**（`webui.py` 的 `CORS_ORIGINS`），不是 `*`。
   这个服务在 127.0.0.1 上，驱动一次 cue 是物理动作；把权限发给任意网页
   不是可以接受的代价。新宿主需要把自己的 origin 加进那张表。

## 二之三、版本与握手

页面、外壳、conductor 是**可能来自不同构建**的三份东西：升级不会清理多余文件（实测，
见 `desktop-shell.md` §升级），于是新的窗口真的会遇上旧的 conductor。这不会报错，
只会“有点怪”。所以每一半都要报出自己是谁，每个会**据此行动**的客户端都要先比一次。

| 常量 | 在哪 | 谁声明 |
|---|---|---|
| `CONTRACT_VERSION` | `chrono_handoff/__init__.py` | 唯一真相源 |
| `contract` | `GET /api/health`、IPC `ping` | conductor（两处都取自同一个常量） |
| `contract` | `GET /api/health` 的 `writable` / `why_not_writable` | conductor 能否写状态文件（见 `schema.py`） |
| `PAGE_CONTRACT` | `panel.html` 顶部 | 页面 |
| `CONTRACT` | `src-tauri/src/lib.rs` | Tauri 外壳 |

两个版本号的区别：`__version__` 随每次发布走，`CONTRACT_VERSION` 只在**含义**变了
（某个操作或某个端点的语义变了）才走。所以客户端比的是 contract，不是版本。

不匹配时三边的行为：

| 谁 | 行为 |
|---|---|
| 页面 | 横幅写明两边各是什么 + **拒绝起拍**（`/api/start` 不发）+ 写一行 `page_note` 到外壳日志。没答、没声明，都算“不是我的 conductor”——对不匹配而言，安全方向是不拍 |
| Tauri 外壳 | **不读也不写**设置（几何落盘），日志说清为什么，窗口用默认几何 |
| `chrono doctor` | 把 daemon 的 contract 与本构建对比，不一致就记成一条 problem |
| 冻结侧车自检 | 包的 `doctor --json` 报的 `contract` 必须等于源检出的，不等就是陈旧快照 |

`CONTRACT_VERSION` 变了要同步的地方：`panel.html` 的 `PAGE_CONTRACT`、`lib.rs` 的
`CONTRACT`。漏改会被 `desktop/sidecar/build.py` 的自检拦住（它读这两个常量并与 Python
包比），不会靠人记得。

## 二之四、更新是宿主的责任，页面只能报告和请求

谁能更新取决于宿主：Tauri 外壳装在一份安装包里，它有更新器；pywebview 窗口可能跑在源码检出上，
它没有。所以 seam 里加三个操作（见 §三），页面**不下载任何东西**：

| 操作 | 语义 |
|---|---|
| `update_status()` | 当前状态（`idle`/`checking`/`downloading`/`ready`/`installing`/`error`/`unconfigured`/`unsupported`） |
| `update_check()` | 立即检查（后台跑，立即返回当前状态；页面轮询 `update_status`） |
| `update_install()` | 请宿主安装并重启。**是否允许停**由 conductor 回答，宿主在安装前问它 |

两个规矩：

1. **`unconfigured` / `unsupported` 要说出来，不能什么也不做。** 一个按下去没反应的按钮，和
   “这个构建没有编译进更新公钥”是两回事，而前者会被当成 bug 反复排查。未实现的操作也必须
   **作答**（pywebview 就是这样）。
2. **安装前必须先问 conductor。** 更新器在 cue 中途重启，就把人留在了一个已经死掉的窗口前——
   这比晚一小时更新贵得多。判定规则与两个 HTTP 接口见 `component.md` §四点七，完整设计见
   `docs/update.md`。

## 三、操作清单（16 个）

`args` 全部为可选；除 `page_note` 外返回值不做约定，页面不读。

| 名字 | 语义 | 契约要点 |
|---|---|---|
| `apply_theme(theme)` | 重画**原生**外框：DWM 亚克力/云母、暗色标题栏、窗口底色 | `theme ∈ {dark, light, amber}`。是每窗口状态，不是 CSS；换主题不调它，浅色卡片会套着深色边框 |
| `set_on_top(bool)` | 置顶开关 | **同时持久化**到设置 |
| `set_opacity(float)` | 窗口不透明度 | 只改 CSS 变量 `--win-alpha`，**不得**写死背景色（会压掉主题）。**同时持久化** |
| `minimize()` | 最小化 | |
| `close()` | 关闭窗口 | 关窗前宿主须落盘窗口位置（`remember_position` 为真时） |
| `toggle_maximize()` | 最大化 / 还原 | 最大化状态由宿主自己跟踪，不要每次问 OS |
| `page_note(text)` | 页面写一行字到宿主日志 | 纯日志口：无文件、无 shell、无窗口控制。页面在 release 下没有可见 console，脚本一报错就只剩一个“看起来只是空闲”的面板，这个口是它唯一能说话的地方 |
| `begin_drag()` | 开始拖动 | 宿主记录窗口原点作为基线 |
| `update_drag(dx, dy)` | 拖动 | **`dx/dy` 是相对 pointerdown 的累计位移，不是增量**（增量会只剩最后一小段生效）。单位 CSS px，宿主按 DPI 缩放 |
| `end_drag()` | 结束拖动 | 落盘位置 |
| `begin_resize(edge)` | 开始缩放 | `edge ∈ {n, s, e, w, ne, nw, se, sw}`；宿主记录 `(x,y,w,h)` 基线 |
| `update_resize(dx, dy)` | 缩放 | 同上：**累计位移**。宿主按 edge 决定哪些边动、并对左下/右上补原点偏移 |
| `end_resize()` | 结束缩放 | 落盘尺寸 |
| `update_status()` | 更新器当前状态 | 永不返回空：没检查过、没配密钥、宿主不支持都是**不同**的答案 |
| `update_check()` | 立即检查更新 | 后台跑、立即返回；离线是常态，只报告不弹框 |
| `update_install()` | 安装并重启 | 先问 conductor 能不能停；被拒时返回原因与等待时间，**不安装** |

四个跨宿主都容易踩的坑：

1. **累计位移，不是增量。** `update_*` 传增量的话，每次调用都会覆盖上一次的基线，120 px 的拖动只会长 6 px。这个 bug 真的发生过。
2. **拖动由页面驱动，宿主不要自己轮询光标。** 轮询版本有竞态：快甩（约 0.28 s）会在第一次轮询前就松开按键，于是完全不动、而慢拖正常。
3. **不要开整窗拖动。** `easy_drag=True` 会让每次按下都变成窗口拖动，连设置面板的滑杆也一起拖走。只让标题栏触发 `begin_drag`。
4. **宿主缺某个命令时不能静默返回。** 页面调一个宿主没注册的命令，曾经静默返回 `null`：Rust 侧有 `page_note`、而页面的 `TAURI_PARAMS` 里漏了，于是每次调用都被无声丢掉——“命令名写错”和“调用成功”长得一模一样，多花了两轮排查。现在 `call()` 会报错。

## 四、设置为什么不在 seam 里

`CueWindow` 在注册 bridge 时同时注册 `CuePanel.window_hooks`：

```python
panel.window_hooks.update({
    "always_on_top": lambda value: _set_on_top(win, bool(value)),
    "opacity": lambda _value: _bind_css_window_tweaks(win, panel.settings),
    "width": _resize_to_settings,
    "height": _resize_to_settings,
})
```

于是 `POST /api/settings`、`POST /api/settings/reset` 与旧的 bridge `save_settings`
作用**完全等价**（`CuePanel.update_settings` 会逐个调用 hook）。
设置只有一条路径，seam 里就不该再有第二份——否则两个真相源迟早不一致。

`scale`（页面侧 UI 缩放）与 `frameless`（建窗时就固定）刻意没有 hook。

## 五、新增一个宿主

1. 在 `SHELL_BACKENDS` 里加一行探针，返回一个带上述 16 个方法的对象。
2. 照 §三 逐条实现；尤其 `update_*` 的累计位移语义与 DPI 换算。
3. 决定 §二之二 那一行：宿主是**随包**页面，还是指向 conductor 的 URL。
   前者需要在初始化脚本里注入 `__CHRONO_API_BASE__`（和 `__CHRONO_SHELL__`），
   并把宿主 origin 加进 `webui.CORS_ORIGINS`。
4. `statics/panel.html` 其余部分**一行都不用改**。
5. 跑测试：`e2e_window.py`、`e2e_resize.py`、`e2e_theme_drag.py`（pywebview 宿主）；
   `e2e_desktop_shell.py` + `cargo test`（Tauri 宿主）。

## 六、不属于这里的东西

- 设置、健康、校准、计划、事件、按键 —— 走 HTTP。
- 服务端侧的窗口反应 —— 走 `window_hooks`，不要加 seam 方法。
- 任何「为了让某个框架跑起来」而加的通用性 —— 契约按 §三 这 16 条封闭。
  改这个列表（加、减、改语义）就是**改契约**，同步 `CONTRACT_VERSION`（见 §二之三）。
  加 `update_*` 那次就是这么做的：13 → 16 条，版本 1 → 2。
