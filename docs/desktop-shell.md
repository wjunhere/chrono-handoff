# 桌面外壳（Tauri 2）

> **一句话**：这个进程只拥有**窗口**，不拥有时钟。计时引擎始终在 Python conductor
> 里；外壳是一个无边框、透明、置顶的 WebView2 窗口，页面由外壳自己随包提供。

## 一、为什么要外壳

pywebview 那条路（`window.py`）能用，但它把"窗口"和"Python 运行时"绑在一起：
分发要用户自己装 Python 与 WebView2 依赖，各平台的原生外观还得各写一遍
ctypes。外壳把这两件事拆开——窗口归框架，计时归 Python。

外壳与 conductor 之间仍然只有两条通道（与 `docs/shell-contract.md` 一致）：

| 通道 | 内容 | 谁提供 |
|---|---|---|
| HTTP `/api/*` | 设置、健康、校准、计划、事件、按键 | Python conductor |
| 12 个 shell 命令 | 最小化/关闭/最大化/拖拽/缩放/置顶/不透明度/原生主题 | Rust 外壳 |

## 二、采用方案 A：页面随包，而不是由 conductor 提供

**曾经试过、并且被否决的做法**：窗口指向 `http://127.0.0.1:8790/?shell=desktop`，
即由 conductor 提供页面。窗口渲染、HTTP 通道、`apply_theme` 全部正常，但
**Tauri 2 的 ACL 拒绝远程 origin 调用应用自身的 `#[tauri::command]`**：

```
INVOKE ERR: set_on_top not allowed. Plugin not found
```

生成的 `src-tauri/gen/schemas/acl-manifests.json` 里只有 `core:*` 命名空间，**没有
本应用命令的命名空间**，所以这不是加一条 permission 能修的。

现在的做法（方案 A）：

| 关注点 | 做法 |
|---|---|
| 页面来源 | `frontendDist` 指向 `src/chrono_handoff/statics`（即 Python 包自己的静态目录），页面与字体的 origin 变成外壳自己的 → 命令免 ACL 授权 |
| API 位置 | 外壳用 `initialization_script` 注入 `__CHRONO_API_BASE__`；页面所有请求经 `apiURL()` 拼前缀（`statics/panel.html`） |
| 跨源 | conductor 侧对**白名单 origin**开启 CORS，并应答 `OPTIONS` 预检（`webui.py` 的 `CORS_ORIGINS`）。白名单是封闭列表而非 `*`：这个服务在 127.0.0.1 上，驱动一次 cue 是物理动作，不该让操作者浏览器里任意页面都能调 |
| 字体 | 与页面同目录，随包提供 → 同源，不需要 CORS |
| `shell=desktop` | URL 查询在打包场景下不可靠，改为 `__CHRONO_SHELL__`；两者任一即可（见 `shell-contract.md`） |

**代价，如实记下**：P0 确立的"页面由数据属主进程提供"不再是字面事实。现在是
"页面是随包静态资源，**数据**仍全部走 HTTP 回到属主进程"。浏览器标签页/手机那条路
不受影响，仍是 conductor 自己提供页面。

## 三、图标

图标是 `src/chrono_handoff/statics/icon.svg` —— **一份源图**。它同时是页面 favicon
（`webui.py` 的 `/icon.svg` 路由 + panel.html 里的 `<link rel="icon">`）和桌面外壳
所有平台图标的源：

```bash
cd desktop && npx tauri icon ../src/chrono_handoff/statics/icon.svg -o src-tauri/icons
```

两个用途共一份文件，是为了让浏览器标签页和任务栏不会各长一个样式。

设计取自面板自己的导轨：一格格拍位、GO 钉在正中、且只有它允许发亮。高度和亮度差
不是装饰——past（左侧：已过，实心偏暗）/ GO（居中：实色琥珀，最高）/ tail（右侧：
延续，逐级变暗变矮）就是实时导轨的含义。

两处**故意偏离**实时导轨，都是为了 16 px 能认：

- 实时导轨的静止格只有轨高的 24%，tail 是 1 px 细描边；放到 16 px 上是亚像素糊。
  图标里高度差被放大、tail 改为填充。
- 第一版把 tail 画成粗描边（照搬实时导轨的“描边=延续”），结果两个描边格在图标尺度下
  **读成了字母 “0” 和 “o”**。改填充后没这个问题了。
