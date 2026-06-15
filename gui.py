#!/usr/bin/env python3
"""
SSH Transfer GUI — cross-platform tkinter interface for SFTP file push/pull.

Launch:
    python gui.py

Requires:
    - tkinter (bundled with Python on Windows/Linux)
    - paramiko (pip install paramiko)
"""

import json
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from sftp_transfer import push as sftp_push, pull as sftp_pull
from sftp_transfer import fmt_size, fmt_speed, fmt_eta
from ssh_manager import SSHManager


class InterruptedError(Exception):
    pass

# ---------------------------------------------------------------------------
# Connection history
# ---------------------------------------------------------------------------

_HISTORY_FILE = Path.home() / '.ssh_transfer_history.json'
_MAX_HISTORY = 10


class _History:
    def __init__(self):
        self.data = {'hosts': [], 'ssh_users': [], 'ssh_ports': []}
        self._load()

    def _load(self):
        try:
            if _HISTORY_FILE.exists():
                self.data = json.loads(_HISTORY_FILE.read_text())
        except Exception:
            pass

    def _save(self):
        try:
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HISTORY_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def record(self, host, ssh_user, ssh_port):
        for key, val in [('hosts', host), ('ssh_users', ssh_user), ('ssh_ports', str(ssh_port))]:
            lst = self.data.setdefault(key, [])
            if val in lst:
                lst.remove(val)
            lst.insert(0, val)
            lst[:] = lst[:_MAX_HISTORY]
        self._save()

    def last(self):
        h = (self.data.get('hosts') or [''])[0]
        u = (self.data.get('ssh_users') or [''])[0]
        p = (self.data.get('ssh_ports') or ['22'])[0]
        return h, u, p

    def values_for(self, key):
        return self.data.get(key, [])


# ---------------------------------------------------------------------------
# Remote file browser dialog
# ---------------------------------------------------------------------------

