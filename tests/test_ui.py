"""Drive the Tcl UI's event handler with synthetic CLI events.

`handle_event` used to live inside `cmd_readable`, reachable only through a live
subprocess — which is how a broken `expr` in the success branch shipped: no test
ever completed a chapter. Feeding it every event shape catches that class of bug
without needing a render.
"""
import os
import subprocess
import sys

import pytest

from webnovel_audio.cli import _pick_tk_host

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tests", "ui", "feed_events.tcl")
HOST = os.path.join(ROOT, "ui", "host.py")
CONTROL = os.path.join(ROOT, "ui", "control.tcl")

has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
needs_display = pytest.mark.skipif(not has_display, reason="needs a display")


def _host_python():
    """The interpreter `webnovel-audio ui` would use — so the tests exercise
    the same Tk build the user gets, not whichever one runs pytest."""
    py, _warn = _pick_tk_host("") if has_display else (None, "")
    return py or sys.executable


def _tk_interpreters():
    """Every interpreter here with a working tkinter, de-duplicated by the Tk
    version it provides.

    Loading the theme has to work under *all* of them, not just the one the UI
    prefers: the versions differ (uv ships Tcl 9, the distro ships 8.6) and the
    `package require` bug was visible only under 9. Keyed by version so a
    machine with one Tk runs one case instead of three identical ones.
    """
    import shutil
    from webnovel_audio.cli import _tk_probe

    found = {}
    for py in (sys.executable, shutil.which("python3"), "/usr/bin/python3"):
        if not py:
            continue
        got = _tk_probe(py)
        if got and got[0] not in found:
            found[got[0]] = py
    return found


def _run(script, *args, python=None, timeout=90):
    return subprocess.run([python or _host_python(), HOST, script, *args],
                          cwd=ROOT, timeout=timeout, capture_output=True, text=True)


@needs_display
def test_ui_handles_every_event_shape(tmp_path):
    out = tmp_path / "events.log"
    r = _run(SCRIPT, str(out))
    log = out.read_text() if out.exists() else ""
    assert r.returncode == 0, f"{r.stderr}\n{log}"
    assert "FAILURES: 0" in log, log
    # the case that used to crash: a completed chapter with audio
    assert "ok (559.4s audio, 112.0s)" in log, log
    # a stage with no audio (fetch/parse) must not print an empty parenthetical
    assert "#5 ok\n" in log, log
    assert "#6 ERROR: boom" in log, log
    # a dry run must not claim the work happened
    assert "#7 would rendered" in log, log
    assert "refused: another render/sync holds the lock" in log, log


@needs_display
@pytest.mark.parametrize("tk_version", sorted(_tk_interpreters()) or [""])
def test_theme_loads_and_is_not_the_clam_fallback(tmp_path, tk_version):
    """The vendored Forest theme must actually load, under every Tk here.

    It once did not: `package require Tk 8.6` is unsatisfiable under Tcl 9
    (there it means "major 8"), so the theme refused to load and apply_theme
    silently fell back to clam. The UI still came up — just in the wrong
    colours, which nothing could see. Parametrising over interpreters is the
    point: run this only against the Tk the UI prefers and the Tcl 9 break is
    invisible, because the preferred one is 8.6.
    """
    interps = _tk_interpreters()
    if not tk_version:
        pytest.skip("no interpreter with tkinter")
    out = tmp_path / "theme.log"
    r = _run(os.path.join(ROOT, "tests", "ui", "probe_theme.tcl"), str(out),
             python=interps[tk_version])
    log = out.read_text() if out.exists() else ""
    assert r.returncode == 0, f"{r.stderr}\n{log}"
    assert "DONE" in log, log

    # both themes load and are actually selected, not silently swapped for clam
    assert "use forest-dark -> forest-dark" in log, log
    assert "use forest-light -> forest-light" in log, log
    assert "auto -> forest-" in log, log

    # and the colours ttk does not reach followed the switch. This is the
    # half that breaks if tk_setPalette is trusted: it runs once, at theme
    # *create* time, so switching back leaves the text panes on the old palette.
    assert "md.t.bg #313131" in log and "md.t.fg #eeeeee" in log, log   # dark
    assert "md.t.bg #ffffff" in log and "md.t.fg #313131" in log, log   # light
    assert "tag.rendered #5ec27f" in log, log                           # dark
    assert "tag.rendered #2a7d4f" in log, log                           # light


@needs_display
def test_ui_host_has_a_tk_that_can_render_text():
    """Guard the font regression that made the Markdown pane unreadable.

    A Tk built without Xft (python-build-standalone, so every uv-managed
    interpreter) sees exactly one font family and renders everything in the
    X11 `fixed` bitmap font, which has no apostrophe, curly quote, em dash or
    ellipsis — the characters web-novel prose is full of. `webnovel-audio ui`
    is supposed to route around such an interpreter; this asserts it found one.
    """
    from webnovel_audio.cli import _tk_probe

    py, warn = _pick_tk_host("")
    assert py, "no interpreter with a usable tkinter was found"
    version, families = _tk_probe(py)
    assert families > 1, (
        f"{py} has Tk {version} with {families} font family; "
        f"the UI would render text in the 'fixed' bitmap font. {warn}"
    )
