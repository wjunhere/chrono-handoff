# 案例研究：龙芯 2K1000LA 开发板 PMON 引导

> 这是催生 `chrono-handoff` 想法的真实场景。
> 日期：2026-09-20 · 平台：龙芯教育派（Loongson 2K1000LA，LoongArch）· 主机：Windows 11 + Git Bash

---

## 1. 场景

### 1.1 任务

为一个嵌入式 Linux 实验采集引导日志：需要在开发板上用**自定义内核 + 自制根文件系统**交替引导，并逐次比对结果。

### 1.2 硬件通路

```
Windows PC (agent 运行处)
  ├── USB 转串口 CH343 → COM8  ──► 开发板 ttyS0    （控制通道，115200 8N1）
  └── USB 网卡 192.168.50.1   ──► 开发板 eth0 192.168.50.2 （TFTP 数据通道）
```

### 1.3 关键约束

| 约束 | 值 | 后果 |
|------|-----|------|
| **PMON 引导菜单窗口** | `bootdelay = 3` 秒 | 3 秒内不按 `c` 就执行默认启动 |
| **默认启动会成功** | 加载 NVMe 上的 Loongnix（内核 + 27MB initrd） | 错过窗口 → 整个引导机会永久丢失 |
| **LLM 一次 turn** | 5 ~ 30 秒 | 无法"及时反应"于物理事件 |
| **物理 Reset 按钮** | 只能人工按 | agent 无软件通路 |

---

## 2. 问题的实际表现

### 2.1 第一次尝试：纯事件触发（失败）

```
用户按 Reset
  ↓
agent 通过串口发现板子已经在跑 Loongnix      ← 30+ 秒后
  ↓
agent 推理"我该做什么"                       ← 5~30 秒
  ↓
❌ 早就错过了 3 秒窗口
```

**记录**：用户多次按 Reset，我**一次都没能直接抓到 PMON**。

### 2.2 第二次尝试：盲目 `sleep`（失败）

```python
send("echo loongson | sudo -S reboot -f")
time.sleep(15)        # 赌 15 秒后 PMON 菜单还在
send("c")
```

**实测日志**：

```
===== 步骤 2：重启开发板 =====
$ echo ****** | sudo -S reboot -f
[sudo] loongson 的密码：Rebooting.
[   93.548052] reboot: Restarting system
PMON2020 LA2K1000 Initializing. Standby...
...

===== 步骤 3：等待 PMON 菜单 =====
>>> [警告] 未捕获到 PMON 菜单        ← 15 秒等待已经错过窗口
```

**更糟的证据**：`sleep(15)` 结束时，AUTO 启动不仅过了菜单，**连 27MB 的 initrd 都加载完了**：

```
Entry address is 011a0000
Loaded 27857389 bytes      ← 27 MB，说明早已越过菜单窗口
```

> ⚠️ **教训**：`sleep(15)` 不是"等了 15 秒"，而是"**在 15 秒后的那个瞬间**去看窗外" —— 而窗口只开了 3 秒。

### 2.3 第三次尝试：抢占式轮询（成功）✅

```python
send("echo loongson | sudo -S reboot -f")
time.sleep(1.0)                    # 只等 1 秒就进入抢占
acc = b''
last_c = 0.0
while time.time() - t0 < 75:       # 75 秒窗口
    d = s.read(8192)
    if d:
        acc += d
        if b'PMON>' in acc or b'command-line' in acc:
            break                  # 抢占成功，立即停
    if time.time() - last_c > 0.3: # 每 0.3 秒发一次 c
        s.write(b'c')
        last_c = time.time()
```

**为什么一定成功**：窗口 3 秒 ÷ 间隔 0.3 秒 = **窗口内约 10 次机会**。

**实测结果**：

```
===== 步骤 3：抢占 PMON 菜单（持续发 c）=====
AUTO
Press <Enter> to execute loading image:(wd0,0)/boot/vmlinuz
Press any other key to abort.
02                              ← 倒计时被打断
PMON>
PMON>
PMON>
>>> [PMON 命令行就绪]            ← ✅ 成功
```

### 2.4 踩到的副作用：多余的 `c` 会污染命令

抢占成功后，队列里残留的 `c` 会堆在输入行上：

```
PMON> cccccccdevls               ← 下一条命令被污染
cccccdevls: Command not found
```

**对策（两步）**：

1. **一检测到菜单标志就立刻停止发送**（减少残留）
2. **发一个 `\r\n`**，把残留的 `c` 当作命令提交掉（得到一次无害的 `Command not found`），再发正常命令

---

## 3. 人的那一环：完全没有被处理 ❌

这是本项目的**起点**。

