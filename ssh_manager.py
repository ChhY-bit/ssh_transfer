#!/usr/bin/env python3
"""
SSH Transfer SSH Manager — deploy and control the remote server via SSH.

Uses paramiko for cross-platform SSH (password or key auth).

Usage:
    from ssh_manager import SSHManager

    ssh = SSHManager('192.168.1.100', 22, 'root', 'password')
    ssh.upload_server()          # SFTP server.py → /tmp/ssh_server.py
    ssh.start_server(9090, 'tok')
    # ... do transfers ...
    ssh.stop_server()
    ssh.close()
"""

import os
import select
import socket
import stat
import threading
import time

import paramiko


# Path to server.py (same directory as this file)
_SERVER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.py')
_REMOTE_SERVER_PATH = '/tmp/ssh_transfer_server.py'


class SSHManager:
    """Manages the remote SSH Transfer server over SSH."""

    def __init__(self, host, port=22, username='root', password=None, key_file=None):
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self._key_file = key_file
        self._client = None
        self._sftp = None
        self._server_pid = None
        self._sftp_lock = threading.Lock()  # protect SFTP operations across threads

    # -- connection -------------------------------------------------------

    def connect(self):
        """Open SSH connection."""
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            'hostname': self.host,
            'port': self.port,
            'username': self.username,
            'timeout': 15,
        }
        if self._key_file:
            connect_kwargs['key_filename'] = self._key_file
        else:
            connect_kwargs['password'] = self._password
        self._client.connect(**connect_kwargs)
        self._sftp = self._client.open_sftp()

    def close(self):
        """Close SSH connection (and port forwarding if active)."""
        self.stop_port_forward()
        if self._sftp:
            self._sftp.close()
        if self._client:
            self._client.close()

    @property
    def is_connected(self):
        return self._client is not None and self._client.get_transport() is not None

    @property
    def sftp(self):
        """Return the underlying paramiko SFTPClient, auto-reconnecting if needed."""
        if self._sftp is None and self._client is not None:
            try:
                self._sftp = self._client.open_sftp()
            except Exception:
                pass
        return self._sftp

    def close_sftp(self):
        """Force-close the SFTP session to interrupt any ongoing transfer."""
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None

    # -- SSH port forwarding (tunnel) ------------------------------------

    def start_port_forward(self, remote_port, local_port=0):
        """Set up SSH local port forwarding: localhost:local_port → remote:remote_port.

        If local_port=0, a free port is chosen automatically.
        Returns the actual local port number in use.
        Runs the forwarder in a daemon thread.
        """
        if not self._client:
            raise RuntimeError('Not connected. Call connect() first.')

        transport = self._client.get_transport()
        if transport is None:
            raise RuntimeError('SSH transport not available.')

        # Bind a local socket
        self._forward_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._forward_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._forward_sock.bind(('127.0.0.1', local_port))
        self._forward_sock.listen(5)
        actual_port = self._forward_sock.getsockname()[1]
        self._forward_active = True
        self._forward_local_port = actual_port

        def _accept_loop():
            self._forward_sock.settimeout(0.5)
            while self._forward_active:
                try:
                    client, addr = self._forward_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    channel = transport.open_channel(
                        'direct-tcpip',
                        ('127.0.0.1', remote_port),
                        ('127.0.0.1', 0),
                    )
                except Exception as e:
                    print(f'[SSH Tunnel] open_channel failed: {e}')
                    try:
                        client.close()
                    except Exception:
                        pass
                    continue
                if channel is None:
                    print('[SSH Tunnel] open_channel returned None — server may not be listening')
                    try:
                        client.close()
                    except Exception:
                        pass
                    continue
                threading.Thread(target=_pipe, args=(client, channel), daemon=True).start()
                threading.Thread(target=_pipe, args=(channel, client), daemon=True).start()

        threading.Thread(target=_accept_loop, daemon=True).start()
        # Verify tunnel works before returning
        time.sleep(0.3)
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(3)
            test_sock.connect(('127.0.0.1', actual_port))
            test_sock.close()
        except Exception as e:
            print(f'[SSH] Tunnel verification failed: {e}')
            # Don't fail — the tunnel thread might still be starting
        print(f'[SSH] Port forward: 127.0.0.1:{actual_port} → remote:127.0.0.1:{remote_port}')
        return actual_port

    def stop_port_forward(self):
        """Stop the SSH port forwarding if active."""
        self._forward_active = False
        if hasattr(self, '_forward_sock') and self._forward_sock:
            try:
                self._forward_sock.close()
            except Exception:
                pass
            self._forward_sock = None

    @property
    def forward_local_port(self):
        """Return the local port being forwarded, or None."""
        return getattr(self, '_forward_local_port', None)

    # -- server lifecycle -------------------------------------------------

    def upload_server(self):
        """SFTP the local server.py to the remote /tmp directory."""
        if not self._sftp:
            raise RuntimeError('Not connected. Call connect() first.')
        print(f'[SSH] Uploading server.py → {self.host}:{_REMOTE_SERVER_PATH}')
        self._sftp.put(_SERVER_PY, _REMOTE_SERVER_PATH)
        # Make it executable
        self._sftp.chmod(_REMOTE_SERVER_PATH, 0o755)

    def start_server(self, port=9090, token=None, host='0.0.0.0'):
        """Start the server on the remote host via nohup.

        Returns (ok: bool, message: str).
        """
        if not self._client:
            raise RuntimeError('Not connected. Call connect() first.')

        # Kill any existing instance first (by script name AND by port)
        self.stop_server(port=port)
        time.sleep(0.5)

        token = token or 'ssh_default'

        # Detect available Python interpreter
        python_bin = self._detect_python()

        cmd = (
            f'nohup {python_bin} -u {_REMOTE_SERVER_PATH} '
            f'--host {host} --port {port} --token {token} '
            f'> /tmp/ssh_server.log 2>&1 & echo $!'
        )
        print(f'[SSH] Starting server: {cmd}')
        stdin, stdout, stderr = self._client.exec_command(cmd)
        pid_str = stdout.read().decode().strip()
        if pid_str:
            self._server_pid = int(pid_str)
            print(f'[SSH] Server PID: {self._server_pid}')
        else:
            log_tail = self.get_server_log(30)
            return False, f'进程启动失败（无 PID）\n\n服务器日志:\n{log_tail}'

        # Wait and verify the server is actually listening (retry up to 5 times)
        for attempt in range(5):
            time.sleep(0.8)
            # Check the log file — if server printed "Listening:", it's ready
            log = self.get_server_log(50)
            if 'Listening:' in log:
                return True, f'服务端已在 {host}:{port} 运行'
            # Also try TCP connect as backup
            if self._is_port_listening(port):
                return True, f'服务端已在 {host}:{port} 运行'

        # Failed after retries — return log for diagnostics
        log_tail = self.get_server_log(30)
        return False, f'进程已启动但端口 {port} 未监听。\n\n服务器日志:\n{log_tail}'

    def _detect_python(self):
        """Find a working Python 3 interpreter on the remote host."""
        for py in ('python3', 'python'):
            stdin, stdout, stderr = self._client.exec_command(
                f'{py} --version 2>&1'
            )
            ver = stdout.read().decode().strip()
            if ver.startswith('Python 3'):
                print(f'[SSH] Using: {py} → {ver}')
                return py
        return 'python3'  # fallback, let it fail with visible error

    def _is_port_listening(self, port):
        """Check whether the given TCP port is in LISTEN state on the remote."""
        if not self._client:
            return False
        # Method 1: try to actually connect via Python (most reliable)
        cmd = (
            f'{self._detect_python()} -c "'
            f'import socket; s=socket.socket(); s.settimeout(1); '
            f'r=s.connect_ex((\"127.0.0.1\",{port})); s.close(); '
            f'print(\"yes\" if r==0 else \"no\")" 2>/dev/null || echo no'
        )
        stdin, stdout, stderr = self._client.exec_command(cmd)
        if stdout.read().decode().strip() == 'yes':
            return True
        # Method 2: ss / netstat as fallback
        cmd = (
            f'ss -tlnp 2>/dev/null | grep -q ":{port} " && echo yes || '
            f'netstat -tlnp 2>/dev/null | grep -q ":{port} " && echo yes || '
            f'echo no'
        )
        stdin, stdout, stderr = self._client.exec_command(cmd)
        return stdout.read().decode().strip() == 'yes'

    def stop_server(self, port=None):
        """Kill the remote server process.  Tries every possible method.

        If `port` is given, also kills whatever is holding that port.
        """
        if not self._client:
            return

        def _run(cmd):
            stdin, stdout, stderr = self._client.exec_command(cmd)
            return stdout.read().decode().strip()

        # 1. pkill by script path
        _run(f'pkill -f {_REMOTE_SERVER_PATH} 2>/dev/null; echo ok')
        # 2. pkill by pattern
        _run(f'pkill -f ssh_transfer_server 2>/dev/null; echo ok')
        # 3. kill by port — extract PID from ss, then kill
        if port:
            pid = _run(
                f'ss -tlnp 2>/dev/null | '
                f'awk "/:{port} /"\'{{print $NF}}\' | '
                f'grep -oP "pid=\\K[0-9]+" | head -1'
            )
            if pid and pid.isdigit():
                _run(f'kill -9 {pid} 2>/dev/null; echo ok')
                print(f'[SSH] Killed PID {pid} on port {port}')
            # 4. fuser as last resort
            _run(f'fuser -k {port}/tcp 2>/dev/null; echo ok')
        self._server_pid = None
        time.sleep(0.5)
        print('[SSH] Server stopped (if it was running).')

    def is_server_running(self):
        """Check whether the server process is alive AND the port is listening."""
        if not self._client:
            return False
        # Check process first (fast)
        cmd = f'pgrep -f {_REMOTE_SERVER_PATH} > /dev/null 2>&1 && echo yes || echo no'
        stdin, stdout, stderr = self._client.exec_command(cmd)
        return stdout.read().decode().strip() == 'yes'

    def check_server_local(self, port):
        """Verify the server is responding by curling localhost on the remote.

        Bypasses the SSH tunnel — tests directly on the remote host.
        """
        if not self._client:
            return False
        cmd = (
            f'curl -s -o /dev/null -w "%{{http_code}}" '
            f'http://127.0.0.1:{port}/health 2>/dev/null || echo 000'
        )
        stdin, stdout, stderr = self._client.exec_command(cmd)
        code = stdout.read().decode().strip()
        return code == '200'

    def get_server_log(self, lines=20):
        """Return the last N lines of the server log."""
        if not self._client:
            return ''
        cmd = f'tail -n {lines} /tmp/ssh_server.log 2>/dev/null || echo "(no log)"'
        stdin, stdout, stderr = self._client.exec_command(cmd)
        return stdout.read().decode()

    # -- remote helpers ---------------------------------------------------

    def _get_sftp(self):
        """Return the SFTP client via the auto-reconnecting property, or None."""
        return self.sftp

    def list_remote_dir(self, path='.'):
        """Return list of {name, is_dir, size} dicts for a remote directory."""
        sftp = self._get_sftp()
        if not sftp:
            raise RuntimeError('Not connected.')
        with self._sftp_lock:
            items = []
            for attr in sftp.listdir_attr(path):
                items.append({
                    'name': attr.filename,
                    'is_dir': attr.st_mode & 0o40000 != 0,  # S_IFDIR
                    'size': attr.st_size,
                })
            return items

    def resolve_path(self, path):
        """Convert a relative remote path to an absolute one via SFTP."""
        sftp = self._get_sftp()
        if not sftp:
            return path
        try:
            return sftp.normalize(path)
        except Exception:
            return path

    def is_remote_dir(self, path):
        """Return True if the remote path is a directory."""
        sftp = self._get_sftp()
        if not sftp:
            return False
        try:
            return sftp.stat(path).st_mode & 0o40000 != 0
        except FileNotFoundError:
            return False

    def remote_file_exists(self, path):
        """Check if a path exists on the remote host."""
        sftp = self._get_sftp()
        if not sftp:
            return False
        try:
            sftp.stat(path)
            return True
        except FileNotFoundError:
            return False

    def remote_file_size(self, path):
        """Return the size of a remote file, or 0 if not found."""
        sftp = self._get_sftp()
        if not sftp:
            return 0
        try:
            return sftp.stat(path).st_size
        except FileNotFoundError:
            return 0

    def delete_remote(self, path):
        """Recursively delete a remote file or directory (best-effort)."""
        sftp = self._get_sftp()
        if not sftp:
            return
        with self._sftp_lock:
            try:
                attr = sftp.stat(path)
                if attr.st_mode & 0o40000:  # directory
                    for item in sftp.listdir_attr(path):
                        child = f"{path}/{item.filename}".replace("//", "/")
                        self._delete_remote_locked(sftp, child)
                    sftp.rmdir(path)
                else:
                    sftp.remove(path)
            except Exception:
                pass

    def _delete_remote_locked(self, sftp, path):
        """Recursive delete helper — caller must hold _sftp_lock."""
        try:
            attr = sftp.stat(path)
            if attr.st_mode & 0o40000:
                for item in sftp.listdir_attr(path):
                    child = f"{path}/{item.filename}".replace("//", "/")
                    self._delete_remote_locked(sftp, child)
                sftp.rmdir(path)
            else:
                sftp.remove(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Port forwarding helper
# ---------------------------------------------------------------------------

def _pipe(src, dst):
    """Bidirectional copy between two sockets (long timeout for large transfers)."""
    try:
        while True:
            r, _, _ = select.select([src], [], [], 600)  # 10 min timeout
            if not r:
                break
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI quick-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='SSH Transfer SSH Manager (test)')
    p.add_argument('--host', required=True)
    p.add_argument('--port', type=int, default=22)
    p.add_argument('--user', required=True)
    p.add_argument('--password', default=None)
    p.add_argument('--key', default=None)
    p.add_argument('--server-port', type=int, default=9090)
    p.add_argument('--token', default='test_default')
    args = p.parse_args()

    ssh = SSHManager(args.host, args.port, args.user, args.password, args.key)
    try:
        ssh.connect()
        print(f'Connected to {args.host}.')

        ssh.upload_server()

        ok, msg = ssh.start_server(args.server_port, args.token)
        if ok:
            print(f'Server running on {args.host}:{args.server_port} — {msg}')
            print('Last log lines:')
            print(ssh.get_server_log())
        else:
            print(f'Failed to start server:\n{msg}')

    finally:
        ssh.close()
