#!/usr/bin/env python3
"""
YCH Transfer Server — HTTP file transfer service.
Run this on the remote server to receive and send files.

Usage:
    python server.py --port 9090 --token mysecret
    python server.py --port 9090 --token mysecret --host 0.0.0.0

Endpoints:
    POST /push?path=<dir>   — Receive tar stream, extract to <dir>
    GET  /pull?path=<path>  — Send path as tar stream (dir) or raw bytes (file)
    HEAD /pull?path=<path>  — Return metadata (size, type) for resume support
    POST /shutdown          — Stop the server
    GET  /health            — Health check (no auth)
"""

import argparse
import hashlib
import json
import math
import os
import signal
import sys
import tarfile
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _compute_tar_size(path):
    """Exact byte size of a tar archive (GNU format) for `path`."""
    total = 0
    if os.path.isfile(path):
        total = 512 + _round_up(os.path.getsize(path), 512)
    else:
        # Root directory entry (os.walk does NOT include it)
        total += 512
        for root, dirs, files in os.walk(path):
            for name in dirs:
                total += 512  # directory header
            for name in files:
                full = os.path.join(root, name)
                total += 512 + _round_up(os.path.getsize(full), 512)
    total += 1024  # two end-of-archive blocks
    return total


def _round_up(n, block):
    return ((n + block - 1) // block) * block


def _sha256_of_file(path):
    sha = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Limited reader — prevents tarfile from over-reading the HTTP body
# ---------------------------------------------------------------------------

class _LimitedReader:
    """Wraps a readable stream, capping total bytes to `limit`."""
    def __init__(self, stream, limit):
        self._s = stream
        self._rem = limit

    def read(self, size=-1):
        if self._rem <= 0:
            return b''
        if size < 0 or size > self._rem:
            size = self._rem
        data = self._s.read(size)
        self._rem -= len(data)
        return data

    def readinto(self, buf):
        if self._rem <= 0:
            return 0
        size = min(len(buf), self._rem)
        data = self._s.read(size)
        buf[:len(data)] = data
        self._rem -= len(data)
        return len(data)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class TransferHandler(BaseHTTPRequestHandler):
    """Request handler with token-auth guarding push / pull / shutdown."""

    token: str = None           # set by main()

    # -- logging -----------------------------------------------------------

    def log_message(self, fmt, *args):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] {self.client_address[0]} — {fmt % args}")

    # -- helpers -----------------------------------------------------------

    def _check_auth(self):
        ah = self.headers.get('Authorization', '')
        if not ah.startswith('Bearer ') or ah[7:] != self.token:
            self._json_error(401, 'unauthorized — invalid or missing token')
            return False
        return True

    def _query_path(self):
        p = parse_qs(urlparse(self.path).query).get('path', [])
        return os.path.expanduser(p[0]) if p else None

    def _json_error(self, code, msg):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'error': msg}).encode())

    def _json_ok(self, extra=None):
        body = {'status': 'ok'}
        if extra:
            body.update(extra)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    # -- HEAD (metadata for resume) ---------------------------------------

    def do_HEAD(self):
        if urlparse(self.path).path.rstrip('/') != '/pull':
            self._json_error(400, 'HEAD only supported on /pull'); return
        if not self._check_auth(): return
        rp = self._query_path()
        if not rp:
            self._json_error(400, 'missing path'); return
        if not os.path.exists(rp):
            self._json_error(404, f'not found: {rp}'); return

        if os.path.isfile(rp):
            self.send_response(200)
            self.send_header('X-File-Type', 'file')
            self.send_header('X-File-Size', str(os.path.getsize(rp)))
            self.send_header('X-File-Name', os.path.basename(rp))
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header('X-File-Type', 'directory')
            self.send_header('X-File-Size', str(_compute_tar_size(rp)))
            self.send_header('X-File-Name', os.path.basename(rp))
            self.end_headers()

    # -- GET (pull) -------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path).path.rstrip('/')
        if route == '/health':
            self._json_ok()
            return
        if route != '/pull':
            self._json_error(404, f'unknown endpoint: {route}'); return
        if not self._check_auth(): return

        rp = self._query_path()
        if not rp:
            self._json_error(400, 'missing path'); return
        if not os.path.exists(rp):
            self._json_error(404, f'not found: {rp}'); return

        print(f"[{time.strftime('%H:%M:%S')}] Sending  {rp}")

        try:
            if os.path.isdir(rp):
                self._stream_dir(rp)
            else:
                self._stream_file(rp)
        except (BrokenPipeError, ConnectionResetError):
            pass    # client disconnected
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] ERROR sending: {exc}")

    def _stream_dir(self, path):
        size = _compute_tar_size(path)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.send_header('X-File-Type', 'directory')
        self.send_header('X-File-Name', os.path.basename(path))
        self.end_headers()

        with tarfile.open(mode='w|', fileobj=self.wfile, dereference=True, format=tarfile.GNU_FORMAT) as tar:
            tar.add(path, arcname=os.path.basename(path))

    def _stream_file(self, path):
        fsize = os.path.getsize(path)
        fname = os.path.basename(path)

        # Range header → partial content
        rng = self.headers.get('Range', '')
        start, end = 0, fsize - 1
        if rng:
            try:
                v = rng.replace('bytes=', '')
                a, b = v.split('-')
                start = int(a) if a else 0
                end = int(b) if b else fsize - 1
            except (ValueError, IndexError):
                self._json_error(400, 'bad Range header'); return

        content_len = end - start + 1
        code = 206 if rng else 200

        self.send_response(code)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(content_len))
        self.send_header('X-File-Type', 'file')
        self.send_header('X-File-Name', fname)
        self.send_header('X-File-SHA256', _sha256_of_file(path))
        if rng:
            self.send_header('Content-Range', f'bytes {start}-{end}/{fsize}')
        self.end_headers()

        with open(path, 'rb') as f:
            f.seek(start)
            remain = content_len
            while remain > 0:
                chunk = f.read(min(8 * 1024 * 1024, remain))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remain -= len(chunk)

    # -- POST (push / shutdown) -------------------------------------------

    def do_POST(self):
        route = urlparse(self.path).path.rstrip('/')

        if route == '/shutdown':
            if not self._check_auth(): return
            self._json_ok({'msg': 'server shutting down'})
            def _stop():
                time.sleep(0.3)
                print(f"[{time.strftime('%H:%M:%S')}] Shutdown requested by client.")
                self.server.shutdown()
            threading.Thread(target=_stop, daemon=True).start()
            return

        if route == '/push':
            if not self._check_auth(): return
            rp = self._query_path()
            if not rp:
                self._json_error(400, 'missing path'); return

            ftype = self.headers.get('X-File-Type', 'file')
            cl = int(self.headers.get('Content-Length', 0))
            print(f"[{time.strftime('%H:%M:%S')}] Receiving {ftype} → {rp}  ({cl / 1e6:.1f} MB)")

            try:
                if ftype == 'directory':
                    os.makedirs(rp, exist_ok=True)
                    reader = _LimitedReader(self.rfile, cl) if cl else self.rfile
                    with tarfile.open(mode='r|', fileobj=reader) as tar:
                        tar.extractall(path=rp, filter='data')
                else:
                    os.makedirs(os.path.dirname(rp) or '.', exist_ok=True)
                    # Resume support: Content-Range header
                    cr = self.headers.get('Content-Range', '')
                    mode = 'ab' if cr else 'wb'
                    with open(rp, mode) as f:
                        if cl:
                            remain = cl
                            while remain > 0:
                                chunk = self.rfile.read(min(8 * 1024 * 1024, remain))
                                if not chunk:
                                    break
                                f.write(chunk)
                                remain -= len(chunk)
                        else:
                            while True:
                                chunk = self.rfile.read(8 * 1024 * 1024)
                                if not chunk:
                                    break
                                f.write(chunk)

                # SHA256 verification if provided
                expected_sha = self.headers.get('X-File-SHA256', '')
                if expected_sha and os.path.isfile(rp):
                    actual = _sha256_of_file(rp)
                    if actual != expected_sha:
                        self._json_error(422, f'SHA256 mismatch: expected {expected_sha[:16]}…, got {actual[:16]}…')
                        return

                self._json_ok()
                print(f"[{time.strftime('%H:%M:%S')}] Done: {rp}")
            except Exception as exc:
                self._json_error(500, str(exc))
                print(f"[{time.strftime('%H:%M:%S')}] ERROR: {exc}")
            return

        self._json_error(404, f'unknown endpoint: {route}')


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

class _ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description='YCH Transfer Server')
    parser.add_argument('--port', type=int, default=9090)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--token', required=True)
    args = parser.parse_args()

    TransferHandler.token = args.token
    srv = _ThreadingServer((args.host, args.port), TransferHandler)

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] YCH Transfer Server")
    print(f"  Listening:  {args.host}:{args.port}")
    print(f"  Token:      {args.token}")
    print(f"  PID:        {os.getpid()}")

    def _on_signal(sig, frame):
        print(f"\n[{time.strftime('%H:%M:%S')}] Signal {sig}, stopping…")
        srv.shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print(f"[{time.strftime('%H:%M:%S')}] Server stopped.")


if __name__ == '__main__':
    main()
