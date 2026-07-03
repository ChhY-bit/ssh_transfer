#!/usr/bin/env python3
"""
SSH Transfer TUI — Textual-based terminal interface for SFTP push/pull.

Copyright (c) 2026 C.Yang — http://www.yangchenhan.cn

Designed for headless servers and terminal environments where a GUI is not
available.  Mirrors the tkinter GUI experience: connection panel, file
browsers, progress bars, and log output — all inside your terminal.

Launch:
    python tui.py

Requires:
    - textual >= 2.0.0
    - paramiko >= 3.0.0
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    Select,
    Static,
    Tree,
)

from history import _History
from sftp_transfer import fmt_eta as _fmt_eta
from sftp_transfer import fmt_size as _fmt_size
from sftp_transfer import fmt_speed as _fmt_speed
from sftp_transfer import pull as sftp_pull
from sftp_transfer import push as sftp_push
from ssh_manager import SSHManager


# =============================================================================
# Custom messages (cross-thread progress reporting)
# =============================================================================

class ProgressUpdate(Message):
    """Posted from the transfer worker to update the progress bar."""
    def __init__(self, pct: float, done: int, total: int,
                 speed: float, eta: float, label: str) -> None:
        super().__init__()
        self.pct = pct
        self.done = done
        self.total = total
        self.speed = speed
        self.eta = eta
        self.label = label


class TransferComplete(Message):
    """Posted when a transfer worker finishes (success or failure)."""
    def __init__(self, success: bool, msg: str = "") -> None:
        super().__init__()
        self.success = success
        self.msg = msg


class ConnectionResult(Message):
    """Posted after a connect/disconnect attempt completes."""
    def __init__(self, connected: bool, msg: str = "") -> None:
        super().__init__()
        self.connected = connected
        self.msg = msg


# =============================================================================
# Local file browser (modal)
# =============================================================================

class LocalFileBrowser(ModalScreen[str | None]):
    """Modal screen that lets the user browse the local filesystem via
    Textual's built-in ``DirectoryTree`` widget."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "取消"),
        Binding("enter", "confirm", "选择"),
        Binding("backspace", "go_parent", "上级目录"),
    ]

    def __init__(self, start_path: str = "") -> None:
        super().__init__()
        self._current = os.path.abspath(os.path.expanduser(start_path or "~"))

    def compose(self) -> ComposeResult:
        with Vertical(id="browser-inner"):
            yield Label(self._make_title(), id="browser-title")
            try:
                tree = DirectoryTree(Path(self._current))
            except Exception:
                tree = DirectoryTree(Path.home())
            tree.id = "dir-tree"
            yield tree
            with Horizontal(id="browser-buttons"):
                yield Button(" 根目录 ", variant="default", id="btn-root")
                yield Button(" 上一级 ", variant="default", id="btn-parent")
                yield Button(" 刷新 ", variant="default", id="btn-refresh")
                yield Button(" 选择此项 ", variant="primary", id="btn-select")
                yield Button(" 选择目录 ", variant="primary", id="btn-select-dir")
                yield Button(" 取消 ", variant="error", id="btn-cancel")

    def on_mount(self) -> None:
        tree = self.query_one("#dir-tree", DirectoryTree)
        tree.root.expand()
        tree.focus()

    # -- Navigation -----------------------------------------------------------

    def _make_title(self) -> str:
        return f"[bold]本地文件浏览[/bold]  —  [dim]{self._current}[/dim]"

    def _navigate_to(self, new_path: str) -> None:
        """Replace the DirectoryTree in-place to show *new_path*."""
        abs_path = os.path.abspath(os.path.expanduser(new_path))
        self._current = abs_path
        # Update title bar
        self.query_one("#browser-title", Label).update(self._make_title())
        # Remove old tree, mount new one
        old = self.query_one("#dir-tree", DirectoryTree)
        old.remove()
        new_tree = DirectoryTree(Path(abs_path))
        new_tree.id = "dir-tree"
        inner = self.query_one("#browser-inner", Vertical)
        inner.mount(new_tree, before="#browser-buttons")
        new_tree.root.expand()
        new_tree.focus()

    def action_go_parent(self) -> None:
        self._go_parent()

    def _go_parent(self) -> None:
        parent = os.path.dirname(self._current)
        if parent != self._current:
            self._navigate_to(parent)

    def _go_root(self) -> None:
        self._navigate_to("/")

    def _refresh(self) -> None:
        self._navigate_to(self._current)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-select":
            self._select()
        elif bid == "btn-select-dir":
            self._select_dir()
        elif bid == "btn-cancel":
            self.dismiss(None)
        elif bid == "btn-parent":
            self._go_parent()
        elif bid == "btn-root":
            self._go_root()
        elif bid == "btn-refresh":
            self._refresh()

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        self.dismiss(str(event.path))

    def _select(self) -> None:
        tree = self.query_one("#dir-tree", DirectoryTree)
        node = tree.cursor_node
        if node is not None and node.data is not None:
            self.dismiss(str(node.data.path))
        else:
            self.dismiss(None)

    def _select_dir(self) -> None:
        tree = self.query_one("#dir-tree", DirectoryTree)
        node = tree.cursor_node
        if node is not None and node.data is not None:
            path = node.data.path
            self.dismiss(str(path))
        else:
            self.dismiss(None)