- 灰色比实时导轨亮。实时导航的静止格坐在大卡片里、隔着桌面距离看；图标里它们只占 32 px
  图标的 3 px，用导轨本身的透明度会直接消失进底标（试过 0.26/0.34，再试 0.34/0.42，
  都在 32 px 下太淡）。

验证方式是从**已安装的 exe 里抽出图标资源**看的（`a scratch directory` 下的 pwsh 脚本，
`Icon.ExtractAssociatedIcon`），而不是看 SVG——“新图标真的进了二进制”才算是真的。

一个构建期陷阱：改完 `icon.svg` 后 `cargo build` 不一定会重跑资源编译
（实测 0.4 s 直接 Finished），要 `touch build.rs` 才会重新嵌入。

## 四、已验证

### 安装包（端到端，无 Python）

`npx tauri build --bundles nsis,msi`（需代理，见下）产出：

```
target/release/bundle/nsis/chrono-handoff_0.1.0_x64-setup.exe   26.12 MiB
target/release/bundle/msi/chrono-handoff_0.1.0_x64_en-US.msi    26.91 MiB
```

体积 ≈ 侧车 25 MiB + 外壳 4.5 MiB，即侧车确实被打进去了（`7z l` 可见
`chrono-core.exe` 与 `chrono-handoff.exe` 并列）。

实测过程：静默安装到 `%LOCALAPPDATA%\chrono-handoff`（**用户级，无需管理员**）→
把 `PATH` 收窄到只剩 `C:\Windows\system32;C:\Windows`（窄到连 `cmd.exe` 都找不到，
正好证明限制生效）→ 启动 → 面板 200、日志写明用的是**安装目录里那个**
`chrono-core.exe` → arm 一个 cue 跑完全生命周期（`timeout`，11 beats）→ 卸载。

结论：装完不需要 Python，也不需要源码树或 venv。

### 升级（0.1.0 → 0.1.1，实测）

把 `tauri.conf.json` 与 `Cargo.toml` 同时改到 0.1.1 重新打包，然后装在 0.1.0
上面：

| 观察 | 结果 |
|---|---|
| `chrono-handoff.exe` | 4,540,928 → 6,026,240 字节，hash 变了（确实被替换） |
| `uninstall.exe` | 被更新 |
| `chrono-core.exe` | 原样保留（本次没变，应该保留） |
| 旧版遗留的无关文件 | **不会被清理** |
| 升级后的应用 | 面板 200；`shell.json` 报 `version: 0.1.1`；`doctor` 认得出它 |
| 卸载 | 两 exe + uninstaller 全清，只剩下手动放进去的那个无关文件 |

“升级后仍能跑”不是空话：0.1.0 那份二进制**根本不会写 `shell.json`**，新功能真的到位了。

### 自动化

`scripts/e2e_desktop_shell.py`：

| 断言 | 说明 |
|---|---|
| CORS 预检 204 + 放行外壳 origin | JSON POST 需要预检，坏了只会**在外壳里**坏——浏览器测试看不见 |
| 陌生 origin 不发放权限 | 白名单确实封闭 |
| 外壳打开可见窗口，尺寸 = 460×620 逻辑像素 | DPI 换算正确 |
| **打包页面经接缝抵达 Rust**（日志出现 `[shell] apply_theme`） | 最关键的一条：页面看起来一样，坏了看不出来 |
| **拖拽命令：`(101,101) → (221,191)` 即恰好 +120,+90** | 证明**累计位移而非增量**的语义在真实链路（页面 JS → invoke → Rust → OS 窗口）上成立 |
| **缩放命令：`460×620 → 540×680` 即恰好 +80,+60** | 同上，含 DPI 换算 |
| 最小化 / 最大化命令到达 OS 窗口 | |

最后四条来自 **不依赖合成输入的 self-check**（`CHRONO_SHELL_SELFTEST=1`，见下）。

几何算式另有 `cargo test` 的 4 个单元测试（边方向、最小尺寸夹紧、原点补偿、
累计位移）。

