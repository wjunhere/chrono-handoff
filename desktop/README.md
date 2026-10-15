# chrono-handoff 桌面外壳

Tauri 2 窗口，只负责**窗口**；计时引擎在 Python conductor 里。

设计与取舍（含曾试过并被否决的做法、以及本机未能自动覆盖的部分）见
[`../docs/desktop-shell.md`](../docs/desktop-shell.md)；与页面之间的接口见
[`../docs/shell-contract.md`](../docs/shell-contract.md)。

```bash
# 1. 构建随包 conductor（只需一次；PyInstaller 不能交叉编译，每平台各跑一次）
../.venv/Scripts/python.exe sidecar/build.py

# 2. 外壳（会自己拉起 conductor，不必另开终端）
npm install
cd src-tauri && cargo build && ./target/debug/chrono-handoff.exe
```

已有一个 conductor 在跑时外壳直接附加；不想自动拉起用 `--attach-only`。
conductor 的来源优先级、为何要子进程化却不拥有它、以及 stdout 为何不继承，
均见 [`../docs/desktop-shell.md`](../docs/desktop-shell.md) §四。

页面本身不在这里——`frontendDist` 指向 `../src/chrono_handoff/statics`，
即 Python 包自己的静态目录（`panel.html` 与字体），所以只有一份。
