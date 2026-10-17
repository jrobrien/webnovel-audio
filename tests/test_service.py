"""`service install|enable|disable|restart|status|remove`: the systemd user unit.

Nothing here reaches the real user manager: the unit directory is a temp dir and
every systemctl/journalctl/loginctl call is replaced by a recorder."""
import contextlib
import io
import json
import os
import subprocess

import pytest

from webnovel_audio import cli, service
from webnovel_audio.config import Config


class Fake:
    """Stands in for systemctl. `props` is what `show` reports."""

    def __init__(self):
        self.calls = []
        self.props = {"ActiveState": "inactive", "SubState": "dead", "MainPID": "0",
                      "UnitFileState": "disabled"}
        self.fail = set()

    def systemctl(self, *args):
        self.calls.append(args)
        rc = 1 if args[0] in self.fail else 0
        out = "".join(f"{k}={v}\n" for k, v in self.props.items()) if args[0] == "show" else ""
        return subprocess.CompletedProcess(args, rc, out, "boom" if rc else "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    fake = Fake()
    monkeypatch.setattr(service, "_systemctl", fake.systemctl)
    monkeypatch.setattr(service, "_journal", lambda n=8: "last log line")
    monkeypatch.setattr(service, "_linger", lambda: False)
    monkeypatch.setattr(service, "_port_in_use", lambda port, host="127.0.0.1": False)
    conf = tmp_path / "proj" / "config.toml"
    conf.parent.mkdir()
    conf.write_text("")
    return fake, str(conf), tmp_path


def verbs(fake):
    return [c[0] for c in fake.calls]


def test_unit_runs_serve_from_the_config_directory():
    text = service.unit_text("/srv/wn/config.toml", python="/srv/wn/.venv/bin/python3")
    assert "WorkingDirectory=/srv/wn\n" in text
    assert "ExecStart=/srv/wn/.venv/bin/python3 -m webnovel_audio serve -c /srv/wn/config.toml\n" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text


def test_unit_quotes_paths_with_spaces_and_percent():
    text = service.unit_text("/my dir/50%/config.toml", python="/py")
    assert 'serve -c "/my dir/50%%/config.toml"' in text


def test_install_creates_then_reports_unchanged_and_updates(env):
    fake, conf, tmp = env
    res = service.install(conf)
    assert res["result"] == "created" and os.path.exists(service.unit_path())
    assert verbs(fake) == ["daemon-reload"]
    fake.calls.clear()
    assert service.install(conf)["result"] == "unchanged" and fake.calls == []
    open(service.unit_path(), "a").write("# edited\n")
    assert service.install(conf)["result"] == "updated"
    assert open(service.unit_path()).read().endswith("WantedBy=default.target\n")


def test_install_dry_run_writes_nothing(env):
    fake, conf, tmp = env
    res = service.install(conf, dry_run=True)
    assert res["dry_run"] and res["result"] == "created" and "ExecStart=" in res["text"]
    assert not os.path.exists(service.unit_path()) and fake.calls == []


def test_actions_need_the_unit_installed(env):
    fake, conf, tmp = env
    for fn in (lambda: service.enable(Config()), service.disable, service.restart):
        with pytest.raises(service.ServiceError) as e:
            fn()
        assert e.value.code == "not_installed" and "service install" in e.value.hint
    assert fake.calls == []


def test_enable_starts_it_now_unless_told_not_to(env):
    fake, conf, tmp = env
    service.install(conf); fake.calls.clear()
    service.enable(Config())
    assert ("enable", "--now", service.UNIT) in fake.calls
    fake.calls.clear()
    service.enable(Config(), start=False)
    assert ("enable", service.UNIT) in fake.calls


def test_enable_refuses_when_something_else_holds_the_port(env, monkeypatch):
    fake, conf, tmp = env
    service.install(conf); fake.calls.clear()
    monkeypatch.setattr(service, "_port_in_use", lambda port, host="127.0.0.1": True)
    with pytest.raises(service.ServiceError) as e:
        service.enable(Config())
    assert e.value.code == "port_in_use" and "--no-start" in e.value.hint
    assert "enable" not in verbs(fake)
    # but enabling without starting is fine, and so is a service that is already running
    service.enable(Config(), start=False)
    fake.props["ActiveState"] = "active"
    service.enable(Config())


def test_disable_restart_and_a_failing_systemctl(env):
    fake, conf, tmp = env
    service.install(conf); fake.calls.clear()
    service.disable(); service.restart()
    assert ("disable", "--now", service.UNIT) in fake.calls and ("restart", service.UNIT) in fake.calls
    fake.fail.add("restart")
    with pytest.raises(service.ServiceError) as e:
        service.restart()
    assert e.value.code == "systemctl_failed" and "boom" in e.value.message


def test_remove_stops_deletes_and_is_safe_twice(env):
    fake, conf, tmp = env
    service.install(conf); fake.calls.clear()
    assert service.remove(dry_run=True)["was_installed"] and os.path.exists(service.unit_path())
    assert fake.calls == []
    res = service.remove()
    assert res["was_installed"] and not os.path.exists(service.unit_path())
    assert verbs(fake) == ["disable", "daemon-reload", "reset-failed"]
    fake.calls.clear()
    assert service.remove()["was_installed"] is False and fake.calls == []


def test_status_across_states(env):
    fake, conf, tmp = env
    st = service.status(Config())
    assert st["installed"] is False and st["state"] == "not installed" and fake.calls == []
    service.install(conf)
    st = service.status(Config())
    assert not st["active"] and not st["enabled"] and st["log"] == "last log line"
    fake.props.update(ActiveState="active", SubState="running", MainPID="4242",
                      UnitFileState="enabled", ActiveEnterTimestamp="Mon 2026-10-12 01:00:00 PDT")
    st = service.status(Config())
    assert st["active"] and st["enabled"] and st["pid"] == 4242 and "log" not in st
    assert st["url"] == "http://localhost:8080/" and st["linger"] is False


def _cli(conf, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["service", *argv, "-c", conf])
    return rc, buf.getvalue()


def test_cli_roundtrip_and_json_errors(env):
    fake, conf, tmp = env
    rc, out = _cli(conf, "enable", "--json")
    assert rc == 1 and json.loads(out)["error"]["code"] == "not_installed"
    rc, out = _cli(conf, "install")
    assert rc == 0 and "created" in out and "service enable" in out
    rc, out = _cli(conf, "status", "--json")
    d = json.loads(out)
    assert rc == 0 and d["installed"] and "text" not in d
    rc, out = _cli(conf, "remove")
    assert rc == 0 and "removed" in out
    rc, out = _cli(conf, "status")
    assert "not installed" in out


def test_cli_install_dry_run_prints_the_unit(env):
    fake, conf, tmp = env
    rc, out = _cli(conf, "install", "-n")
    assert rc == 0 and "dry run" in out and "ExecStart=" in out and not os.path.exists(service.unit_path())


def test_status_names_whatever_holds_the_port(env, monkeypatch):
    fake, conf, tmp = env
    monkeypatch.setattr(service, "_port_in_use", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(service, "_run", lambda argv: subprocess.CompletedProcess(
        argv, 0, 'LISTEN 0 5 0.0.0.0:8080 0.0.0.0:* users:(("webnovel-audio",pid=3837481,fd=3))\n', ""))
    st = service.status(Config())                                  # not installed, port taken
    assert st["port_busy"] and st["port_owner"] == {"name": "webnovel-audio", "pid": 3837481}
    service.install(conf)
    fake.props.update(ActiveState="active", SubState="running", MainPID="9")
    assert "port_busy" not in service.status(Config())             # the service itself holds it


def test_status_log_option_shows_the_journal_even_when_running(env):
    fake, conf, tmp = env
    service.install(conf)
    fake.props.update(ActiveState="active", SubState="running", MainPID="9")
    assert "log" not in service.status(Config())
    assert service.status(Config(), log_lines=5)["log"] == "last log line"