class _LocalBrowser:
    """Toplevel dialog that browses the local filesystem (like remote browser)."""

    def __init__(self, parent, initial_path=''):
        self._result = None

        self._win = tk.Toplevel(parent)
        self._win.title('浏览本地文件系统')
        self._win.geometry('600x420')
        self._win.transient(parent)
        self._win.grab_set()

        self._cwd = os.path.abspath(os.path.expanduser(initial_path or '.'))

        self._build_ui()
        self._refresh()

    def _build_ui(self):
        top = ttk.Frame(self._win)
        top.pack(fill='x', padx=8, pady=(8, 4))
        ttk.Label(top, text='路径:').pack(side='left')
        self._path_var = tk.StringVar()
        path_entry = ttk.Entry(top, textvariable=self._path_var)
        path_entry.pack(side='left', fill='x', expand=True, padx=4)
        path_entry.bind('<Return>', lambda e: self._navigate(self._path_var.get()))
        ttk.Button(top, text='跳转', command=lambda: self._navigate(self._path_var.get())).pack(side='left')
        ttk.Button(top, text='主目录', command=self._go_home).pack(side='left', padx=2)
        ttk.Button(top, text='根目录', command=lambda: self._navigate('/')).pack(side='left')

        mid = ttk.Frame(self._win)
        mid.pack(fill='both', expand=True, padx=8, pady=4)

        self._listbox = tk.Listbox(mid, font=('monospace', 10))
        self._listbox.pack(side='left', fill='both', expand=True)
        self._listbox.bind('<Double-1>', self._on_doubleclick)

        scroll = ttk.Scrollbar(mid, orient='vertical', command=self._listbox.yview)
        scroll.pack(side='right', fill='y')
        self._listbox.configure(yscrollcommand=scroll.set)

        bot = ttk.Frame(self._win)
        bot.pack(fill='x', padx=8, pady=(4, 8))
        ttk.Button(bot, text='选择此项', command=self._on_select_item).pack(side='right', padx=2)
        ttk.Button(bot, text='选择此目录', command=self._on_select).pack(side='right', padx=2)
        ttk.Button(bot, text='取消', command=self._win.destroy).pack(side='right', padx=2)

        self._status_var = tk.StringVar(value='正在加载…')
        ttk.Label(bot, textvariable=self._status_var).pack(side='left')

    def _refresh(self):
        self._listbox.delete(0, 'end')
        self._path_var.set(self._cwd)
        self._status_var.set('正在加载…')
        self._win.update_idletasks()

        try:
            names = os.listdir(self._cwd)
        except PermissionError:
            self._status_var.set('权限不足'); return
        except Exception as e:
            self._status_var.set(f'读取失败: {e}'); return

        self._listbox.insert('end', '📁 ..')
        self._items = ['..']

        dirs, files = [], []
        for n in names:
            full = os.path.join(self._cwd, n)
            try:
                if os.path.isdir(full):
                    dirs.append(n)
                else:
                    files.append((n, os.path.getsize(full)))
            except OSError:
                pass

        for d in sorted(dirs, key=str.lower):
            self._listbox.insert('end', f'📁 {d}')
            self._items.append(d)
        for f, sz in sorted(files, key=lambda x: x[0].lower()):
            s = _fmt_size_static(sz)
            self._listbox.insert('end', f'📄 {f:40s} {s}')
            self._items.append(f)

        self._status_var.set(f'{len(dirs)} 个目录, {len(files)} 个文件')

    def _navigate(self, path):
        path = os.path.expanduser(path.strip())
        if not path: return
        if not os.path.isabs(path):
            path = os.path.join(self._cwd, path)
        path = os.path.normpath(path)
        if os.path.isdir(path):
            self._cwd = path
            self._refresh()
        elif os.path.isfile(path):
            self._result = path
            self._win.destroy()

    def _on_doubleclick(self, event):
        sel = self._listbox.curselection()
        if not sel: return
        name = self._items[sel[0]]
        if name == '..':
            self._navigate(os.path.dirname(self._cwd))
        else:
            full = os.path.join(self._cwd, name)
            if os.path.isdir(full):
                self._navigate(full)
            else:
                self._result = full
                self._win.destroy()

    def _on_select(self):
        self._result = self._cwd
        self._win.destroy()

    def _on_select_item(self):
        sel = self._listbox.curselection()
        if not sel: return
        name = self._items[sel[0]]
        if name == '..':
            self._on_select()
        else:
            self._result = os.path.join(self._cwd, name)
            self._win.destroy()

    def _go_home(self):
        self._navigate(str(Path.home()))

    def show(self):
        self._win.wait_window()
        return self._result


