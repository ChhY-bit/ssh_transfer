#!/usr/bin/env python3
"""
SSH Transfer — SFTP-based file transfer (push / pull).

Replaces the HTTP+tunnel approach.  Uses paramiko SFTP directly over
the existing SSH connection.  No extra server process, no port forwarding.

Usage:
    from sftp_transfer import push, pull
    push(sftp, '/local/path', '/remote/path', on_progress=callback)
    pull(sftp, '/remote/path', '/local/path', on_progress=callback)
"""

import os
import time


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def push(sftp, local_path, remote_path, on_progress=None):
    """Upload `local_path` (file or directory) to `remote_path` via SFTP.

    If remote_path is an existing directory, the file/folder is placed inside it.
    on_progress(done_bytes, total_bytes, speed_bps, eta_sec, label) or None
    """
    local_path = os.path.expanduser(local_path)
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)
    # If remote is an existing directory, compose path
    if _is_remote_dir(sftp, remote_path):
        remote_path = remote_path.rstrip('/') + '/' + os.path.basename(local_path)
    if os.path.isdir(local_path):
        _push_dir(sftp, local_path, remote_path, on_progress)
    else:
        _push_file(sftp, local_path, remote_path, on_progress)


def pull(sftp, remote_path, local_path, on_progress=None):
    """Download `remote_path` (file or directory) to `local_path` via SFTP.

    If local_path is an existing directory, the file/folder is placed inside it.
    on_progress(done_bytes, total_bytes, speed_bps, eta_sec, label) or None
    """
    local_path = os.path.expanduser(local_path)
    # If local is an existing directory, compose path
    if os.path.isdir(local_path):
        local_path = os.path.join(local_path, os.path.basename(remote_path.rstrip('/')))
    if _is_remote_dir(sftp, remote_path):
        _pull_dir(sftp, remote_path, local_path, on_progress)
    else:
        _pull_file(sftp, remote_path, local_path, on_progress)


# ---------------------------------------------------------------------------
# Single-file push / pull
# ---------------------------------------------------------------------------

def _push_file(sftp, local_path, remote_path, on_progress):
    label = os.path.basename(local_path)
    total = os.path.getsize(local_path)
    prog = _Progress(total, on_progress, label)

    # Ensure parent directory exists
    _ensure_remote_dir(sftp, os.path.dirname(remote_path))

    try:
        sftp.put(local_path, remote_path, callback=prog.update)
    except IOError as e:
        raise IOError(f'上传失败: {e}  (→ {remote_path})') from e
    prog.flush()


def _pull_file(sftp, remote_path, local_path, on_progress):
    label = os.path.basename(remote_path)
    try:
        total = sftp.stat(remote_path).st_size
    except IOError as e:
        raise IOError(f'无法访问远程文件: {remote_path} ({e})') from e
    prog = _Progress(total, on_progress, label)

    os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
    try:
        sftp.get(remote_path, local_path, callback=prog.update)
    except IOError as e:
        raise IOError(f'下载失败: {e}  (← {remote_path})') from e
    prog.flush()


# ---------------------------------------------------------------------------
# Directory push / pull
# ---------------------------------------------------------------------------

def _push_dir(sftp, local_dir, remote_dir, on_progress):
    label = os.path.basename(local_dir)
    # Scan to get total size
    files = _scan_local_dir(local_dir)
    total = sum(sz for _, sz in files)
    prog = _Progress(total, on_progress, label)

    for rel_path, size in files:
        local = os.path.join(local_dir, rel_path)
        remote = os.path.join(remote_dir, rel_path).replace('\\', '/')
        _ensure_remote_dir(sftp, os.path.dirname(remote))
        sftp.put(local, remote, callback=prog.update)
        prog.next_file()          # account for completed file exactly

    prog.flush()


def _pull_dir(sftp, remote_dir, local_dir, on_progress):
    label = os.path.basename(remote_dir)
    # Scan remote directory
    files = _scan_remote_dir(sftp, remote_dir)
    total = sum(sz for _, sz in files)
    prog = _Progress(total, on_progress, label)

    for rel_path, size in files:
        remote = os.path.join(remote_dir, rel_path).replace('\\', '/')
        local = os.path.join(local_dir, rel_path)
        os.makedirs(os.path.dirname(local) or '.', exist_ok=True)
        sftp.get(remote, local, callback=prog.update)
        prog.next_file()          # account for completed file exactly

    prog.flush()


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------

def _scan_local_dir(path):
    """Walk `path` and return list of (relative_path, size_in_bytes)."""
    result = []
    for root, dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, path)
            result.append((rel, os.path.getsize(full)))
    return result


def _scan_remote_dir(sftp, path):
    """Walk a remote directory and return list of (relative_path, size_in_bytes)."""
    result = []
    _remote_walk(sftp, path, '', result)
    return result