# =============================================================================
# Remote file browser (modal)
# =============================================================================

class RemoteFileBrowser(ModalScreen[str | None]):
    """Modal screen for browsing the remote filesystem via SFTP.

    Uses a ``Tree`` widget with lazy-loaded directory nodes so large
    directory trees do not block the UI.
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "取消"),
        Binding("enter", "confirm", "选择"),
        Binding("backspace", "go_parent", "上级目录"),
    ]

    def __init__(self, ssh: SSHManager, start_path: str = "") -> None:
        super().__init__()
        self._ssh = ssh
        self._current = start_path or "."

    def compose(self) -> ComposeResult:
        cwd = self._ssh.resolve_path(self._current) if self._ssh else self._current
        with Vertical(id="browser-inner"):
            yield Label(self._make_title(cwd), id="browser-title")
            tree = Tree(cwd, id="remote-tree")
            tree.root.expand()
            yield tree
            with Horizontal(id="browser-buttons"):
                yield Button(" 根目录 ", variant="default", id="btn-root")
                yield Button(" 上一级 ", variant="default", id="btn-parent")
                yield Button(" 刷新 ", variant="default", id="btn-refresh")
                yield Button(" 选择此项 ", variant="primary", id="btn-select")
                yield Button(" 取消 ", variant="error", id="btn-cancel")

    def on_mount(self) -> None:
        tree = self.query_one("#remote-tree", Tree)
        self._populate_node(tree.root)
        tree.focus()

    # -- Navigation -----------------------------------------------------------

    def _make_title(self, cwd: str) -> str:
        return f"[bold]远程文件浏览[/bold]  —  [dim]{cwd}[/dim]"

    def _navigate_to(self, new_path: str) -> None:
        """Replace the remote Tree in-place to show *new_path*."""
        try:
            resolved = self._ssh.resolve_path(new_path)
        except Exception:
            resolved = new_path
        self._current = resolved
        # Update title
        self.query_one("#browser-title", Label).update(self._make_title(resolved))
        # Remove old tree, mount new one
        old = self.query_one("#remote-tree", Tree)
        old.remove()
        new_tree = Tree(resolved, id="remote-tree")
        new_tree.root.expand()
        inner = self.query_one("#browser-inner", Vertical)
        inner.mount(new_tree, before="#browser-buttons")
        self._populate_node(new_tree.root)
        new_tree.focus()

    def action_go_parent(self) -> None:
        self._go_parent()

    def _go_parent(self) -> None:
        parent = os.path.dirname(self._current)
        if parent and parent != self._current:
            self._navigate_to(parent)

    def _go_root(self) -> None:
        self._navigate_to("/")

    def _refresh(self) -> None:
        self._navigate_to(self._current)

    # -- Tree lazy-loading ---------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Lazy-load children when a remote directory node is expanded."""
        node = event.node
        if node.children:
            first_label = str(node.children[0].label)
            if first_label == "…":
                node.remove_children()
                self._populate_node(node)

    # -- Internal ------------------------------------------------------------

    def _node_full_path(self, node) -> str:
        """Recursively reconstruct the absolute remote path for *node*."""
        tree = self.query_one("#remote-tree", Tree)
        parts = []
        n = node
        while n is not None and n is not tree.root:
            data = n.data
            if isinstance(data, str):
                parts.insert(0, data)
            else:
                label = str(n.label)
                for pfx in ("📁 ", "📄 "):
                    if label.startswith(pfx):
                        label = label[2:]
                parts.insert(0, label)
            n = n.parent
        root_path = str(tree.root.label)
        if not parts:
            return root_path
        return os.path.join(root_path, *parts).replace("\\", "/")

    def _populate_node(self, node) -> None:
        """Fetch remote directory listing and add child nodes."""
        path = self._node_full_path(node)
        if node.is_root:
            try:
                resolved = self._ssh.resolve_path(self._current)
                path = resolved
                node.label = resolved
            except Exception:
                pass

        try:
            items = self._ssh.list_remote_dir(path)
        except Exception:
            node.add_leaf("[red]读取失败[/red]")
            return

        dirs = sorted(
            [i for i in items if i["is_dir"]], key=lambda x: x["name"].lower()
        )
        files = sorted(
            [i for i in items if not i["is_dir"]], key=lambda x: x["name"].lower()
        )

        for d in dirs:
            child = node.add(f"📁 {d['name']}", expand=True, data=d["name"])
            child.add_leaf("…", data=None)  # placeholder for lazy loading

        for f in files:
            size = _fmt_size(f["size"])
            node.add_leaf(f"📄 {f['name']:40s} {size}", data=f["name"])

    # -- Button handlers -----------------------------------------------------

    def action_confirm(self) -> None:
        self._select_item()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-select":
            self._select_item()
        elif bid == "btn-cancel":
            self.dismiss(None)
        elif bid == "btn-parent":
            self._go_parent()
        elif bid == "btn-root":
            self._go_root()
        elif bid == "btn-refresh":
            self._refresh()

    def _select_item(self) -> None:
        tree = self.query_one("#remote-tree", Tree)
        node = tree.cursor_node
        if node is None:
            self.dismiss(None)
            return

        full = self._node_full_path(node)
        self.dismiss(full)


