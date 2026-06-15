#!/usr/bin/env python3
"""
SSH Transfer Client — push / pull files and directories over HTTP.

Can be used as a library (import `push`, `pull`, `shutdown_server`)
or as a CLI tool (see `python client.py --help`).

Requirements: Python 3.8+ (stdlib only — no external deps).
"""

import hashlib
import http.client
import json
import math
import os
import sys
import tarfile
import time
import urllib.parse


# ---------------------------------------------------------------------------
# Tar size estimation (exact — for Content-Length)
# ---------------------------------------------------------------------------

def _compute_tar_size(path):
    """Exact byte size of the tar archive (GNU format) that push_dir would produce."""
    total = 0
    if os.path.isfile(path):
        total = 512 + _round_up(os.path.getsize(path), 512)
    else:
        # Root directory entry (os.walk does NOT include it)
        total += 512
        for root, dirs, files in os.walk(path):
            for name in dirs:
                total += 512  # directory header only
            for name in files:
                full = os.path.join(root, name)
                total += 512 + _round_up(os.path.getsize(full), 512)
    total += 1024  # two end-of-archive zero blocks
    return total


def _round_up(n, block):
    return ((n + block - 1) // block) * block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_of_file(path):
    sha = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _sha256_of_reader(reader, size):
    """Compute SHA256 while reading exactly `size` bytes from a callable `reader(n)`."""
    sha = hashlib.sha256()
    remain = size
    while remain > 0:
        chunk = reader(min(8 * 1024 * 1024, remain))
        if not chunk:
            break
        sha.update(chunk)
        remain -= len(chunk)
    return sha.hexdigest()


def _make_conn(host, port):
    """Create an HTTPConnection. Returns the conn object."""
    # Use a very long timeout — transfers can be GB-scale.
    return http.client.HTTPConnection(host, port, timeout=3600)


# ---------------------------------------------------------------------------
# Progress callback helpers
# ---------------------------------------------------------------------------

class _Progress:
    """Lightweight progress tracker for push/pull operations."""

    def __init__(self, total_bytes, callback, label=''):
        self.total = total_bytes
        self.done = 0
        self.cb = callback
        self.label = label
        self._start = time.time()
        self._last_done = 0
        self._last_ts = self._start

    def update(self, n):
        self.done += n
        now = time.time()
        # throttle to ~10 updates/sec
        if self.cb and (now - self._last_ts >= 0.1 or self.done >= self.total):
            elapsed = now - self._start
            speed = self.done / elapsed if elapsed > 0 else 0
            eta = (self.total - self.done) / speed if speed > 0 else 0
            self.cb(self.done, self.total, speed, eta, self.label)
            self._last_ts = now


# ---------------------------------------------------------------------------
# Push — send a file or directory to the server
# ---------------------------------------------------------------------------

def push(host, port, token, local_path, remote_path, on_progress=None):
    """Push `local_path` (file or directory) to `remote_path` on the server.

    Parameters
    ----------
    on_progress : callable(done, total, speed_bytes_sec, eta_seconds, label) or None
    """
    local_path = os.path.expanduser(local_path)
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)

    if os.path.isdir(local_path):
        conn = _make_conn(host, port)
        try:
            _push_dir(conn, token, local_path, remote_path, on_progress)
        finally:
            conn.close()
    else:
        _push_file(host, port, token, local_path, remote_path, on_progress)


