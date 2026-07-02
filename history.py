#!/usr/bin/env python3
"""
SSH Transfer — connection history cache (shared by GUI and TUI).

Reads/writes ``~/.ssh_transfer_history.json``.  Both GUI and TUI import
this module so connection history stays in sync across interfaces.
"""

import json
from pathlib import Path

_HISTORY_FILE = Path.home() / '.ssh_transfer_history.json'
_MAX_HISTORY = 10


class _History:
    """Persistent per-user cache of recent SSH connection parameters."""

    def __init__(self):
        self.data = {'hosts': [], 'ssh_users': [], 'ssh_ports': []}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        try:
            if _HISTORY_FILE.exists():
                self.data = json.loads(_HISTORY_FILE.read_text())
        except Exception:
            pass

    def _save(self):
        try:
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HISTORY_FILE.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2)
            )
        except Exception:
            pass

    # -- public API --------------------------------------------------------

    def record(self, host, ssh_user, ssh_port):
        """Record a successful connection so it appears first next time."""
        for key, val in [('hosts', host),
                         ('ssh_users', ssh_user),
                         ('ssh_ports', str(ssh_port))]:
            lst = self.data.setdefault(key, [])
            if val in lst:
                lst.remove(val)
            lst.insert(0, val)
            lst[:] = lst[:_MAX_HISTORY]
        self._save()

    def last(self):
        """Return (host, user, port) of the most-recent connection."""
        h = (self.data.get('hosts') or [''])[0]
        u = (self.data.get('ssh_users') or [''])[0]
        p = (self.data.get('ssh_ports') or ['22'])[0]
        return h, u, p

    def values_for(self, key):
        """Return the ordered list of remembered values for *key*."""
        return self.data.get(key, [])