# =============================================================================
# Connection panel (left column)
# =============================================================================

class ConnectionPanel(ScrollableContainer):
    """SSH connection configuration panel."""

    def compose(self) -> ComposeResult:
        yield Label("[bold]SSH 连接[/bold]", id="section-title")
        yield Select([], prompt="📋 历史记录…", id="history-select")
        yield Input(placeholder="服务器 IP", id="host")
        yield Input(placeholder="SSH 端口 (默认 22)", id="port", type="integer")
        yield Input(placeholder="SSH 用户", id="user")
        yield Input(placeholder="SSH 密码", id="password", password=True)
        with Horizontal(id="conn-buttons"):
            yield Button(" 连接 ", variant="primary", id="btn-connect")
            yield Button(" 断开 ", variant="default", id="btn-disconnect")
        yield Static("● 未连接", id="status-label")

    def on_mount(self) -> None:
        hist = _History()
        last_host, last_user, last_port = hist.last()
        self.query_one("#host", Input).value = last_host
        self.query_one("#port", Input).value = last_port
        self.query_one("#user", Input).value = last_user
        self.query_one("#btn-disconnect", Button).disabled = True
        # Populate history dropdown
        self._refresh_history()

    def _refresh_history(self) -> None:
        """Rebuild the history Select options from saved connections."""
        hist = _History()
        hosts = hist.values_for("hosts")
        users = hist.values_for("ssh_users")
        ports = hist.values_for("ssh_ports")
        n = min(len(hosts), len(users), len(ports))
        entries = []
        seen = set()
        for i in range(n):
            label = f"{users[i]}@{hosts[i]}:{ports[i]}"
            if label in seen:
                continue
            seen.add(label)
            entries.append((label, f"{hosts[i]}|{users[i]}|{ports[i]}"))
        select = self.query_one("#history-select", Select)
        if entries:
            select.set_options(entries)
        else:
            select.set_options([("(暂无历史记录)", "")])

    def on_select_changed(self, event: Select.Changed) -> None:
        if not event.value or "|" not in str(event.value):
            return
        host, user, port = str(event.value).split("|")
        self.query_one("#host", Input).value = host
        self.query_one("#port", Input).value = port
        self.query_one("#user", Input).value = user
        self.query_one("#password", Input).focus()

    def set_connected(self, connected: bool) -> None:
        """Update UI to reflect connection state."""
        status = self.query_one("#status-label", Static)
        btn_conn = self.query_one("#btn-connect", Button)
        btn_disc = self.query_one("#btn-disconnect", Button)
        sel = self.query_one("#history-select", Select)

        if connected:
            status.update("[green]● 已连接[/green]")
            btn_conn.disabled = True
            btn_disc.disabled = False
            sel.disabled = True
            for wid in ("#host", "#port", "#user", "#password"):
                self.query_one(wid, Input).disabled = True
        else:
            status.update("[red]● 未连接[/red]")
            btn_conn.disabled = False
            btn_disc.disabled = True
            sel.disabled = False
            for wid in ("#host", "#port", "#user", "#password"):
                self.query_one(wid, Input).disabled = False

    def get_connection_params(self) -> dict:
        """Return the current form values as a dict."""
        return {
            "host": self.query_one("#host", Input).value.strip(),
            "port": int(self.query_one("#port", Input).value.strip() or "22"),
            "user": self.query_one("#user", Input).value.strip(),
            "password": self.query_one("#password", Input).value.strip(),
        }