在 2.3 的方案里，我用"抢占 + 容错"绕过了人类的时间精度问题 —— 但代价是：

| 代价 | 说明 |
|------|------|
| **必须由 agent 控制重启时机** | 只能走"登录 Loongnix → `sudo reboot`"的软件路径 |
| **物理 Reset 仍然不可用** | 用户按 Reset 的时刻无法与 agent 对齐 |
| **75 秒固定窗口很浪费** | 如果人机对齐，只需 3~5 秒 |
| **无法知道人是否按了** | 只能靠"有没有出现 PMON 输出"间接推断 |

**也就是说：人类这个执行器，在这个方案里是完全"不可编程"的 —— 只能用蛮力等待去覆盖他的不确定性。**

**这正是 `chrono-handoff` 要解决的。**

---

## 4. 复盘：如果把 `chrono-handoff` 用在这里

### 4.1 理想流程

```
t=0s     agent 提交 timeline:
           cue: 倒计时 5 秒 + 蜂鸣       instruction: "按一下开发板 RESET"
           human_action_at: T+5s
           expect_signal:  串口出现 "PMON2020"  (timeout 60s)
           on_window:      T+8s..T+11s, 每 300ms 发 'c'
           verify:         串口出现 "PMON>"  (timeout 20s)
         ↓ (agent 立刻结束当前 turn，不阻塞)

t=0s     Conductor 开始渲染倒计时 + 蜂鸣
t=5s     蜂鸣 → 用户按 Reset                          ← 人类精度 ±50ms
t=7.8s   串口读到 "PMON2020 ... command-line"        ← 闭环确认人的动作生效
t=8.0s   窗口打开，立刻发 'c'（不用发 250 次）
t=8.5s   串口读到 "PMON>" → verify 通过
         ↓
t=9s     agent 下一轮 turn，从事件日志读到：
           human_latency_ms = 2842
           status = satisfied
```

### 4.2 改善量化

| 维度 | 现状（抢占轮询） | 用 chrono-handoff |
|------|----------------|------------------|
| 从"决定"到"进 PMON" | ~75 秒（最坏情况） | **~9 秒** |
| 能否用物理 Reset | ❌ 不能 | ✅ 能 |
| 冗余串口写入 | ~250 次 `c` | **1 次** |
| 知道人是否按了 | ❌ 只能推断 | ✅ `signal_matched` 事件 |
| 知道人晚了多少 | ❌ 不知道 | ✅ `human_latency_ms` |
| 下次能否更快 | ❌ 不能 | ✅ 自适应提前 2.8 秒 |

### 4.3 这个案例暴露的更深问题

**"抢占 + 容错"是绕过问题，不是解决问题。**

抢占把"精确时刻"替换成"窗口内暴力扫描"，代价是：

- 时间上浪费（75 秒 vs 9 秒）
- 副作用（多余字符污染）
- 无法多步协调（如果需要在 3 个不同时刻做 3 件不同的事，"扫射"就失效了）

**`chrono-handoff` 的目标是把"暴力"换回"精确"。**

---

## 5. 其他实测踩坑（同一会话）

这些坑虽不是时间协调问题，但构成了本案例的完整背景。

### 5.1 `initrd` 失败 → 内核 panic → 板子挂死

**现象**：PMON 的 `load` 成功，但 `initrd` 失败：

```
PMON> initrd tftp://192.168.50.1/ramdisk_loong.cpio.gz
tftp://192.168.50.1/ramdisk_loong.cpio.gz: Undefined error: 0
```

随后内核 panic：

```
[    3.917602] Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)
```

**挂死机制**（值得记录）：

```
initrd 失败 → 内核无根文件系统 → VFS: Unable to mount root fs → panic()
                                                              ↓
                                          panic_timeout = 0（缺省）
                                                              ↓
                                          local_irq_disable() + for(;;)
                                                              ↓
                    ┌─────────────────────────────────────────┴──────────────┐
                    │ 串口无响应（中断已关 + 无进程能解释输入）                    │
                    │ 网络不通（协议栈已死，但 PHY 链路仍亮 —— 硬件自协商）        │
                    │ → 只能物理 Reset                                       │
                    └───────────────────────────────────────────────────────┘
```

**根因**：TFTP 服务端首次传输握手异常慢（5.5MB 用了 18.65 秒，正常应 <1 秒），PMON 超时后先行认为 `load` 完成，紧接着的 `initrd` 请求与服务端残留的会话状态错位。

