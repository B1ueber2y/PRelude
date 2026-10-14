"""`prelude start` / `stop` / `status`: run the server detached until explicitly stopped."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import Config


def _pidfile(cfg: Config) -> Path:
    return cfg.data_dir / "prelude.pid"


def _logfile(cfg: Config) -> Path:
    return cfg.data_dir / "server.log"


def running_pid(cfg: Config) -> int | None:
    path = _pidfile(cfg)
    try:
        pid = int(path.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if b"prelude" in cmdline else None


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start(cfg: Config) -> str:
    if pid := running_pid(cfg):
        return f"PRelude is already running (pid {pid})."
    host, port = cfg.server.host, cfg.server.port
    if _port_open(host, port):
        return f"Something is already listening on {host}:{port}; stop it first."
    with open(_logfile(cfg), "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "prelude.cli", "serve"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    _pidfile(cfg).write_text(str(proc.pid))
    for _ in range(40):
        if _port_open(host, port):
            return f"PRelude is running on http://{host}:{port} (pid {proc.pid}). Stop it with `prelude stop`."
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    return f"PRelude didn't start; see {_logfile(cfg)}."


def stop(cfg: Config) -> str:
    pid = running_pid(cfg)
    if pid is None:
        _pidfile(cfg).unlink(missing_ok=True)
        return "PRelude isn't running (started with `prelude start`)."
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if running_pid(cfg) is None or not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.25)
    _pidfile(cfg).unlink(missing_ok=True)
    return f"PRelude stopped (pid {pid})."


def status(cfg: Config) -> str:
    pid = running_pid(cfg)
    return f"PRelude is running (pid {pid})." if pid else "PRelude is not running."
