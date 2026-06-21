# SSH Transfer - 跨平台数据收发工具

基于 SSH/SFTP 的跨平台文件传输 GUI 工具，支持**文件**和**目录**的上传与下载。

## 功能

- **图形界面** — 操作直观，支持本地和远程文件浏览
- **SFTP 直连** — 通过 SSH 连接直接传输，无需额外端口或服务端进程
- **连接记忆** — 自动记住最近使用的 IP、端口、用户名
- **传输进度** — 实时显示进度条、速度和剩余时间
- **跨平台** — Windows / macOS / Linux 均可运行

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
| `tkinter` | GUI 界面 | Python 自带 |
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

启动 GUI（在`ssh_transfer/`目录下）：

```bash
python3 gui.py
```

操作步骤：

1. 填写**服务器 IP**、SSH 端口（默认 22）、**用户名**和**密码**，点击 **连接**
2. 选择传输方向：**推送**（本机 → 服务器）或 **拉取**（服务器 → 本机）
3. 填写本地路径和远程路径（可点击「浏览…」选择）
4. 点击 **开始传输**，等待进度条完成

> 连接成功后会自动记住本次的 IP、端口和用户名，下次启动自动填入。

---

## 项目结构

```
ssh_transfer/
├── gui.py               # tkinter GUI 图形界面
├── sftp_transfer.py     # SFTP 传输核心
├── ssh_manager.py       # SSH 连接管理
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

- 技术架构、API 参考、命令行模式详见 [`DOCUMENT.md`](DOCUMENT.md)

---

## 更新日志

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

捆绑字体 DroidSansFallbackFull.ttf 基于 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)。