def _push_dir(conn, token, local_path, remote_path, on_progress):
    tar_size = _compute_tar_size(local_path)
    label = os.path.basename(local_path)
    prog = _Progress(tar_size, on_progress, label)

    qs = urllib.parse.urlencode({'path': remote_path})
    conn.putrequest('POST', f'/push?{qs}')
    conn.putheader('Authorization', f'Bearer {token}')
    conn.putheader('X-File-Type', 'directory')
    conn.putheader('Content-Length', str(tar_size))
    conn.putheader('Content-Type', 'application/octet-stream')
    conn.endheaders()

    class _CountedWriter:
        """Wraps conn.send, calling prog.update on each chunk."""
        def __init__(self, conn, prog):
            self._c = conn
            self._p = prog
        def write(self, data):
            self._c.send(data)
            self._p.update(len(data))

    cw = _CountedWriter(conn, prog)
    with tarfile.open(mode='w|', fileobj=cw, dereference=True, format=tarfile.GNU_FORMAT) as tar:
        tar.add(local_path, arcname=os.path.basename(local_path))

    if on_progress:
        on_progress(prog.done, prog.total, 0, 0, f'{label} (等待服务器处理…)')
    _check_response(conn)


def _push_file(host, port, token, local_path, remote_path, on_progress):
    fsize = os.path.getsize(local_path)
    fname = os.path.basename(local_path)
    print(f'[DEBUG] _push_file: {fname} ({_fmt_size(fsize)}) → {remote_path}', flush=True)

    if on_progress:
        on_progress(0, fsize, 0, 0, f'{fname} (正在校验文件…)')
    sha = _sha256_of_file(local_path)
    print(f'[DEBUG] SHA256 computed, checking remote offset...', flush=True)

    # Check for remote partial file (resume) — use a separate connection
    # because Python's http.server closes the connection after each request.
    offset = 0
    try:
        head_conn = _make_conn(host, port)
        print(f'[DEBUG] HEAD connection created: {host}:{port}', flush=True)
        offset = _remote_file_offset(head_conn, token, remote_path)
        print(f'[DEBUG] Remote offset: {offset}', flush=True)
        head_conn.close()
        print(f'[DEBUG] HEAD connection closed', flush=True)
    except Exception as e:
        print(f'[DEBUG] HEAD request failed (continuing): {e}', flush=True)

    prog = _Progress(fsize, on_progress, fname)
    prog.update(offset)

    # Create a fresh connection for the actual upload
    print(f'[DEBUG] Creating POST connection to {host}:{port}...', flush=True)
    conn = _make_conn(host, port)
    print(f'[DEBUG] POST connection created, sending request...', flush=True)
    try:
        qs = urllib.parse.urlencode({'path': remote_path})
        conn.putrequest('POST', f'/push?{qs}')
        conn.putheader('Authorization', f'Bearer {token}')
        conn.putheader('X-File-Type', 'file')
        conn.putheader('X-File-SHA256', sha)
        if offset > 0:
            conn.putheader('Content-Range', f'bytes {offset}-{fsize - 1}/{fsize}')
        conn.putheader('Content-Length', str(fsize - offset))
        conn.putheader('Content-Type', 'application/octet-stream')
        print(f'[DEBUG] Sending headers...', flush=True)
        conn.endheaders()
        print(f'[DEBUG] Headers sent, streaming file body ({_fmt_size(fsize - offset)})...', flush=True)

        with open(local_path, 'rb') as f:
            f.seek(offset)
            remain = fsize - offset
            while remain > 0:
                chunk = f.read(min(8 * 1024 * 1024, remain))
                if not chunk:
                    break
                conn.send(chunk)
                remain -= len(chunk)
                prog.update(len(chunk))

        print(f'[DEBUG] File body sent, waiting for server response...', flush=True)
        if on_progress:
            on_progress(prog.done, prog.total, 0, 0, f'{fname} (等待服务器处理…)')
        _check_response(conn)
        print(f'[DEBUG] Server responded OK', flush=True)
    finally:
        conn.close()
        print(f'[DEBUG] POST connection closed', flush=True)


# ---------------------------------------------------------------------------
# Pull — fetch a file or directory from the server
# ---------------------------------------------------------------------------