class _RemoteBrowser:
    def __init__(self, parent, ssh_manager, initial_path=''):
        self._ssh = ssh_manager
        self._result = None

        self._win = tk.Toplevel(parent)
        self._win.title('浏览远程文件系统')
        self._win.geometry('600x420')
        self._win.transient(parent)
        self._win.grab_set()

        try:
            self._cwd = self._ssh.resolve_path(initial_path or '.')
        except Exception:
            self._cwd = initial_path or '/'

        self._build_ui()
        self._refresh()

    def _build_ui(self):
        top = ttk.Frame(self._win)
        top.pack(fill='x', padx=8, pady=(8, 4))

        ttk.Label(top, text='路径:').pack(side='left')
        self._path_var = tk.StringVar()
        path_entry = ttk.Entry(top, textvariable=self._path_var)
        path_entry.pack(side='left', fill='x', expand=True, padx=4)
        path_entry.bind('<Return>', lambda e: self._navigate(self._path_var.get()))
        ttk.Button(top, text='跳转', command=lambda: self._navigate(self._path_var.get())).pack(side='left')
        ttk.Button(top, text='主目录', command=self._go_home).pack(side='left', padx=2)
        ttk.Button(top, text='根目录', command=lambda: self._navigate('/')).pack(side='left')

        mid = ttk.Frame(self._win)
        mid.pack(fill='both', expand=True, padx=8, pady=4)

        self._listbox = tk.Listbox(mid, font=('monospace', 10))
        self._listbox.pack(side='left', fill='both', expand=True)
        self._listbox.bind('<Double-1>', self._on_doubleclick)

        scroll = ttk.Scrollbar(mid, orient='vertical', command=self._listbox.yview)
        scroll.pack(side='right', fill='y')
        self._listbox.configure(yscrollcommand=scroll.set)

        bot = ttk.Frame(self._win)
        bot.pack(fill='x', padx=8, pady=(4, 8))
        ttk.Button(bot, text='选择此项', command=self._on_select_item).pack(side='right', padx=2)
        ttk.Button(bot, text='选择此目录', command=self._on_select).pack(side='right', padx=2)
        ttk.Button(bot, text='取消', command=self._win.destroy).pack(side='right', padx=2)

        self._status_var = tk.StringVar(value='正在加载…')
        ttk.Label(bot, textvariable=self._status_var).pack(side='left')

    def _refresh(self):
        self._listbox.delete(0, 'end')
        self._path_var.set(self._cwd)
        self._status_var.set('正在加载…')
        self._win.update_idletasks()
        try:
            items = self._ssh.list_remote_dir(self._cwd)
        except PermissionError:
            self._status_var.set('权限不足'); return
        except Exception as e:
            self._status_var.set(f'读取失败: {e}'); return

        self._listbox.insert('end', '📁 ..')
        self._items = ['..']
        dirs = sorted([i for i in items if i['is_dir']], key=lambda x: x['name'].lower())
        files = sorted([i for i in items if not i['is_dir']], key=lambda x: x['name'].lower())
        for d in dirs:
            self._listbox.insert('end', f'📁 {d["name"]}')
            self._items.append(d['name'])
        for f in files:
            s = _fmt_size_static(f['size'])
            self._listbox.insert('end', f'📄 {f["name"]:40s} {s}')
            self._items.append(f['name'])
        self._status_var.set(f'{len(dirs)} 个目录, {len(files)} 个文件')

    def _navigate(self, path):
        path = path.strip()
        if not path: return
        if not path.startswith('/'):
            path = self._cwd.rstrip('/') + '/' + path
        try:
            path = self._ssh.resolve_path(path)
        except Exception:
            path = os.path.normpath(path)
        self._cwd = path
        self._refresh()

    def _on_doubleclick(self, event):
        sel = self._listbox.curselection()
        if not sel: return
        name = self._items[sel[0]]
        if name == '..':
            self._navigate(os.path.dirname(self._cwd))
        else:
            full = os.path.join(self._cwd, name)
            try:
                if self._ssh.is_remote_dir(full):
                    self._navigate(full)
                else:
                    # Double-click on file → select it
                    self._result = full
                    self._win.destroy()
            except Exception:
                pass

    def _on_select(self):
        """Select the current directory."""
        self._result = self._cwd
        self._win.destroy()

    def _on_select_item(self):
        """Select the currently highlighted item (file or directory)."""
        sel = self._listbox.curselection()
        if not sel: return
        name = self._items[sel[0]]
        if name == '..':
            self._on_select()  # ".." → select current dir
        else:
            self._result = os.path.join(self._cwd, name)
            self._win.destroy()

    def _go_home(self):
        try:
            home = self._ssh.resolve_path('.')
            self._navigate(home)
        except Exception:
            pass

    def show(self):
        self._win.wait_window()
        return self._result