本机未能自动覆盖的只剩**页面的指针处理 JS**（手柄拖动、GO 按钮点击）：
这台机器的合成鼠标输入经常一个事件都不投递，测试会显式打印 `[SKIP]` 并说明
对应命令已被上面的 self-check 覆盖，而不是假通过。手工验证过的完整回路：
conductor 上 armed 的 cue 出现在窗口里，时钟偏移 `-0.0 ms`、往返延迟 `1.0 ms`、
`17 scheduled beats`、校准"已学到 4 组"——即跨源 GET/POST 与渲染都通。

### 为什么要一个 self-check

合成鼠标在这台机器上基本不可用（`e2e_resize.py` 就为此带了回退路径）。
没有免输入通道的话，拖拽/缩放/最小化/最大化命令就**完全没有**自动化覆盖——
这正是让坏手柄上线的那种缺口。所以 `CHRONO_SHELL_SELFTEST=1` 时，外壳会在页面
加载后自己通过**页面自己的 `shellCall`** 驱动这几个命令，并打印观测到的结果：

```
[shell] selftest drag ok=true from=(101,101) to=(221,191)
[shell] selftest resize ok=true from=(460x620) to=(540x680)
[shell] selftest minimize ok=true
[shell] selftest maximize ok=true
```

链路上只略过了合成鼠标，其余全真。默认关闭，正常运行里不存在。

### 为何需要代理

`tauri build` 会去 GitHub Releases 下 WiX / NSIS。本机直连可达但会超过 CLI 自带的
下载超时（实测 20 s+，未过），代理下 6 s。所以：

```bash
HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 npx tauri build
```

### 一个只剩本机才看得出的细节

release 构建里 `main.rs` 带 `windows_subsystem = "windows"`，于是**没有 console，
`println!` 无处可去**——启动失败会彻底静默，窗口就是不出现，用户无从下手。
所以 `report_fatal()` 会把原因同时写进 `conductor.log` 并弹一个 MessageBox。
这条在 debug 下看不出来，只在装出来之后才显形。

## 五、怎么跑

外壳会**自己拉起 conductor**，不需要另开一个终端：

```bash
cd desktop && npm install
cd src-tauri && cargo build && ./target/debug/chrono-handoff.exe
```

若已经有一个 conductor 在跑，外壳就直接附加（日志会写明是哪一种）。
不想要自动拉起就用 `--attach-only` 或 `CHRONO_ATTACH_ONLY=1`。

### conductor 从哪来（优先级，从高到低，均会打日志）

| 顺序 | 来源 | 用途 |
|---|---|---|
| 1 | `CHRONO_SIDECAR` | 显式路径（测试、非标准布局） |
| 2 | exe 旁边的 `chrono-core[.exe]` | **发布形态**，即随包的侧车 |
| 3 | `CHRONO_PYTHON` | 源码检出（跑 `-m chrono_handoff.daemon`） |
| — | 都没有 | **只附加**，不猜；没有就报错退出 |

刻意**不**回落到 PATH 上随便哪个 `python`：一个跑在意料之外解释器上的 conductor
比没有更糟——那是第二个、配置不同的时钟，而且窗口看不出来。

### 打包侧车

```bash
.venv/Scripts/python.exe desktop/sidecar/build.py
```

产出 `desktop/src-tauri/binaries/chrono-core-<target-triple>[.exe]`（Tauri 的
`externalBin` 约定，已写进 `tauri.conf.json`），并把它复制到 `target/debug/`
旁以便 `cargo build` 直接验证。脚本最后会跑一次 `chrono-core doctor` 自检——
一个静默丢了 PortAudio 的冻结包，在需要声音之前看起来和正常的一模一样。

**PyInstaller 不能交叉编译**，必须在每个目标平台上各跑一次（CI 亦然）。

侧车是**冻结**的：`statics/` 和 `chrono_handoff/*.py` 都是构建那一刻的快照。
所以改完 Python 只重跑 `tauri build` 是没用的——出包会带着一早上的旧代码。实测
遇到过一次：侧车是 13:58 编的，代码改到 22:15，中间 8 小时的修改全没进包。
**先重跑侧车，再重跑 `tauri build`。**

### 打包 wheel

```bash
uv build --wheel
```

包资源靠 `pyproject.toml` 的 `package-data` 列。它原先只有
`["statics/*.wav"]`——而 `statics/` 下**根本没有 .wav 文件**，于是这条 glob 匹配
不到任何东西，而真正必需的 `panel.html` / 字体 / 图标一个都没列。构建 wheel 并列
内容确认过：**里面任何 statics 都没有**，装上去 `chrono window` 没有页面可显示。
现已按扩展名列全（html / svg / wav / fonts\*.woff2）。