**对策**：
1. `load` 后**等足 22 秒**（实测首次传输 17~19 秒），等服务端会话彻底收尾
2. `initrd` **自动重试**（最多 4 次）
3. **防御性设计**：`initrd` 重试仍失败则**不执行 `g`**，让板子安全停在 PMON（把"挂死"变成"可重试状态"）← ★ 这是幂等性设计的实例

### 5.2 Windows 下 TFTP 服务被 UDP 错误打死

**现象**：服务运行一段时间后退出（exit code 1），日志无 traceback。

**根因**：Windows 的 UDP socket 在收到 ICMP port-unreachable 时，会在下一次 `recvfrom` 报 `WSAECONNRESET`。

**对策**：自愈监督进程：

```python
while True:
    subprocess.run([sys.executable, '-u', 'tftp_server.py'])
    time.sleep(1)
```

> **这条与本项目相关**：**长时等待/长时服务的韧性设计**是同一类问题 —— 世界会干扰你，必须假设进程会死。

### 5.3 后台任务管理器会杀掉长跑的 server

**现象**：TFTP 服务作为"后台任务"启动后，被会话切换/工具重载连带终止。

**对策**：改用 `nohup` 脱离进程组，并把 PID 写入文件以便管理。

> **这条也与本项目相关**：Conductor 作为长时间运行的独立进程，**必须脱离 LLM turn 的生命周期** —— 否则就退化成"阻塞在 tool call 上"。

### 5.4 `env` 命令的分页器会吃掉后续命令

**现象**：PMON 的 `env` 输出过长时分页显示 `more...`，分页器消费了随后输入的字符：

```
PMON> 0 192.168.50.2
0: Command not found. Try 'h' for help!        ← 本该是 "ifconfig syn0 192.168.50.2"
```

**对策**：避免使用带分页的命令；或用无分页的短命令（`devls`）。

---

## 6. 成功记录

最终双向引导全部成功。

### 6.1 出厂镜像（对照组）

```
PMON> load tftp://192.168.50.1/vmlinuz_fy
Entry address is 012a0000
PMON> initrd tftp://192.168.50.1/ramdisk_loong.cpio.gz
Loaded 1976598 bytes
PMON> g console=ttyS0,115200 rdinit=/sbin/init

[    0.000000] Linux version 4.19.190 (fang@fang-Desktop) ... #46 SMP Fri Aug 30 16:22:51 CST 2024
```

### 6.2 自制镜像（实验目标）★

```
PMON> load tftp://192.168.50.1/vmlinuz_wjun
Entry address is 012c0000
PMON> initrd tftp://192.168.50.1/initrd_wjun.cpio.gz
Loaded 1924297 bytes
PMON> g console=ttyS0,115200 rdinit=/sbin/init

[    0.000000] Linux version 4.19.190 (student@C413-22) ... #1 SMP Tue Sep 8 14:19:35 CST 2026
[    3.780031] Run /sbin/init as init process

/ # uname -a
Linux Loongson 4.19.190 #1 SMP Tue Sep 8 14:19:35 CST 2026 loongarch64 GNU/Linux
/ # cat /etc/motd
  Welcome to
  ==================================
         LoongARCH WORLD
  ported by wjun (241180137)
  ==================================
/ # ping -c 3 192.168.50.1
3 packets transmitted, 3 packets received, 0% packet loss
```

**全流程耗时 61 秒**（含内核启动），其中 TFTP 传输 18 秒、稳定等待 22 秒。

---

## 7. 本案例给出的设计需求

从这次的实机经验，直接推导出 `chrono-handoff` 的硬性需求：

| # | 需求 | 来源 |
|---|------|------|
| R1 | 时间敏感动作必须**提前提交** | `sleep(15)` 的失败证明事后决策来不及 |
| R2 | 执行必须是**独立进程** | 后台任务被杀的教训 |
| R3 | 人类需要**倒计时 + 声音**锚点 | 物理 Reset 始终无法对齐 |
| R4 | 动作给**窗口**不给单点 | 0.3s 抢占的 10 次机会才保证命中 |
| R5 | 必须**闭环验证** | "发出 `c`" ≠ "进入 PMON"，要靠串口回读确认 |
| R6 | 失败要**可恢复**，不要变灾难 | `initrd` 失败 → 不启动内核 |
| R7 | 必须记录**人类延迟**用于校准 | 观察到人类动作有系统性偏晚 |
| R8 | 服务要能**自愈** | Windows UDP / 后台任务被杀 |
| R9 | 窗口过窄时要能**降级到硬件** | 3 秒能靠人；50ms 就必须上 GPIO |

---

## 8. 一句话

> **这个案例的价值不在于"我最终成功引导了开发板"，而在于它精确地暴露了：当一个执行器是人时，agent 现有的"请求—等待—反应"范式是彻底失效的。**
