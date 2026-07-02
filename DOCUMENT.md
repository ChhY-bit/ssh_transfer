# SSH Transfer — 技术文档

本文档面向开发者，涵盖项目架构、模块 API、关键技术方案及设计决策。用户使用指南请参阅 [`README.md`](README.md)。

---

## 目录

- [架构概览](#架构概览)
- [模块详解](#模块详解)
  - [gui.py — tkinter 图形界面](#gui.py--tkinter-图形界面)
  - [tui.py — Textual 终端界面](#tui.py--textual-终端界面)
  - [sftp_transfer.py — SFTP 传输核心](#sftp_transfer.py--sftp-传输核心)
  - [ssh_manager.py — SSH 连接管理](#ssh_manager.py--ssh-连接管理)
  - [history.py — 连接历史缓存](#history.py--连接历史缓存)
  - [update.py — 自动更新脚本](#update.py--自动更新脚本)
  - [server.py — HTTP 服务端](#server.py--http-服务端)
  - [client.py — HTTP 客户端](#client.py--http-客户端)
- [关键设计决策](#关键设计决策)
  - [Linux tkinter 中文字体方案](#linux-tkinter-中文字体方案)
  - [TUI vs GUI 双界面架构](#tui-vs-gui-双界面架构)
  - [连接历史缓存](#连接历史缓存)
  - [传输进度回调模式](#传输进度回调模式)
  - [SFTP vs HTTP 双模式](#sftp-vs-http-双模式)
- [数据流](#数据流)

---

## 架构概览

```
┌──────────────────────────────────────────────────────────┐
│                         用户                              │
├──────────────────────────────────────────────────────────┤
│  GUI (gui.py)     TUI (tui.py)     CLI (sftp_transfer.py)│
│  tkinter 界面     Textual 终端界面  --host/--user/--pass  │
│        │                │                  │              │
│        ▼                ▼                  ▼              │
│  ┌──────────┐   ┌──────────┐    ┌──────────────────┐    │
│  │SSHManager│   │SSHManager│    │   paramiko.SSH   │    │
│  │· connect │   │· connect │    │   · connect      │    │
│  │· sftp    │   │· sftp    │    │   · open_sftp    │    │
│  │· tunnel  │   │· tunnel  │    └──────────────────┘    │
│  └──────────┘   └──────────┘              │              │
│        │                │                  │              │
│        ▼                ▼                  ▼              │
│  ┌──────────┐   ┌──────────┐    ┌──────────────────┐    │
│  │sftp_     │   │sftp_     │    │    远程服务器     │    │
│  │transfer  │   │transfer  │    │  SFTP 子系统      │    │
│  │push/pull │   │push/pull │    └──────────────────┘    │
│  └──────────┘   └──────────┘                            │
│                                                          │
│  ┌──────────┐   ┌──────────────────┐                    │
│  │client.py │──▶│   server.py      │                    │
│  │HTTP push │   │   HTTP 服务端    │                    │
│  │HTTP pull │   │   (tar 流传输)   │                    │
│  └──────────┘   └──────────────────┘                    │
│        ▲                      │                          │
│        │    SSH 端口转发       │                          │
│        └── ssh_manager ───────┘                          │
│                                                          │
│  ┌──────────────────────────────────────────┐           │
│  │  共享模块:  history.py   update.py        │           │
│  │  连接历史 (GUI/TUI)   自动同步 GitHub     │           │
│  └──────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────┘
```

项目提供两套传输通道：

| 通道 | 传输方式 | 优点 | 缺点 |
|------|---------|------|------|
| **SFTP** | paramiko SFTP 直连 | 无需额外进程，GUI / TUI 默认使用 | 不支持断点续传 |
| **HTTP** | SSH 端口转发 + HTTP tar 流 | 支持断点续传，客户端零依赖 | 需部署 server.py 到远端 |

---

## 模块详解

### gui.py — tkinter 图形界面

**职责**：提供跨平台 GUI，集成 SSH 连接、文件浏览、传输控制。

**关键类**：

| 类 | 说明 |
|---|------|
| `SshTransferApp` | 主窗口，管理全部 UI 和连接生命周期 |
| `_LocalBrowser` | 本地文件浏览对话框（含上一级/主目录/根目录导航按钮） |
| `_RemoteBrowser` | 远程文件浏览对话框（含上一级/主目录/根目录导航按钮） |

**连接历史**：通过 `history._History` 共享模块持久化连接信息，详见[连接历史缓存](#连接历史缓存)。

**线程模型**：SSH 连接、断开、文件传输均在 daemon 线程中执行，通过 `root.after()` 将结果投递回主线程更新 UI。传输支持取消（`threading.Event`）。

**字体方案**：启动时通过 `_setup_tk_fonts()` 检测/注册 CJK 字体，详见[字体方案](#linux-tkinter-中文字体方案)。

---

### tui.py — Textual 终端界面

**职责**：提供无需桌面环境的终端 UI，对标 GUI 的全部功能。基于 [Textual](https://textual.textualize.io/) 框架构建。

**关键类**：

| 类 | 说明 |
|---|------|
| `SshTransferTUI` | Textual App 主类，管理全部 UI 和连接生命周期 |
| `ConnectionPanel` | 左侧 SSH 连接面板（IP/端口/用户/密码输入 + 连接/断开按钮 + 状态灯） |
| `TransferPanel` | 右侧传输面板（方向选择 + 路径输入 + 进度条 + 开始/取消按钮） |
| `LocalFileBrowser` | 本地文件浏览弹窗（封装 Textual 内置 `DirectoryTree`，支持根目录/上级/刷新导航） |
| `RemoteFileBrowser` | 远程文件浏览弹窗（自定义 `Tree` + SFTP 懒加载，支持根目录/上级/刷新导航） |
| `ProgressUpdate` | 跨线程进度消息 |
| `TransferComplete` | 传输完成消息 |
| `ConnectionResult` | 连接结果消息 |

**消息驱动架构**：

```
Worker 线程 (SFTP传输)
  │
  ├─ 进度回调 → self.post_message(ProgressUpdate(...))
  │                └─ SshTransferTUI.on_progress_update() → 更新 ProgressBar
  │
  └─ 完成/异常 → self.post_message(TransferComplete(...))
                   └─ SshTransferTUI.on_transfer_complete() → 重置 UI
```

**CSS 布局**：Textual 使用类似 CSS 的布局语言，面板通过 `id` 选择器停靠：

- `#connection-panel`：左侧固定宽度 34 列
- `#transfer-panel`：右侧弹性宽度（`1fr`）
- `#log-panel`：底部固定高度 8 行（`RichLog` widget）

**键盘快捷键**：

| 快捷键 | 操作 |
|--------|------|
| `F5` | 开始传输 |
| `F9` | 浏览本地文件 |
| `F10` | 浏览远程文件 |
| `Backspace` | 文件浏览中返回上级目录 |
| `Enter` | 文件浏览中确认选择 |
| `Escape` | 关闭弹窗 / 取消 |
| `Ctrl+Q` | 退出 |
| `Tab` | 焦点切换 |

**远程文件懒加载**：`RemoteFileBrowser` 使用占位节点（"…"）标记未加载的目录。用户展开目录时，`on_tree_node_expanded` 事件触发 SFTP `list_remote_dir()` 调用，移除占位节点并插入实际内容。路径通过节点 `data` 属性存储纯文件名，`_node_full_path()` 从根到当前节点递归重建绝对路径。

**文件浏览导航**：两个浏览器均提供「根目录」「上一级」「刷新」按钮 + `Backspace` 快捷键返回上级。导航通过原地替换 Tree/DirectoryTree 控件实现（`remove()` + `mount()`），无需反复 push/pop 屏幕栈。

**布局方案演进**：v1.2 初版使用 `dock: top/bottom` + `align: center middle` 导致 `DirectoryTree` 高度为 0 不可见。v1.3 改为纯 in-flow 三行布局（标题 `auto` → 树 `1fr` → 按钮 `auto`），配合 `align: left top` 覆盖 ModalScreen 默认居中行为。

**依赖**：`textual >= 2.0.0`（含 `rich` 渲染层），已加入 `requirements.txt` 和 `environment.yml`。

---

### sftp_transfer.py — SFTP 传输核心

**职责**：基于 paramiko SFTP 的文件/目录上传和下载。

**公开 API**：

```python
def push(sftp, local_path, remote_path, on_progress=None)
def pull(sftp, remote_path, local_path, on_progress=None)
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `sftp` | `paramiko.SFTPClient` | 已认证的 SFTP 客户端 |
| `local_path` / `remote_path` | `str` | 本地/远程路径，支持 `~` 展开 |
| `on_progress` | `callable` | `(done, total, speed_bps, eta_sec, label)` |

**路径解析逻辑**：

- 若目标路径是已存在的目录 → 源文件/文件夹放入该目录**内部**
- 若目标路径不存在 → 以该路径为名创建

**目录传输**：递归扫描 → 计算总大小 → 逐文件 SFTP 传输并回调进度。

**进度回调**：基于 `paramiko.SFTPClient.put/get` 的 `callback` 参数，刷新频率限制为 10 Hz。

**_Progress 类（v1.3 重构）**：追踪跨文件传输进度。

- **v1.1–v1.2（有 bug）**：使用启发式 `n_done < _last_n` 检测文件边界。当某个文件完全被一个 chunk 读完（<32KB）时，下一个文件的首个 callback 值 `n_done=32768` 会大于该文件的 `_last_n`，边界检测失败，文件字节永久遗漏。
- **v1.3（修复）**：改为显式追踪。调用方在每次 `sftp.put()/get()` 后调用 `prog.next_file()`，用 `_scan_local_dir/_scan_remote_dir` 的已知文件大小精确累积，不再依赖启发式猜测。

```python
# v1.3 目录传输循环
for rel_path, size in files:
    sftp.put(local, remote, callback=prog.update)
    prog.next_file()          # 显式标记文件完成，精确累积
prog.flush()
```

**命令行模式**：

```bash
python sftp_transfer.py push --host 192.168.1.100 --user root \
    --password pass --local /path/to/file --remote /remote/path
python sftp_transfer.py pull --host 192.168.1.100 --user root \
    --password pass --remote /remote/path --local /local/path
```

---

### ssh_manager.py — SSH 连接管理

**职责**：管理 SSH 连接、远程服务端部署、SSH 端口转发。

**公开 API**：

```python
class SSHManager:
    def __init__(self, host, port=22, username='root', password=None, key_file=None)
    def connect()                              # 建立 SSH 连接
    def close()                                # 关闭连接和端口转发
    @property is_connected                     # bool
    @property sftp                             # paramiko.SFTPClient

    # 远程服务端部署
    def upload_server()                        # SFTP 上传 server.py 到 /tmp
    def start_server(port=9090, token=None)    # 启动远程服务端 → (ok, msg)
    def stop_server(port=None)                 # 停止远程服务端
    def is_server_running()                    # 检查服务端进程是否存活

    # SSH 端口转发（HTTP 模式使用）
    def start_port_forward(remote_port, local_port=0)  # → actual_local_port
    def stop_port_forward()

    # 远程辅助方法
    def list_remote_dir(path)                  # 列出远程目录内容
    def resolve_path(path)                     # 规范化远程路径
    def is_remote_dir(path)                    # 判断远程路径是否为目录
    def remote_file_exists(path)               # 检查远程文件是否存在
```

**端口转发实现**：使用 paramiko 的 `transport.open_channel('direct-tcpip', ...)` 创建 SSH 隧道，本地 socket accept → SSH channel → 远程端口。双向数据通过 `select.select` + `sendall` 管道传输，超时 600 秒以支持大文件传输。

**服务端部署流程**：

1. `upload_server()` — SFTP 上传 `server.py` 到 `/tmp/ssh_transfer_server.py`
2. `start_server()` — 通过 SSH 执行 `nohup python3 -u /tmp/ssh_transfer_server.py ...`，轮询日志中 `Listening:` 标记确认启动
3. `stop_server()` — 组合使用 `pkill`、`ss + kill`、`fuser -k` 确保彻底终止

**Python 版本检测**：`_detect_python()` 在远程机器上依次尝试 `python3` → `python`，通过 `--version` 输出确认是 Python 3。

---

### history.py — 连接历史缓存

**职责**：持久化保存最近使用的 SSH 连接参数（IP、端口、用户名），供 GUI 和 TUI 共享。从 `gui.py` 中提取为独立模块（v1.2）。

**公开 API**：

```python
class _History:
    def record(host, ssh_user, ssh_port)   # 记录一次成功连接（去重 + 置顶）
    def last() → (host, user, port)         # 最近一次连接的参数
    def values_for(key) → list[str]         # 某个字段的历史值列表
```

**存储位置**：`~/.ssh_transfer_history.json`

**数据结构**：

```json
{
  "hosts": ["192.168.1.100", "10.0.0.5"],
  "ssh_users": ["root", "admin"],
  "ssh_ports": ["22", "2222"]
}
```

**管理策略**：最多保留 10 条（`_MAX_HISTORY`），新纪录写入时先去重再置顶。GUI 和 TUI 启动时均调用 `last()` 预填输入框，连接成功后调用 `record()` 持久化。

---

### update.py — 自动更新脚本

**职责**：在服务器上从 GitHub 一键同步项目最新版本和依赖，无需手动 git pull + pip install。

**用法**：

```bash
python update.py                # 更新到最新版 (要求工作区干净)
python update.py --check        # 仅检查新提交，不修改文件
python update.py --force        # 丢弃本地修改后强制更新
python update.py --deps         # 更新后自动同步依赖 (auto-detect)
python update.py --deps conda   # 强制使用 conda env update
python update.py --deps pip     # 强制使用 pip install
python update.py --branch dev   # 跟踪其他分支
```

**更新流程**：

```
git fetch origin <branch>
  ├─ 已是最新 → 结束 (exit 0)
  ├─ 有新提交 → 显示提交列表
  │     ├─ 工作区干净 → git pull --ff-only
  │     │     ├─ 成功 → (可选) 更新依赖
  │     │     └─ 失败 → git pull --rebase (回退)
  │     └─ 工作区有修改
  │           ├─ --force → git reset --hard + git clean -fd
  │           └─ 否则 → 报错退出
  └─ 网络错误 → 报错退出
```

**依赖更新 — 环境自动检测**：

```
检查环境变量
  ├─ CONDA_PREFIX 存在 → conda env update -f environment.yml --prune
  │     └─ 失败 → 回退到 pip install -r requirements.txt
  ├─ VIRTUAL_ENV 存在  → pip install -r requirements.txt
  └─ 都不存在 → 警告后尝试 pip install
```

**实现位置**：`update.py`（约 350 行），纯 stdlib，无外部依赖。

---

### server.py — HTTP 服务端

**职责**：在远程机器上运行的 HTTP 文件传输服务。

**端点**：

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/push?path=<dir>` | 接收 tar 流（目录）或原始字节（文件），写入指定路径 |
| `GET` | `/pull?path=<path>` | 以 tar 流返回目录或原始字节返回文件 |
| `HEAD` | `/pull?path=<path>` | 返回 `X-File-Type`、`X-File-Size`、`X-File-Name` 元数据 |
| `POST` | `/shutdown` | 停止服务端进程 |
| `GET` | `/health` | 健康检查（无需认证） |

**认证**：所有数据端点通过 `Authorization: Bearer <token>` 认证。token 通过命令行参数 `--token` 指定。

**断点续传**：

- 上传：客户端发送 `Content-Range: bytes <offset>-<total>/<total>`，服务端跳过已接收字节后追加写入
- 下载：客户端发送 `Range: bytes=<offset>-`，服务端返回 `206 Partial Content`

**实现细节**：

- 基于 `http.server.HTTPServer` + `ThreadingMixIn`（多线程处理并发请求）
- tar 流传输使用 `tarfile.open(mode='w|')` / `tarfile.open(mode='r|')` 流模式，避免磁盘缓冲
- 文件完整性通过 SHA256 校验（`X-File-SHA256` 头）

---

### client.py — HTTP 客户端

**职责**：HTTP 文件传输客户端，可作为库或 CLI 使用。

**公开 API**：

```python
def push(host, port, token, local_path, remote_path, on_progress=None)
def pull(host, port, token, remote_path, local_path, on_progress=None)
def shutdown_server(host, port, token)
def health_check(host, port)
```

**关键特性**：

- **Python 标准库零依赖**：仅使用 `http.client`、`tarfile`、`hashlib` 等标准库，可在任何 Python 3.8+ 环境运行
- **tar 大小预估**：`_compute_tar_size()` 精确计算 tar 归档字节数（GNU 格式），用于设置 `Content-Length` 头
- **断点续传**：上传前 HEAD 查询远端已有字节偏移；下载前检查本地文件大小，设置 `Range` 头

**CLI 模式**：

```bash
python client.py push --host 127.0.0.1:9090 --token mysecret /local/path /remote/path
python client.py pull --host 127.0.0.1:9090 --token mysecret /remote/path /local/path
python client.py shutdown --host 127.0.0.1:9090 --token mysecret
python client.py health --host 127.0.0.1:9090
```

---

## 关键设计决策

### Linux tkinter 中文字体方案

**问题**：Tk 8.6 在 Linux 上字体名称枚举走 X11 核心字体体系（仅约 20 个旧式位图字体），而渲染走 Xft/fontconfig。现代 CJK 字体（如 Noto Sans CJK）虽然已安装，但 Tk 的字体列表中找不到，导致中文显示为方框。

**解决方案**：应用启动时，将捆绑的 TrueType 字体注册到 X11 核心字体路径。

**完整流程**：

```
_grok_setup_fonts(root)
  │
  ├─ 查询 Tk 可见字体列表
  │
  ├─ 优先列表（现代字体）匹配？
  │   ├─ droid sans fallback  (捆绑，自动注册)
  │   ├─ Microsoft YaHei      (Windows)
  │   └─ PingFang SC          (macOS)
  │
  ├─ Linux 且未匹配 → _register_bundled_font()
  │   ├─ 拷贝 assets/fonts/DroidSansFallbackFull.ttf → ~/.ssh_transfer/fonts/
  │   ├─ mkfontscale + mkfontdir  (生成 X11 字体元数据)
  │   └─ xset +fp + xset fp rehash  (注册到 X11)
  │
  ├─ 仍未匹配 → 回退列表
  │   ├─ song ti  (宋体，X11 核心字体，始终可用)
  │   └─ fangsong ti  (仿宋)
  │
  └─ 配置 TkDefaultFont / TkTextFont / TkFixedFont / TkMenuFont / TkHeadingFont
```

**退出清理**：`_on_close()` 中调用 `xset -fp` 移除注册的字体路径。这是会话级操作，X 服务器重启后自动还原。

**捆绑字体**：`DroidSansFallbackFull.ttf`（Apache 2.0，3.9 MB），完整 CJK 字形覆盖。

**依赖**：`mkfontscale` / `mkfontdir` 来自 `xfonts-utils`（Ubuntu 桌面版默认安装）。

**核心代码位置**：`gui.py` 中 `_setup_tk_fonts()`、`_register_bundled_font()`、`_unregister_bundled_font()` 三个函数。

**替代方案对比**：

| 方案 | 优点 | 缺点 |
|------|------|------|
| 捆绑字体 + X11 注册（当前方案） | 用户无感知，开箱即用 | 约 4MB 字体文件，依赖 xfonts-utils |
| 切换到 PyQt5/PySide | fontconfig 原生支持，零字体问题 | 重构成本高，增加 Qt 依赖（~50MB） |
| 要求用户安装字体包 | 零代码改动 | 用户体验差 |

---

### TUI vs GUI 双界面架构

**问题**：v1.0 仅提供 tkinter GUI，无法在 headless 服务器、WSL、远程 SSH 会话等无图形桌面环境中使用。已有的 `sftp_transfer.py` CLI 子命令只能做一次性 push/pull，无法交互式浏览文件。

**解决方案**：v1.2 新增基于 [Textual](https://textual.textualize.io/) 框架的 TUI 模式（`tui.py`），与 GUI 共享同一传输核心：

```
              ┌─────────────────┐
              │  sftp_transfer  │  ← 传输核心 (零改动)
              │  ssh_manager    │  ← SSH 管理 (零改动)
              └────────┬────────┘
                       │
         ┌─────────────┴─────────────┐
         │                           │
    ┌────▼────┐                ┌─────▼─────┐
    │  gui.py │                │  tui.py   │
    │ tkinter │                │  Textual  │
    │ 桌面 GUI │                │  终端 TUI  │
    └─────────┘                └───────────┘
         │                           │
         └───────────┬───────────────┘
                     │
              ┌──────▼──────┐
              │  history.py │  ← 连接历史共享
              └─────────────┘
```

**TUI 与 GUI 的控件对应关系**：

| GUI (tkinter) | TUI (Textual) |
|--------------|----------------|
| `ttk.Entry` | `Input` |
| `ttk.Button` | `Button` |
| `ttk.Progressbar` | `ProgressBar` |
| `ttk.Radiobutton` | `RadioSet` / `RadioButton` |
| `tk.Text` (日志) | `RichLog` |
| `_LocalBrowser` (Toplevel) | `LocalFileBrowser` (ModalScreen + DirectoryTree) |
| `_RemoteBrowser` (Toplevel) | `RemoteFileBrowser` (ModalScreen + Tree 懒加载) |

**线程模型对比**：

| | GUI (tkinter) | TUI (Textual) |
|---|---|---|
| 异步机制 | `threading.Thread` + `root.after()` | `self.run_worker(thread=True)` + `self.post_message()` |
| 取消传输 | `threading.Event` | `threading.Event` (相同) |
| 进度更新 | 回调直接更新 tkinter 变量 | 回调 → ProgressUpdate 消息 → 主线程 widget |

---

### 连接历史缓存

详见 [`history.py`](#history.py--连接历史缓存) 模块文档。

v1.2 中将 `_History` 从 `gui.py` 提取为独立模块 `history.py`，消除了 GUI 和 TUI 之间的代码重复。两个接口共享同一个 `~/.ssh_transfer_history.json` 文件，切换使用时无需重新填写连接信息。

---

### 传输进度回调模式

两个传输模块（SFTP 和 HTTP）使用相同的进度回调签名：

```python
on_progress(done_bytes, total_bytes, speed_bytes_per_sec, eta_seconds, label)
```

**内部实现**：

- 通过 `_Progress` 类封装进度追踪和节流
- v1.3 重构：废弃启发式文件边界检测（`n_done < _last_n`），改为显式 `next_file()` 方法
- 目录传输中每次 `sftp.put()/get()` 后调用 `next_file()`，基于扫描阶段已知的文件大小精确累积
- 节流频率 ~10 Hz（距上次回调 ≥ 0.1 秒或传输完成）
- `label` 为文件名或目录名，用于 UI 状态文本

**SFTP 模式**：利用 paramiko `callback` 参数，paramiko 自动汇报已传输字节数。

**HTTP 模式**：手动追踪 `conn.send(chunk)` / `resp.read(chunk)` 的字节数。

---

### SFTP vs HTTP 双模式

项目保留两套传输通道的历史原因及各自适用场景：

| | SFTP 模式 | HTTP 模式 |
|---|---|---|
| **传输层** | paramiko SFTP | HTTP over SSH 隧道 |
| **服务端** | SSH 自带的 SFTP 子系统 | 需部署 server.py |
| **端口** | 22（SSH 端口） | 动态分配的本地端口 |
| **断点续传** | 不支持 | 支持（Content-Range / Range） |
| **GUI 使用** | ✅ 默认 | ❌ |
| **TUI 使用** | ✅ 默认 | ❌ |
| **CLI 使用** | sftp_transfer.py  | client.py + server.py |

SFTP 模式更简洁（无需部署额外进程），适合 GUI/TUI 交互场景。HTTP 模式更灵活（支持断点续传、客户端零依赖），适合脚本和大文件场景。

---

## 数据流

### SFTP 推送流程（GUI 模式）

```
用户点击「开始传输」
  ↓
SshTransferApp._on_start_transfer()
  ↓
daemon 线程: sftp_push(sftp, local, remote, on_progress=callback)
  ↓
sftp_transfer.push() → _push_file() or _push_dir()
  ↓
paramiko SFTPClient.put(local, remote, callback=progress.update)
  ↓ (每个 chunk)
progress.update(done, total) → _Progress.update() → on_progress(done, total, speed, eta, label)
  ↓ (通过 root.after 回到主线程)
_update_progress() → 更新进度条和状态文本
```

### SFTP 推送流程（TUI 模式）

```
用户点击「开始传输」(或按 F5)
  ↓
SshTransferTUI._on_start_transfer()
  ↓
Worker 线程: self.run_worker(_run, thread=True)
  ├─ sftp_push(sftp, local, remote, on_progress=_progress)
  │     ↓
  │   sftp_transfer.push() → _push_file() or _push_dir()
  │     ↓
  │   paramiko SFTPClient.put(local, remote, callback=prog.update)
  │     ↓ (每个 chunk)
  │   _progress(done, total, speed, eta, label)
  │     ↓
  │   self.post_message(ProgressUpdate(...))  ← 跨线程投递
  │
  └─ 完成后:
       self.post_message(TransferComplete(...))
  ↓ (消息队列回到主线程)
SshTransferTUI.on_progress_update()  → 更新 ProgressBar + Label
SshTransferTUI.on_transfer_complete() → 重置 UI + 日志
```

**与 GUI 模式的关键区别**：

- GUI 使用 `root.after()` 将回调投递到 tkinter 事件循环
- TUI 使用 `self.post_message()` 将消息投递到 Textual 消息队列
- 两者均在实际传输中复用 `sftp_transfer.push/pull` 和 `_Progress`，只是 UI 层的投递机制不同

### SSH 端口转发流程（HTTP 模式）

```
SSHManager.start_port_forward(9090)
  ↓
socket.bind(('127.0.0.1', 0)) → 获取可用端口（如 45678）
  ↓
daemon 线程: accept localhost:45678 连接
  ↓
transport.open_channel('direct-tcpip', ('127.0.0.1', 9090), ...)
  ↓
双线程双向 pipe: local_socket ↔ SSH_channel
  ↓
client.push('127.0.0.1', 45678, ...)
  ↓
HTTP 请求经本地端口 → SSH 隧道 → 远程 server.py:9090
```