### 看 UI 到底长什么样

```bash
python scripts/_shot_window.py chrono-handoff            # -> logs/shots/chrono-handoff.png
python scripts/_shot_window.py chrono-handoff -o <path>.png
```

为什么留着这个工具：有一个布局 bug，**全部断言都放行**（文档不溢出、`body.scrollWidth`
正确、都“没溢出”），而实际界面把 GO 按钮和五个数字全裁掉了。文字上它是“正常”，
图上它一眼就能看出来。能把“数字看着没问题”换成“这就是它现在的样子”的工具值得留。

三个坑写在了 `scripts/_shot_window.py` 的 docstring 里，都是踩过的：

- **要物理像素，而且是按线程要的。** DPI-unaware 进程拿到的 rect 是虚拟化过的
  （150% 下 690x930 的窗口会说成 460x620），于是 BitBlt 拷的是一个**错位**的区域——
  第一版截出来的是杯子后面的终端。用 `SetThreadDpiAwarenessContext` 只改本线程，
  不会像 `SetProcessDPIAware` 那样把整个进程的坐标系永久改掉（那会静默影响导入它的 suite）。
- **不能用 `PrintWindow`。** 它是截被遮挡窗口的常规手段，但它把 WebView2 内容渲成空白——
  而要看的正是那些内容。改截屏幕区域，所以窗口必须是可见的（置顶小部件没问题）。
- **先 restore。** 最小化的窗口报哨兵矩形（`-21333,-21333` 158x26），不先恢复就只能截到任务栏。

默认输出到 `logs/shots/`（已在 `.gitignore` 里）：截图是本地产物，不应该不小心被提交。
窗口按**进程名**找，不按标题——浏览器标签页会把自己标题成 `chrono-handoff - <浏览器>`，
那是个安静的错答案而不是报错。

### 几件刻意为之的事

- **conductor 被子进程化了，但**不**被子进程拥有**。它以 `CREATE_NEW_CONSOLE`
  脱离：窗口关掉不影响在途 cue（`docs/component.md` 的分离拓扑就靠这一点，
  e2e 里有断言），而且它自己的 console 同时满足键盘捕捉的需要。
- **它的 stdout 不继承外壳的**，而是写进 `<CHRONO_HOME>/.chrono-handoff/conductor.log`。
  一个攥着外壳管道的 conductor 会在管道写满时阻塞、或在窗口关闭时吃到 EPIPE，
  而**会阻塞在 write 上的 conductor 会漏拍**。
- 外壳启动时等面板最多 20 s，等不到就**报错退出而不开窗**——一个数不了数的卡片和
  一个坏掉的卡片在人眼里是一样的。
- 外壳每次被页面调用都往 stdout 打一行 `[shell] <命令>`。这不是调试残留：
  "看起来正常的空壳"是最危险的失败模式，所以让静默失败无法藏身。

## 六、窗口几何：记住、恢复、且不上屏外

外壳在**开窗前**用 HTTP 读 `GET /api/settings`，把 x/y/width/height 交给建窗器；
在 `CloseRequested` 时用 `POST /api/settings` 写回去。走 HTTP 而不是直接写文件，
是因为 panel 进程是 `settings.json` 的唯一写者（见 `docs/shell-contract.md`），
两个写者迟早会让设置界面说谎。

三处不是想当然，是踩出来的：

1. **只在“正常状态”下记几何。** Windows 对最小化的窗口报哨兵坐标
   （实测 `-21333,-21333`）和一个缩小的尺寸。第一版直接把关闭时的实时矩形存下来，
   于是“先最小化再从任务栏关掉”就把用户选的位置毁了——而这是个常见操作。
   现在 `Moved`/`Resized` 时只在非最小/非最大化时更新 `LAST_GOOD`，关闭时优先用它。
2. **存逻辑像素。** 存物理、恢复逻辑，每天开一次就按 DPI 倍数长大一次；
   这台是 1.5x。`cargo test` 里锁了这条。
3. **坐标读到屏幕外就拉回主屏中心。** 拔掉外接屏、改分辨率、改 DPI 之后，
   旧坐标可能完全在桌面外；widget 会**隐身启动**，屏幕上没有任何东西能拖回来。