# =============================================================================
# Transfer panel (right column)
# =============================================================================

class TransferPanel(ScrollableContainer):
    """File transfer controls: direction, paths, progress, start/cancel."""

    def compose(self) -> ComposeResult:
        yield Label("[bold]传输[/bold]", id="section-title")
        with RadioSet(id="direction"):
            yield RadioButton("推送 (本机 → 服务器)", value=True)
            yield RadioButton("拉取 (服务器 → 本机)", value=False)
        yield Label("本地路径", classes="field-label")
        with Horizontal(classes="path-row"):
            yield Input(placeholder="/home/user/myfile", id="local-path")
            yield Button(" 浏览 ", variant="primary", id="btn-browse-local")
        yield Label("远程路径", classes="field-label")
        with Horizontal(classes="path-row"):
            yield Input(placeholder="/data/models", id="remote-path")
            yield Button(" 浏览 ", variant="primary", id="btn-browse-remote")
        yield ProgressBar(total=100, show_eta=False, id="progress-bar")
        yield Label("等待传输…", id="progress-text")
        with Horizontal(id="transfer-buttons"):
            yield Button("开始传输", variant="primary", id="btn-start")
            yield Button("取消", variant="error", id="btn-cancel", disabled=True)


# =============================================================================
# Main TUI application
# =============================================================================

