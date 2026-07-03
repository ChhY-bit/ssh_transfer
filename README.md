# SSH Transfer - 跨平台数据收发工具

基于 SSH/SFTP 的跨平台文件传输 GUI 工具，支持**文件**和**目录**的上传与下载。

## 功能

- **图形界面 (GUI)** — tkinter 界面，操作直观，支持本地和远程文件浏览
- **终端界面 (TUI)** — 基于 Textual 的终端界面，无需桌面环境，headless 服务器可用
- **SFTP 直连** — 通过 SSH 连接直接传输，无需额外端口或服务端进程
- **连接记忆** — 自动记住最近使用的 IP、端口、用户名（GUI 和 TUI 共享历史）
- **传输进度** — 实时显示进度条、速度和剩余时间
- **跨平台** — Windows / macOS / Linux 均可运行

> 技术架构详见 [DOCUMENT.md](DOCUMENT.md)。

---

## 环境要求

- Python 3.10+
- SSH 服务器端需开启 SFTP 子系统（Linux 默认开启）

---

## 快速开始

### 拉取源码

```bash
git clone https://github.com/ChhY-bit/ssh_transfer.git && cd ssh_transfer
```

### 安装依赖

| 包 | 用途 | 备注 |
|---|---|---|
| `paramiko >= 3.0.0` | SSH / SFTP 连接 | 必装 |
| `textual >= 2.0.0` | TUI 终端界面 | 推荐安装（headless 环境必备） |
| `tkinter` | GUI 图形界面 | Python 自带 |
| `xfonts-utils` | Linux 中文字体自动注册 | Ubuntu 桌面版已预装 |

> 以下两种方式 **任选一种** ：

#### 方式1 - conda 安装

```bash
conda env create -f environment.yml
conda activate ssh_transfer
```