def _fmt_size_static(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:>8.1f} {u}'
        n /= 1024
    return f'{n:>8.1f} TB'


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class SshTransferApp:
    def __init__(self, root):
        self.root = root
        self.root.title('SSH Transfer — SFTP 跨平台数据收发')
        self.root.geometry('720x580')
        self.root.minsize(600, 500)

        self.ssh: SSHManager = None
        self._transfer_thread = None
        self._cancel_event = threading.Event()
        self._history = _History()
        last_host, last_user, last_ssh = self._history.last()
        self._ssh_port = tk.IntVar(value=int(last_ssh) if last_ssh.isdigit() else 22)
        self._last_host = last_host
        self._last_user = last_user

        self._build_server_section()
        self._build_transfer_section()
        self._build_log_section()

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ===================================================================
    # UI — Server config
    # ===================================================================

    def _build_server_section(self):
        f = ttk.Labelframe(self.root, text='SSH 连接', padding=8)
        f.pack(fill='x', padx=8, pady=(8, 0))

        r0 = ttk.Frame(f)
        r0.pack(fill='x', pady=2)
        ttk.Label(r0, text='服务器 IP:').pack(side='left')
        self._sv_host = ttk.Combobox(r0, width=18, values=self._history.values_for('hosts'))
        self._sv_host.pack(side='left', padx=(4, 16))
        self._sv_host.set(self._last_host)
        self._sv_host.bind('<Button-1>', lambda e: self._refresh_history())

        ttk.Label(r0, text='SSH 端口:').pack(side='left')
        self._sv_ssh_port = ttk.Combobox(r0, width=6, textvariable=self._ssh_port,
                                         values=self._history.values_for('ssh_ports'))
        self._sv_ssh_port.pack(side='left', padx=4)
        self._sv_ssh_port.bind('<Button-1>', lambda e: self._refresh_history())

        r1 = ttk.Frame(f)
        r1.pack(fill='x', pady=2)
        ttk.Label(r1, text='SSH 用户:').pack(side='left')
        self._sv_user = ttk.Combobox(r1, width=14, values=self._history.values_for('ssh_users'))
        self._sv_user.pack(side='left', padx=4)
        self._sv_user.set(self._last_user)
        self._sv_user.bind('<Button-1>', lambda e: self._refresh_history())

        ttk.Label(r1, text='SSH 密码:').pack(side='left')
        self._sv_pass = ttk.Entry(r1, width=18, show='*')
        self._sv_pass.pack(side='left', padx=(4, 16))

        self._btn_connect = ttk.Button(r1, text='连接', command=self._on_connect)
        self._btn_connect.pack(side='right', padx=2)
        self._btn_disconnect = ttk.Button(r1, text='断开', command=self._on_disconnect, state='disabled')
        self._btn_disconnect.pack(side='right', padx=2)

        # Status light
        self._status_canvas = tk.Canvas(r1, width=18, height=18, highlightthickness=0)
        self._status_canvas.pack(side='left', padx=(16, 0))
        self._status_light = self._status_canvas.create_oval(2, 2, 16, 16, fill='#e74c3c', outline='#c0392b')
        self._status_var = tk.StringVar(value='未连接')
        ttk.Label(r1, textvariable=self._status_var, foreground='#888').pack(side='left', padx=4)

    # ===================================================================
    # UI — Transfer
    # ===================================================================

    def _build_transfer_section(self):
        f = ttk.Labelframe(self.root, text='传输', padding=8)
        f.pack(fill='x', padx=8, pady=8)

        r0 = ttk.Frame(f)
        r0.pack(fill='x', pady=2)
        ttk.Label(r0, text='方向:').pack(side='left')
        self._direction = ttk.Combobox(r0, values=['推送 (本机→服务器)', '拉取 (服务器→本机)'],
                                       state='readonly', width=22)
        self._direction.pack(side='left', padx=4)
        self._direction.current(0)

        r1 = ttk.Frame(f)
        r1.pack(fill='x', pady=2)
        ttk.Label(r1, text='本地路径:').pack(side='left')
        self._local_path = ttk.Entry(r1)
        self._local_path.pack(side='left', fill='x', expand=True, padx=4)
        self._btn_browse_local = ttk.Button(r1, text='浏览…', command=self._on_browse_local_menu)
        self._btn_browse_local.pack(side='right')

        r2 = ttk.Frame(f)
        r2.pack(fill='x', pady=2)
        ttk.Label(r2, text='远程路径:').pack(side='left')
        self._remote_path = ttk.Entry(r2)
        self._remote_path.pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(r2, text='浏览…', command=self._on_browse_remote).pack(side='right')

        self._progress_var = tk.DoubleVar(value=0)
        self._progress_bar = ttk.Progressbar(f, variable=self._progress_var, maximum=100)
        self._progress_bar.pack(fill='x', pady=(8, 2))

        self._progress_text = tk.StringVar(value='等待传输…')
        ttk.Label(f, textvariable=self._progress_text).pack(fill='x')

        r3 = ttk.Frame(f)
        r3.pack(fill='x', pady=4)
        self._btn_start = ttk.Button(r3, text='开始传输', command=self._on_start_transfer)
        self._btn_start.pack(side='left', padx=2)
        self._btn_cancel = ttk.Button(r3, text='取消', command=self._on_cancel, state='disabled')
        self._btn_cancel.pack(side='left', padx=2)

    def _build_log_section(self):
        f = ttk.Labelframe(self.root, text='日志', padding=4)
        f.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        self._log_text = tk.Text(f, height=8, state='disabled', wrap='word', font=('monospace', 9))
        self._log_text.pack(fill='both', expand=True)

        scroll = ttk.Scrollbar(self._log_text, command=self._log_text.yview)
        scroll.pack(side='right', fill='y')
        self._log_text.configure(yscrollcommand=scroll.set)

    # ===================================================================
    # Helpers
    # ===================================================================

    def _log(self, msg):
        ts = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}\n'
        self._log_text.configure(state='normal')
        self._log_text.insert('end', line)
        self._log_text.see('end')
        self._log_text.configure(state='disabled')

    def _refresh_history(self):
        self._sv_host.configure(values=self._history.values_for('hosts'))
        self._sv_ssh_port.configure(values=self._history.values_for('ssh_ports'))
        self._sv_user.configure(values=self._history.values_for('ssh_users'))

    def _set_connected(self, connected):
        if connected:
            self._status_canvas.itemconfig(self._status_light, fill='#2ecc71', outline='#27ae60')
            self._status_var.set('已连接')
            self._btn_connect.configure(state='disabled')
            self._btn_disconnect.configure(state='normal')
        else:
            self._status_canvas.itemconfig(self._status_light, fill='#e74c3c', outline='#c0392b')
            self._status_var.set('未连接')
            self._btn_connect.configure(state='normal')
            self._btn_disconnect.configure(state='disabled')
        self._set_ui_state(connecting=False)

    def _set_ui_state(self, connecting=False, transferring=False):
        if connecting:
            state = 'disabled'
        elif transferring:
            self._btn_disconnect.configure(state='disabled')
            self._btn_start.configure(state='disabled')
            self._btn_cancel.configure(state='normal')
            return
        else:
            state = 'normal'
            self._btn_cancel.configure(state='disabled')
            self._btn_start.configure(state='normal')
            # Re-enable disconnect if still connected
            if self.ssh and self.ssh.is_connected:
                self._btn_disconnect.configure(state='normal')

        for w in (self._sv_host, self._sv_user, self._sv_pass, self._btn_connect):
            try:
                w.configure(state=state)
            except Exception:
                pass

    # ===================================================================
    # Local browse
    # ===================================================================

    def _on_browse_local_menu(self):
        dialog = _LocalBrowser(self.root, self._local_path.get().strip())
        result = dialog.show()
        if result:
            self._local_path.delete(0, 'end')
            self._local_path.insert(0, result)

    def _on_browse_remote(self):
        if not self.ssh or not self.ssh.is_connected:
            messagebox.showwarning('未连接', '请先点击"连接"按钮建立 SSH 连接。')
            return
        dialog = _RemoteBrowser(self.root, self.ssh, self._remote_path.get().strip())
        result = dialog.show()
        if result:
            self._remote_path.delete(0, 'end')
            self._remote_path.insert(0, result)

    # ===================================================================
    # Connect / Disconnect
    # ===================================================================

    def _on_connect(self):
        host = self._sv_host.get().strip()
        user = self._sv_user.get().strip()
        password = self._sv_pass.get().strip()
        ssh_port = self._ssh_port.get()

        if not all(c.isascii() and (c.isalnum() or c in '.-') for c in host):
            messagebox.showwarning('IP 格式错误', f'非法字符: "{host}"')
            return
        if not host or not user or not password:
            messagebox.showwarning('参数不完整', '请填写服务器 IP、SSH 用户和密码。')
            return

        self._set_ui_state(connecting=True)
        self._log(f'正在连接 {host}:{ssh_port} (SSH) …')

        def _run():
            ssh = SSHManager(host, ssh_port, user, password)
            try:
                ssh.connect()
                self._history.record(host, user, ssh_port)
                self._log(f'SSH 已连接 → {host}')
                self.ssh = ssh
                self.root.after(0, lambda: self._set_connected(True))
            except Exception as e:
                self._log(f'连接失败: {e}')
                try:
                    ssh.close()
                except Exception:
                    pass
                self.root.after(0, lambda: self._set_connected(False))

        threading.Thread(target=_run, daemon=True).start()

    def _on_disconnect(self):
        self._log('正在断开…')
        self._set_ui_state(connecting=True)

        def _run():
            try:
                if self.ssh:
                    self.ssh.close()
                    self.ssh = None
                self.root.after(0, lambda: self._log('已断开连接。'))
            except Exception as e:
                self.root.after(0, lambda: self._log(f'断开出错: {e}'))
            finally:
                self.root.after(0, lambda: self._set_connected(False))

        threading.Thread(target=_run, daemon=True).start()

    # ===================================================================
    # Transfer
    # ===================================================================

    def _on_start_transfer(self):
        local = self._local_path.get().strip()
        remote = self._remote_path.get().strip()
        direction = self._direction.current()  # 0=push, 1=pull

        if not local or not remote:
            messagebox.showwarning('参数不完整', '请填写本地路径和远程路径。')
            return

        if not self.ssh or not self.ssh.is_connected:
            messagebox.showwarning('未连接', '请先点击"连接"按钮建立 SSH 连接。')
            return

        sftp = self.ssh.sftp
        if sftp is None:
            messagebox.showerror('错误', 'SFTP 未就绪，请重新连接。')
            return

        self._cancel_event.clear()
        self._progress_var.set(0)
        self._set_ui_state(transferring=True)
        verb = '推送' if direction == 0 else '拉取'
        self._log(f'开始{verb}: {local} {"→" if direction == 0 else "←"} {remote}')

        def _progress(done, total, speed, eta, label):
            if self._cancel_event.is_set():
                raise InterruptedError('已取消')
            pct = done / total * 100 if total else 0
            self.root.after(0, lambda: self._update_progress(pct, done, total, speed, eta, label))

        def _run():
            try:
                if direction == 0:
                    sftp_push(sftp, local, remote, on_progress=_progress)
                else:
                    sftp_pull(sftp, remote, local, on_progress=_progress)

                if not self._cancel_event.is_set():
                    self.root.after(0, lambda: self._on_transfer_done(True, ''))
                else:
                    self.root.after(0, lambda: self._on_transfer_done(False, '用户取消'))
            except InterruptedError:
                self.root.after(0, lambda: self._on_transfer_done(False, '用户取消'))
            except Exception as e:
                self.root.after(0, lambda msg=str(e): self._on_transfer_done(False, msg))

        self._transfer_thread = threading.Thread(target=_run, daemon=True)
        self._transfer_thread.start()

    def _on_cancel(self):
        self._log('正在取消…')
        self._cancel_event.set()

    def _update_progress(self, pct, done, total, speed, eta, label):
        self._progress_var.set(pct)
        self._progress_text.set(
            f'{label}  |  {fmt_size(done)} / {fmt_size(total)}  |  {fmt_speed(speed)}  |  剩余 {fmt_eta(eta)}')

    def _on_transfer_done(self, ok, msg):
        self._set_ui_state(transferring=False)
        if ok:
            self._progress_var.set(100)
            self._progress_text.set('传输完成 ✓')
            self._log('传输完成。')
        else:
            self._log(f'传输中断: {msg}')
            messagebox.showwarning('传输未完成', msg)

    def _on_close(self):
        if self._transfer_thread and self._transfer_thread.is_alive():
            if not messagebox.askyesno('确认', '传输正在进行中，确定退出吗？'):
                return
            self._cancel_event.set()
        if self.ssh:
            try:
                self.ssh.close()
            except Exception:
                pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    SshTransferApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