class SshTransferTUI(App):
    """Textual TUI app for SSH/SFTP file transfer."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-horizontal {
        height: 1fr;
    }

    #connection-panel {
        width: 34;
        border: solid $primary;
        padding: 1 1;
        margin: 1 0 0 1;
    }

    #transfer-panel {
        width: 1fr;
        border: solid $primary;
        padding: 1 1;
        margin: 1 1 0 1;
    }

    #log-panel {
        height: 6;
        border: solid $primary;
        margin: 1 1 1 1;
        padding: 0 1;
    }

    #section-title {
        text-style: bold;
        padding-bottom: 1;
        color: $accent;
    }

    .field-label {
        padding-top: 1;
        text-style: bold;
    }

    #history-select {
        margin-bottom: 1;
    }

    #status-label {
        padding-top: 1;
        text-align: center;
    }

    #progress-text {
        text-align: center;
        padding: 1 0;
    }

    #progress-bar {
        width: 100%;
    }

    #conn-buttons, #transfer-buttons {
        padding: 1 0;
        align-horizontal: center;
        height: auto;
    }

    #conn-buttons Button {
        min-width: 8;
    }

    #btn-connect {
        text-style: bold;
    }

    #btn-browse-local, #btn-browse-remote {
        min-width: 10;
    }

    #browser-title {
        padding: 1 2;
        background: $primary;
        color: $text;
        height: auto;
    }

    #browser-buttons {
        padding: 1 2;
        align-horizontal: center;
        height: auto;
    }

    #browser-inner {
        height: 1fr;
    }

    #dir-tree, #remote-tree {
        height: 1fr;
    }

    LocalFileBrowser, RemoteFileBrowser {
        width: 90%;
        height: 85%;
        border: solid $primary;
        background: $surface;
        align: left top;
    }

    /* Ensure path-input + browse-button rows have enough height */
    .path-row {
        height: auto;
        min-height: 3;
    }

    Button {
        margin: 0 1;
    }

    #transfer-panel Input {
        margin-bottom: 0;
    }

    #transfer-panel #progress-bar {
        margin-top: 1;
    }

    #connection-panel Input {
        margin-bottom: 1;
    }

    #local-path, #remote-path {
        width: 1fr;
    }

    RadioSet {
        margin-bottom: 1;
    }

    RadioButton {
        padding: 0 2;
        border: none;
        background: transparent;
        height: auto;
        min-height: 1;
    }

    RadioButton Toggle {
        border: none;
        background: transparent;
        padding: 0 1;
    }

    RadioButton:focus {
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("f5", "start_transfer", "开始传输", show=True),
        Binding("f9", "browse_local", "本地浏览", show=True),
        Binding("f10", "browse_remote", "远程浏览", show=True),
        Binding("ctrl+q", "quit", "退出", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._ssh: SSHManager | None = None
        self._cancel_flag = threading.Event()
        self._transfer_active = False

    # -- compose -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-horizontal"):
            yield ConnectionPanel(id="connection-panel")
            yield TransferPanel(id="transfer-panel")
        yield RichLog(id="log-panel", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "SSH Transfer"
        self.sub_title = "SFTP 跨平台数据收发  |  © 2025 C.Yang"
        log = self.query_one("#log-panel", RichLog)
        log.write("[dim]SSH Transfer TUI 已启动。填写左侧连接信息后点击「连接」。[/dim]")
        log.write("[dim]Copyright © 2026 C.Yang — http://www.yangchenhan.cn[/dim]")

    # -- helpers -------------------------------------------------------------

    @property
    def _conn(self) -> ConnectionPanel:
        return self.query_one("#connection-panel", ConnectionPanel)

    @property
    def _transfer(self) -> TransferPanel:
        return self.query_one("#transfer-panel", TransferPanel)

    @property
    def _log_panel(self) -> RichLog:
        return self.query_one("#log-panel", RichLog)

    def _log_line(self, msg: str, *, style: str = "") -> None:
        ts = time.strftime("%H:%M:%S")
        if style:
            self._log_panel.write(f"[{ts}] [{style}]{msg}[/{style}]")
        else:
            self._log_panel.write(f"[{ts}] {msg}")

    # -- keyboard shortcuts --------------------------------------------------

    def action_browse_local(self) -> None:
        self._on_browse_local()

    def action_browse_remote(self) -> None:
        self._on_browse_remote()

    def action_start_transfer(self) -> None:
        self._on_start_transfer()

    # -- connection ----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses from ConnectionPanel to handlers."""
        bid = event.button.id

        if bid == "btn-connect":
            self._on_connect()
        elif bid == "btn-disconnect":
            self._on_disconnect()
        elif bid == "btn-start":
            self._on_start_transfer()
        elif bid == "btn-cancel":
            self._on_cancel()
        elif bid == "btn-browse-local":
            self._on_browse_local()
        elif bid == "btn-browse-remote":
            self._on_browse_remote()

    def _on_connect(self) -> None:
        params = self._conn.get_connection_params()
        if not params["host"] or not params["user"] or not params["password"]:
            self._log_line("参数不完整 — 请填写服务器 IP、用户和密码",
                           style="yellow")
            return

        self._log_line(f"正在连接 {params['host']}:{params['port']} (SSH) …")
        self._conn.query_one("#btn-connect", Button).disabled = True

        def _do_connect():
            ssh = SSHManager(
                params["host"], params["port"],
                params["user"], params["password"],
            )
            try:
                ssh.connect()
                _History().record(params["host"], params["user"], params["port"])
                self.post_message(ConnectionResult(True, "SSH 已连接"))
                self._ssh = ssh
            except Exception as exc:
                self.post_message(ConnectionResult(False, str(exc)))
                try:
                    ssh.close()
                except Exception:
                    pass

        self.run_worker(_do_connect, thread=True)

    def _on_disconnect(self) -> None:
        self._log_line("正在断开…")
        self._conn.query_one("#btn-disconnect", Button).disabled = True

        def _do_disconnect():
            try:
                if self._ssh:
                    self._ssh.close()
                    self._ssh = None
                self.post_message(ConnectionResult(False, "已断开连接"))
            except Exception as exc:
                self.post_message(ConnectionResult(False, f"断开出错: {exc}"))

        self.run_worker(_do_disconnect, thread=True)

    def on_connection_result(self, event: ConnectionResult) -> None:
        self._conn.set_connected(event.connected and self._ssh is not None)
        if event.connected:
            self._conn._refresh_history()  # update dropdown with new entry
            self._log_line(event.msg, style="green")
        elif "断开" in event.msg:
            self._conn._refresh_history()
            self._log_line(event.msg)
        else:
            self._log_line(f"连接失败: {event.msg}", style="red")

    # -- local / remote browsing ---------------------------------------------

    def _on_browse_local(self) -> None:
        local_val = self._transfer.query_one("#local-path", Input).value.strip()
        self.push_screen(LocalFileBrowser(local_val), self._on_local_selected)

    def _on_local_selected(self, result: str | None) -> None:
        if result:
            self._transfer.query_one("#local-path", Input).value = result

    def _on_browse_remote(self) -> None:
        if not self._ssh or not self._ssh.is_connected:
            self._log_line("请先建立 SSH 连接", style="yellow")
            return
        remote_val = self._transfer.query_one("#remote-path", Input).value.strip()
        self.push_screen(
            RemoteFileBrowser(self._ssh, remote_val), self._on_remote_selected
        )

    def _on_remote_selected(self, result: str | None) -> None:
        if result:
            self._transfer.query_one("#remote-path", Input).value = result

    # -- transfer ------------------------------------------------------------

    def _on_start_transfer(self) -> None:
        if self._transfer_active:
            return
        if not self._ssh or not self._ssh.is_connected:
            self._log_line("请先建立 SSH 连接", style="yellow")
            return

        local = self._transfer.query_one("#local-path", Input).value.strip()
        remote = self._transfer.query_one("#remote-path", Input).value.strip()
        if not local or not remote:
            self._log_line("请填写本地路径和远程路径", style="yellow")
            return

        sftp = self._ssh.sftp
        if sftp is None:
            self._log_line("SFTP 未就绪，请重新连接", style="red")
            return

        radiost = self._transfer.query_one("#direction", RadioSet)
        is_push = radiost.pressed_button.value if radiost.pressed_button else True
        verb = "推送" if is_push else "拉取"
        arrow = "→" if is_push else "←"

        self._cancel_flag.clear()
        self._transfer_active = True
        self._set_transfer_ui(active=True)
        self._log_line(f"开始{verb}: {local} {arrow} {remote}")

        def _progress(done, total, speed, eta, label):
            if self._cancel_flag.is_set():
                raise InterruptedError("用户取消")
            pct = done / total * 100 if total else 0
            self.post_message(
                ProgressUpdate(pct, done, total, speed, eta, label)
            )

        def _run():
            try:
                if is_push:
                    sftp_push(sftp, local, remote, on_progress=_progress)
                else:
                    sftp_pull(sftp, remote, local, on_progress=_progress)
                if not self._cancel_flag.is_set():
                    self.post_message(TransferComplete(True, ""))
                else:
                    self.post_message(TransferComplete(False, "用户取消"))
            except InterruptedError:
                self.post_message(TransferComplete(False, "用户取消"))
            except Exception as exc:
                self.post_message(TransferComplete(False, str(exc)))

        self.run_worker(_run, thread=True)

    def _on_cancel(self) -> None:
        self._log_line("正在取消…", style="yellow")
        self._cancel_flag.set()

    def _set_transfer_ui(self, active: bool) -> None:
        self._transfer.query_one("#btn-start", Button).disabled = active
        self._transfer.query_one("#btn-cancel", Button).disabled = not active

    def on_progress_update(self, event: ProgressUpdate) -> None:
        bar = self._transfer.query_one("#progress-bar", ProgressBar)
        text = self._transfer.query_one("#progress-text", Label)
        bar.update(progress=event.pct)
        text.update(
            f"{event.label}  |  "
            f"{_fmt_size(event.done)} / {_fmt_size(event.total)}  |  "
            f"{_fmt_speed(event.speed)}  |  剩余 {_fmt_eta(event.eta)}"
        )

    def on_transfer_complete(self, event: TransferComplete) -> None:
        self._transfer_active = False
        self._set_transfer_ui(active=False)
        bar = self._transfer.query_one("#progress-bar", ProgressBar)
        text = self._transfer.query_one("#progress-text", Label)
        if event.success:
            bar.update(progress=100)
            text.update("传输完成 ✓")
            self._log_line("传输完成 ✓", style="green")
        else:
            self._log_line(f"传输中断: {event.msg}", style="red")


# =============================================================================
# Shared exception
# =============================================================================

class InterruptedError(Exception):
    """Raised inside the progress callback to abort a transfer."""
    pass


# =============================================================================
# Entry point
# =============================================================================

def main():
    app = SshTransferTUI()
    app.run()


if __name__ == "__main__":
    main()