> conda 环境下的 tkinter 可能缺少中文字体，但 GUI 启动时会自动注册捆绑字体，无需额外操作。如遇字体问题，参见 [Linux 中文字体](#linux-中文字体)。

#### 方式2 - pip 安装

（可选）创建虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
```

> Windows 下激活命令为：`venv\Scripts\activate`

安装：

```bash
pip install -r requirements.txt
```


### 开始使用

#### GUI 模式（桌面环境）

启动 GUI（在`ssh_transfer/`目录下）：

```bash
python3 gui.py
```

操作步骤：

1. 填写**服务器 IP**、SSH 端口（默认 22）、**用户名**和**密码**，点击 **连接**
2. 选择传输方向：**推送**（本机 → 服务器）或 **拉取**（服务器 → 本机）
3. 填写本地路径和远程路径（可点击「浏览…」选择）
4. 点击 **开始传输**，等待进度条完成

#### TUI 模式（终端环境 / headless 服务器）⭐

专为**无图形桌面**的远程服务器、WSL、SSH 会话设计，操作与 GUI 一样直观：

```bash
python3 tui.py
```

界面布局与 GUI 对标：

- **左侧面板**：SSH 连接配置（IP、端口、用户名、密码）+ 连接/断开按钮 + 状态提示灯
- **右侧面板**：传输方向选择 + 本地/远程路径输入 + 浏览按钮 + 进度条 + 开始/取消
- **底部面板**：实时日志滚动区域

快捷键：

| 快捷键 | 操作 |
|--------|------|
| `F5` | 开始传输 |
| `F9` | 浏览本地文件 |
| `F10` | 浏览远程文件 |
| `Ctrl+Q` | 退出 |
| `Tab` | 焦点切换 |
| `Enter` | 确认选择 |

> TUI 模式与 GUI 模式**共享连接历史**（`~/.ssh_transfer_history.json`），切换使用无需重新填写。

> 连接成功后会自动记住本次的 IP、端口和用户名，下次启动自动填入。

> TUI 与 GUI 的架构对比、控件映射、线程模型详见 [DOCUMENT.md § TUI vs GUI 双界面架构](DOCUMENT.md#tui-vs-gui-双界面架构)。

---

### 自动更新（推荐）

在服务器上部署后，使用内置的 `update.py` 一键同步最新版：

```bash
python update.py                  # 拉取并应用最新版本（默认 GitHub）
python update.py --check          # 仅检查是否有更新
python update.py --source gitee   # 从 Gitee 拉取（国内服务器推荐）
python update.py --source github  # 显式从 GitHub 拉取
python update.py --deps           # 更新后同步更新 pip 依赖
python update.py --force          # 丢弃本地修改后强制更新
```

**`--source` 说明**：支持 `github`（默认）和 `gitee`。国内服务器连 GitHub 不稳定时，可在 [Gitee](https://gitee.com) 创建一个从 `github.com/ChhY-bit/ssh_transfer` 导入的公开镜像仓库，之后用 `--source gitee` 即可提速。脚本直接通过 URL fetch，**不修改 git remote**，不影响本机 push。

> 更新前会自动检查工作区是否干净，有未提交的修改时会拒绝更新（除非 `--force`）。更新成功后会显示版本变化（如 `v1.3 → v1.4`）。环境检测与更新流程详见 [DOCUMENT.md § update.py](DOCUMENT.md#update.py--自动更新脚本)。

---

## 项目结构

```
ssh_transfer/
├── gui.py               # tkinter GUI 图形界面
├── tui.py               # Textual TUI 终端界面
├── sftp_transfer.py     # SFTP 传输核心
├── ssh_manager.py       # SSH 连接管理
├── history.py           # 连接历史缓存（GUI / TUI 共享）
├── update.py            # 自动更新脚本（GitHub / Gitee）
├── _version.py          # 统一版本号
├── server.py            # HTTP 服务端（高级模式）
├── client.py            # HTTP 客户端（高级模式）
├── assets/fonts/        # 捆绑的 CJK 字体
├── DOCUMENT.md          # 技术文档（API、架构、设计决策）
├── environment.yml      # conda 环境配置
├── requirements.txt     # pip 依赖
└── README.md
```

---

## 注意事项

### 传输路径规则

- 如果目标路径是已存在的目录，文件/文件夹会被放入该目录**内部**
- 如果目标路径不存在，则以该路径为名创建
- 同名文件会被**覆盖**，不会提示确认

### Linux 中文字体

GUI 在 Linux 上启动时会自动检测并注册中文字体。如果界面中文仍显示为方框：

1. 确认 `xfonts-utils` 已安装：
   ```bash
   sudo apt install xfonts-utils
   ```
2. 安装一款中文字体：
   ```bash
   sudo apt install fonts-wqy-microhei
   ```
3. 重启应用

技术细节见 [`DOCUMENT.md`](DOCUMENT.md)。

### SSH 认证

- 支持密码认证和密钥认证
- 首次连接自动接受主机密钥
- 密钥认证需在代码中传入 `key_file` 参数（GUI 当前仅支持密码）

### 传输中断

- 传输过程中可随时点击「取消」中断
- 关闭窗口时有传输进行中会弹出确认对话框

---

## 更多信息

技术文档 [`DOCUMENT.md`](DOCUMENT.md) 涵盖：

- [架构概览](DOCUMENT.md#架构概览) — 整体分层架构图、SFTP 与 HTTP 双通道对比
- [tui.py 模块详解](DOCUMENT.md#tui.py--textual-终端界面) — 消息驱动架构、CSS 布局、键盘快捷键
- [TUI vs GUI 双界面架构](DOCUMENT.md#tui-vs-gui-双界面架构) — 控件对应表、线程模型对比、技术选型
- [update.py 自动更新](DOCUMENT.md#update.py--自动更新脚本) — 更新流程、环境检测策略
- [数据流](DOCUMENT.md#数据流) — GUI、TUI、HTTP 三种模式的数据流详解

---

## 更新日志

### v1.4 (2026-07-03)

- **新增** `update.py --source` 选项，支持从 GitHub / Gitee 切换更新来源（国内服务器可用 Gitee）
- **新增** 版本号显示：GUI 标题栏、TUI 标题栏与启动日志中显示当前版本
- **新增** `update.py` 更新时展示版本变化（如 `v1.3 → v1.4`）
- **新增** GUI / TUI 界面版权信息（作者 C.Yang，主页 http://www.yangchenhan.cn）
- **新增** `_version.py` 统一版本号管理

### v1.3 (2026-07-02)

- **修复** 目录传输进度追踪 Bug：小于 32KB 的文件在进度条中会被遗漏计数
  - 根因：`_Progress` 用启发式 `n_done < _last_n` 检测文件边界，小文件跟大文件时失效
  - 改为显式 `next_file()` 方法，由调用方基于已知文件大小精确累积
- **修复** TUI 文件浏览弹窗因 `dock` + `align: center middle` 导致目录树不可见
- **新增** TUI 文件浏览弹窗导航按钮：根目录、上一级、刷新
- **新增** TUI 文件浏览 `Backspace` 快捷键返回上级目录
- **修复** TUI RadioButton 圆形被方框截断的样式问题
- **新增** GUI 文件浏览顶部工具栏「上一级」按钮
- **修复** TUI 路径输入框 + 浏览按钮行高度过小

### v1.2 (2026-07-02)

- **新增** TUI 终端界面（`tui.py`），基于 Textual 框架
- 无图形桌面的 headless 服务器可直接在终端中使用，操作体验对标 GUI
- 支持键盘快捷键（F5 开始传输、F9/F10 浏览文件、Ctrl+Q 退出）
- 支持远程文件懒加载浏览、传输进度实时显示、取消传输
- **新增** 自动更新脚本（`update.py`），服务器上一键同步 GitHub 最新版
- 提取连接历史模块（`history.py`），GUI 和 TUI 共享 `~/.ssh_transfer_history.json`
- Textual 依赖同步加入 conda 环境配置（`environment.yml`）

### v1.1 (2026-06-21)

- **修复** 目录传输时进度条每传完一个文件就复位的问题
- **修复** 目录传输时速度显示严重偏低（只反映了当前文件而非全局速率）
- 上述问题均源于 `_Progress` 对 paramiko 单文件回调值做赋值而非跨文件累加

### v1.0 (2026-06-18)

- 初始发布
- GUI 图形界面，支持本地/远程文件浏览
- SFTP 文件与目录的上传（push）和下载（pull）
- 连接记忆（自动记住最近使用的 IP、端口、用户名）
- 实时进度条、传输速度和预计剩余时间
- 跨平台支持（Windows / macOS / Linux）
- 捆绑 CJK 字体，Linux 下自动注册中文字体
- CLI 命令行模式

---

## 许可

Copyright © 2026 [C.Yang](http://www.yangchenhan.cn)

捆绑字体 DroidSansFallbackFull.ttf 基于 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)。