def pull(host, port, token, remote_path, local_path, on_progress=None):
    """Pull `remote_path` from the server and save to `local_path`."""
    local_path = os.path.expanduser(local_path)

    # First, get metadata
    meta = _remote_head(conn := _make_conn(host, port), token, remote_path)
    conn.close()

    ftype = meta['type']
    rsize = int(meta.get('size', 0))
    rname = meta.get('name', os.path.basename(remote_path))
    label = rname

    conn = _make_conn(host, port)
    try:
        if ftype == 'directory':
            _pull_dir(conn, token, remote_path, local_path, rsize, label, on_progress)
        else:
            _pull_file(conn, token, remote_path, local_path, rsize, label, on_progress)
    finally:
        conn.close()


def _pull_dir(conn, token, remote_path, local_path, size, label, on_progress):
    prog = _Progress(size, on_progress, label)

    qs = urllib.parse.urlencode({'path': remote_path})
    conn.putrequest('GET', f'/pull?{qs}')
    conn.putheader('Authorization', f'Bearer {token}')
    conn.endheaders()

    if on_progress:
        on_progress(0, size, 0, 0, f'{label} (等待服务器响应…)')
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f'Server returned {resp.status}: {resp.read().decode()}')

    os.makedirs(local_path, exist_ok=True)

    class _CountedReader:
        def __init__(self, resp, prog):
            self._r = resp
            self._p = prog
        def read(self, size=-1):
            data = self._r.read(size if size >= 0 else 8 * 1024 * 1024)
            if data:
                self._p.update(len(data))
            return data
        def readinto(self, buf):
            size = len(buf)
            data = self._r.read(size)
            buf[:len(data)] = data
            if data:
                self._p.update(len(data))
            return len(data)

    reader = _CountedReader(resp, prog)
    with tarfile.open(mode='r|', fileobj=reader) as tar:
        tar.extractall(path=local_path, filter='data')


def _pull_file(conn, token, remote_path, local_path, size, label, on_progress):
    # Resume support
    offset = 0
    if os.path.exists(local_path):
        offset = os.path.getsize(local_path)
        if offset >= size:
            # Already complete — verify hash?
            return

    prog = _Progress(size, on_progress, label)
    prog.update(offset)

    qs = urllib.parse.urlencode({'path': remote_path})
    conn.putrequest('GET', f'/pull?{qs}')
    conn.putheader('Authorization', f'Bearer {token}')
    if offset > 0:
        conn.putheader('Range', f'bytes={offset}-')
    conn.endheaders()

    if on_progress:
        on_progress(offset, size, 0, 0, f'{label} (等待服务器响应…)')
    resp = conn.getresponse()
    if resp.status not in (200, 206):
        raise RuntimeError(f'Server returned {resp.status}: {resp.read().decode()}')

    mode = 'ab' if offset > 0 else 'wb'
    os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)

    with open(local_path, mode) as f:
        while True:
            chunk = resp.read(8 * 1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            prog.update(len(chunk))


# ---------------------------------------------------------------------------
# Server interaction helpers
# ---------------------------------------------------------------------------

def shutdown_server(host, port, token):
    """Ask the remote server to exit gracefully."""
    conn = _make_conn(host, port)
    try:
        conn.putrequest('POST', '/shutdown')
        conn.putheader('Authorization', f'Bearer {token}')
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f'Shutdown failed ({resp.status}): {resp.read().decode()}')
    finally:
        conn.close()