def _remote_walk(sftp, base, rel_prefix, result):
    try:
        attrs = sftp.listdir_attr(base)
    except IOError:
        return
    for attr in attrs:
        name = attr.filename
        full = f'{base}/{name}'.replace('//', '/')
        rel = f'{rel_prefix}/{name}'.lstrip('/') if rel_prefix else name
        if attr.st_mode & 0o40000:  # directory
            _remote_walk(sftp, full, rel, result)
        else:
            result.append((rel, attr.st_size))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_remote_dir(sftp, path):
    try:
        return sftp.stat(path).st_mode & 0o40000 != 0
    except (FileNotFoundError, IOError):
        return False


def _ensure_remote_dir(sftp, path):
    """Create remote directory (and parents) if they don't exist."""
    if not path or path in ('/', '.'):
        return
    try:
        sftp.stat(path)
    except (FileNotFoundError, IOError):
        _ensure_remote_dir(sftp, os.path.dirname(path))
        try:
            sftp.mkdir(path)
        except IOError:
            pass  # might already exist (race)


# ---------------------------------------------------------------------------
# Progress tracker
# ---------------------------------------------------------------------------

class _Progress:
    """Tracks transfer progress across one or more files.

    For single-file transfers the caller just feeds ``update()`` and ends
    with ``flush()`` — no extra book-keeping needed.

    For *directory* transfers the caller MUST call ``next_file()`` after
    every ``sftp.put()`` / ``sftp.get()`` so that completed-file bytes are
    accounted for *exactly*, regardless of chunk sizes.
    """

    def __init__(self, total, callback, label=''):
        self.total = total
        self.cb = callback
        self.label = label
        self._start = time.time()
        self._done = 0            # total bytes transferred so far
        self._file_offset = 0     # bytes from fully-completed files
        self._current_n = 0       # n_done value for the *current* file
        self._last_ts = self._start

    def update(self, n_done, _=None):
        """Called by paramiko with (bytes_done_so_far_for_this_file, file_size)."""
        self._current_n = n_done
        self._done = self._file_offset + n_done
        self._report()

    def next_file(self):
        """Mark the current file as complete and advance to the next one."""
        self._file_offset += self._current_n
        self._current_n = 0
        self._done = self._file_offset

    def flush(self):
        """Finalise: account for the last file and force a progress update."""
        self._file_offset += self._current_n
        self._current_n = 0
        self._done = self._file_offset
        if self.cb:
            self.cb(self._done, self.total, 0, 0, self.label)

    # -- internal ---------------------------------------------------------

    def _report(self):
        now = time.time()
        if self.cb and (now - self._last_ts >= 0.1 or self._done >= self.total):
            elapsed = now - self._start
            speed = self._done / elapsed if elapsed > 0 else 0
            eta = (self.total - self._done) / speed if speed > 0 else 0
            self.cb(self._done, self.total, speed, eta, self.label)
            self._last_ts = now


# ---------------------------------------------------------------------------
# Formatting (shared with CLI)
# ---------------------------------------------------------------------------

def fmt_size(n):
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {u}'
        n /= 1024
    return f'{n:.1f} PB'


def fmt_speed(bps):
    return fmt_size(bps) + '/s'


def fmt_eta(sec):
    if sec < 60:
        return f'{sec:.0f}s'
    if sec < 3600:
        return f'{sec / 60:.0f}m {sec % 60:.0f}s'
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    return f'{h}h {m}m'


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    import paramiko

    p = argparse.ArgumentParser(description='SSH SFTP Transfer Client')
    sp = p.add_subparsers(dest='cmd')

    def add_common(parser):
        parser.add_argument('--host', required=True)
        parser.add_argument('--port', type=int, default=22)
        parser.add_argument('--user', required=True)
        parser.add_argument('--password', default=None)
        parser.add_argument('--key', default=None)

    pp = sp.add_parser('push')
    add_common(pp)
    pp.add_argument('--local', required=True)
    pp.add_argument('--remote', required=True)

    pl = sp.add_parser('pull')
    add_common(pl)
    pl.add_argument('--remote', required=True)
    pl.add_argument('--local', required=True)

    args = p.parse_args()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(args.host, args.port, args.user, args.password,
                key_filename=args.key)
    sftp = ssh.open_sftp()

    def _progress(done, total, speed, eta, label):
        pct = done / total * 100 if total else 0
        bar_len = 30
        filled = int(bar_len * done / total) if total else 0
        bar = '█' * filled + '░' * (bar_len - filled)
        print(f'\r  {label}: [{bar}] {pct:5.1f}%  {fmt_size(done)}/{fmt_size(total)}  {fmt_speed(speed)}  ETA {fmt_eta(eta)}   ',
              end='', flush=True)

    try:
        if args.cmd == 'push':
            push(sftp, args.local, args.remote, on_progress=_progress)
        else:
            pull(sftp, args.remote, args.local, on_progress=_progress)
        print('\nDone.')
    finally:
        sftp.close()
        ssh.close()


if __name__ == '__main__':
    _cli()
