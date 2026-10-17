"""The web server as a systemd *user* service.

`webnovel-audio service install|enable|disable|restart|status|remove` writes and
drives one unit, `webnovel-audio.service`, in `~/.config/systemd/user/`. It runs the
same `serve` the command line does, from the config file's own directory (the
config's library and state paths are relative to it), and restarts it if it dies.

Everything that touches systemd goes through `_systemctl` / `_journal` / `_loginctl`,
so tests substitute them and never reach the real user manager.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys

from .config import Config

UNIT = "webnovel-audio.service"


class ServiceError(Exception):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


def unit_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user")


def unit_path() -> str:
    return os.path.join(unit_dir(), UNIT)


def _quote(arg: str) -> str:
    """One ExecStart word: double-quoted when it has spaces, quotes or a backslash;
    `%` is doubled because systemd expands it."""
    arg = arg.replace("%", "%%")
    if any(c.isspace() or c in '"\\' for c in arg):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def unit_text(config_path: str, python: str | None = None) -> str:
    """The unit file for this installation. `config_path` is made absolute, and the
    service runs from its directory so relative library/state paths resolve."""
    config = os.path.abspath(config_path)
    workdir = os.path.dirname(config)
    argv = [python or sys.executable, "-m", "webnovel_audio", "serve", "-c", config]
    return (
        "[Unit]\n"
        "Description=webnovel-audio web player and podcast feeds\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workdir}\n"
        f"ExecStart={' '.join(_quote(a) for a in argv)}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# -- the only places that run external programs ---------------------------------

def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ServiceError("no_systemd", f"{argv[0]} not found",
                           "this needs a systemd user session") from None


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args])


def _journal(lines: int = 8) -> str:
    return _run(["journalctl", "--user", "-u", UNIT, "-n", str(lines), "--no-pager",
                 "-o", "cat"]).stdout.strip()


def _linger() -> bool | None:
    out = _run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"])
    return out.stdout.strip() == "yes" if out.returncode == 0 else None


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _port_owner(port: int) -> dict | None:
    """The process listening on `port` ({pid, name}), via `ss`; None if unknown."""
    out = _run(["ss", "-ltnp", f"sport = :{port}"]).stdout
    import re
    m = re.search(r'users:\(\("([^"]*)",pid=(\d+)', out)
    return {"name": m.group(1), "pid": int(m.group(2))} if m else None


def _must(proc: subprocess.CompletedProcess, what: str) -> None:
    if proc.returncode != 0:
        raise ServiceError("systemctl_failed", f"{what} failed: "
                           f"{(proc.stderr or proc.stdout).strip() or 'exit ' + str(proc.returncode)}")


def _installed() -> bool:
    return os.path.exists(unit_path())


def _need_installed() -> None:
    if not _installed():
        raise ServiceError("not_installed", f"{UNIT} is not installed",
                           "webnovel-audio service install")


def _props() -> dict:
    out = _systemctl("show", UNIT, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                     "-p", "UnitFileState", "-p", "ActiveEnterTimestamp", "-p", "NRestarts",
                     "-p", "Result")
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


# -- actions ----------------------------------------------------------------------

def install(config_path: str, *, dry_run: bool = False) -> dict:
    """Write (or refresh) the unit file and reload systemd. Does not enable or start."""
    text = unit_text(config_path)
    path = unit_path()
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    state = "unchanged" if old == text else ("updated" if old is not None else "created")
    res = {"ok": True, "action": "install", "unit": path, "result": state,
           "dry_run": dry_run, "text": text}
    if dry_run or state == "unchanged":
        return res
    os.makedirs(unit_dir(), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(path + ".tmp", path)
    _must(_systemctl("daemon-reload"), "daemon-reload")
    return res


def enable(cfg: Config, *, start: bool = True) -> dict:
    """Start at login (and now, unless `start` is False)."""
    _need_installed()
    if start and _props().get("ActiveState") != "active" and _port_in_use(cfg.serve.port):
        raise ServiceError(
            "port_in_use", f"port {cfg.serve.port} is already in use, so the service could not start",
            "stop the `serve` you started by hand (or the UI's server), then run this again; "
            "or use --no-start")
    _must(_systemctl("enable", *(["--now"] if start else []), UNIT), "enable")
    return {"ok": True, "action": "enable", "started": start}


def disable() -> dict:
    """Stop it and stop it starting at login. The unit file stays."""
    _need_installed()
    _must(_systemctl("disable", "--now", UNIT), "disable")
    return {"ok": True, "action": "disable"}


def restart() -> dict:
    _need_installed()
    _must(_systemctl("restart", UNIT), "restart")
    return {"ok": True, "action": "restart"}


def remove(*, dry_run: bool = False) -> dict:
    """Stop, disable and delete the unit file. Safe to run when nothing is installed."""
    res = {"ok": True, "action": "remove", "unit": unit_path(), "dry_run": dry_run,
           "was_installed": _installed()}
    if dry_run or not res["was_installed"]:
        return res
    _systemctl("disable", "--now", UNIT)       # fine if it was never enabled
    os.remove(unit_path())
    _must(_systemctl("daemon-reload"), "daemon-reload")
    _systemctl("reset-failed", UNIT)
    return res


def status(cfg: Config, log_lines: int = 0) -> dict:
    """What a person needs to know: is it there, on, running, reachable, and why not."""
    out = {"ok": True, "action": "status", "unit": unit_path(), "installed": _installed(),
           "url": f"http://localhost:{cfg.serve.port}/", "port": cfg.serve.port}
    if not out["installed"]:
        out.update(enabled=False, active=False, state="not installed")
        _note_port(out)
        return out
    p = _props()
    out.update(
        enabled=p.get("UnitFileState") in ("enabled", "enabled-runtime"),
        active=p.get("ActiveState") == "active",
        state=f"{p.get('ActiveState', '?')} ({p.get('SubState', '?')})",
        pid=int(p["MainPID"]) if p.get("MainPID", "0") not in ("", "0") else None,
        since=p.get("ActiveEnterTimestamp") or None,
        restarts=int(p.get("NRestarts") or 0),
        result=p.get("Result"),
        linger=_linger(),
    )
    if log_lines or not out["active"]:
        out["log"] = _journal(log_lines or 8)
    _note_port(out)
    return out


def _note_port(out: dict) -> None:
    """If the port is held and the service is not what holds it, say what does."""
    if out.get("active") or not _port_in_use(out["port"]):
        return
    out["port_busy"] = True
    out["port_owner"] = _port_owner(out["port"])