def health_check(host, port):
    """Return True if the server is reachable (short timeout)."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request('GET', '/health')
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 200
    except Exception:
        return False


def _remote_head(conn, token, remote_path):
    """Send HEAD /pull?path=... and return metadata dict."""
    qs = urllib.parse.urlencode({'path': remote_path})
    conn.putrequest('HEAD', f'/pull?{qs}')
    conn.putheader('Authorization', f'Bearer {token}')
    conn.endheaders()
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read().decode()
        raise RuntimeError(f'HEAD failed ({resp.status}): {body}')
    # Must read (even empty body) to allow connection reuse
    resp.read()
    return {
        'type': resp.getheader('X-File-Type', 'file'),
        'size': resp.getheader('X-File-Size', '0'),
        'name': resp.getheader('X-File-Name', ''),
    }


def _remote_file_offset(conn, token, remote_path):
    """Try to get the size of a partially-uploaded file on the server.

    Does a HEAD request: if the server reports a file, return its size
    (we'll resume from there).  If 404, return 0.
    """
    try:
        meta = _remote_head(conn, token, remote_path)
        if meta['type'] == 'file':
            return int(meta.get('size', 0))
    except Exception:
        pass
    return 0


def _check_response(conn):
    resp = conn.getresponse()
    body = resp.read().decode()
    if resp.status != 200:
        try:
            msg = json.loads(body).get('error', body)
        except Exception:
            msg = body
        raise RuntimeError(f'Server returned {resp.status}: {msg}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    p = argparse.ArgumentParser(description='SSH Transfer Client')
    sp = p.add_subparsers(dest='cmd')

    # push
    pp = sp.add_parser('push', help='Push a file/directory to the server')
    pp.add_argument('--host', required=True)
    pp.add_argument('--port', type=int, default=9090)
    pp.add_argument('--token', required=True)
    pp.add_argument('--local', required=True)
    pp.add_argument('--remote', required=True)

    # pull
    pl = sp.add_parser('pull', help='Pull a file/directory from the server')
    pl.add_argument('--host', required=True)
    pl.add_argument('--port', type=int, default=9090)
    pl.add_argument('--token', required=True)
    pl.add_argument('--remote', required=True)
    pl.add_argument('--local', required=True)

    # shutdown
    sd = sp.add_parser('shutdown', help='Shut down the remote server')
    sd.add_argument('--host', required=True)
    sd.add_argument('--port', type=int, default=9090)
    sd.add_argument('--token', required=True)

    # health
    hc = sp.add_parser('health', help='Check if server is alive')
    hc.add_argument('--host', required=True)
    hc.add_argument('--port', type=int, default=9090)

    args = p.parse_args()

    def _progress(done, total, speed, eta, label):
        pct = done / total * 100 if total else 0
        bar_len = 30
        filled = int(bar_len * done / total) if total else 0
        bar = '█' * filled + '░' * (bar_len - filled)
        speed_str = _fmt_speed(speed)
        eta_str = _fmt_time(eta)
        size_str = f'{_fmt_size(done)}/{_fmt_size(total)}'
        print(f'\r  {label}: [{bar}] {pct:5.1f}%  {size_str}  {speed_str}  ETA {eta_str}   ', end='', flush=True)

    if args.cmd == 'push':
        print(f"Pushing {args.local} → {args.host}:{args.port}{args.remote}")
        push(args.host, args.port, args.token, args.local, args.remote, on_progress=_progress)
        print('\nDone.')

    elif args.cmd == 'pull':
        print(f"Pulling {args.remote} ← {args.host}:{args.port} → {args.local}")
        pull(args.host, args.port, args.token, args.remote, args.local, on_progress=_progress)
        print('\nDone.')

    elif args.cmd == 'shutdown':
        shutdown_server(args.host, args.port, args.token)
        print(f'Server at {args.host}:{args.port} shutting down.')

    elif args.cmd == 'health':
        ok = health_check(args.host, args.port)
        print(f'Server {args.host}:{args.port} is {"alive" if ok else "unreachable"}.')

    else:
        p.print_help()


def _fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:6.1f} {unit}'
        n /= 1024
    return f'{n:6.1f} PB'


def _fmt_speed(bps):
    return _fmt_size(bps) + '/s'


def _fmt_time(sec):
    if sec < 60:
        return f'{sec:3.0f}s'
    if sec < 3600:
        return f'{sec/60:3.0f}m'
    return f'{sec/3600:.1f}h'


if __name__ == '__main__':
    _cli()