`GET /api/settings` 和 `POST /api/settings` 的形状**不对称**（GET 嵌在
`settings` 下，POST 收平铺的 patch）。按平铺读会静默拿到默认值——窗口“忘了”自己
在哪，却没有报错。现在读到缺 `settings` 对象会告警。

顺带一条让人少找半天的：日志里偶尔会凭空出现 `[shell] minimize`，而你没点过。
查过了——那是**真的有人在点**。窗口置顶、启动后出现在屏幕中央，坐在机器前面的人
把它最小化拨开是最自然的反应（`isTrusted=true`，落在 `tb-min` 上，时间就在窗口
出现的几秒后）。侧面也证明了上面那套 `LAST_GOOD` 不是多余：“先最小化、再关掉”
就是人的常规动作。

## 七、页面的指针 JS 怎么被覆盖

合成鼠标输入在这台机器上丢 `pointermove`：一次拖动到页面那里只剩
pointerdown+pointerup，没有任何位移。于是手柄拖动、标题栏拖动、GO 按钮全部只能
报 `[SKIP]`——报告里“没测过”和“没问题”长得一模一样。

解法是把**事件本身**在页面里造出来并派发到真实元素上：`panel.html` 暴露
`window.__chronoPageTest`（`click` / `dragGrip` / `dragTitle` / `pressWhenArmed`），
外壳用 `CHRONO_PAGE_SELFTEST=pointer|press` 驱动它。同一些 listener 在跑、同样的
delta、同样发 shellCall/fetch，被拿掉的只有 OS→Chromium 的输入队列。

结果从**窗口和 conductor** 上量，不是听页面自报：拖动后的几何、最小化/最大化
状态、conductor 的 `signal_seen`。一个只会静默什么都不做的钩子会以 FAIL 收场。

两个坑：

- **`target_in_s` 是“从武装到 GO 拍”的时长，不是 `lead_s`。** 第一版用了
  `target_in_s=120` 然后只等 16 s，于是按键早了近两分钟，conductor 按
  `before_min_offset` 规则**正确地**丢弃了它。这一版把 zero 放进了等待窗口内。
- **页面是构建期嵌进二进制的。** 改完 `panel.html` 必须 `cargo build`，
  否则跑的还是上一次编进去的那份——这个让人白找了两轮，因为页面看起来“已经改了”。

## 八、`chrono doctor` 怎么知道外壳在跑

conductor 看不见窗口（外壳对它就只是一个 HTTP 客户端），所以“现在屏幕上有个 Tauri
窗口”这条信息只有外壳自己知道。外壳把它写进 `CHRONO_HOME/shell.json`
（后端、pid、exe、用的是哪条启动规则、conductor pid），doctor 读它。

- 进程已死就报 **stale record**，不会担着一副“正在运行”的口气描述一个不存在的进程。
- 正常退出时外壳自己删掉记录；被强杀的留下的那份由读的一侧处理。

顺带修了 doctor 的退出码：它原先对**渲染后的文本**做 `"NO" in value` 子串匹配，
于是主目录叫 `C:\Users\NOAH` 的人会被诊成一个坏掉的机器。现在问题被显式收集并
逐条列出。

## 九、状态落点：一个目录，一个答案

原先 journal 默认是 `cwd/logs`，其余状态（`daemon.json` / `settings.json` /
`calibration.json`）却在 `CHRONO_HOME`。从开始菜单启动时 cwd 就是安装目录，于是
journal 掉在程序旁边；同一次 cue 从终端跑又落在别处——**“日志在哪”有两个答案**。

现统一由一个 `home.chrono_home()` 决定（四处重复的表达式收敛成一处），
由 `scripts/e2e_state_paths.py` 锁住：拿两个不同的 cwd 跑同一个子进程，
要求四处状态都落在同一个 home 下、且 cwd 一点东西也不多。

## 十、还没做

- **升级的“清理”不是升级的一部分。** 实测 0.1.0 → 0.1.1：主程序被替换、
  `uninstall.exe` 被更新、`chrono-core.exe` 因未变而原样保留（对），但旧版留在安装
  目录里、新包不再提供的**多余文件不会被清掉**。当前包里除了安装器自己装的三样东西
  没别的，所以现在不是问题；哪天安装了额外的静态资源就要记得这条。
