"""Drive the Tcl UI's event handler with synthetic CLI events.

`handle_event` used to live inside `cmd_readable`, reachable only through a live
subprocess — which is how a broken `expr` in the success branch shipped: no test
ever completed a chapter. Feeding it every event shape catches that class of bug
without needing a render.
"""
import os
import re
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
    """The theme machinery must work under every Tk here.

    Split three ways: the vendored Catppuccin themes (ui/theme) must work
    unconditionally, on any machine, Omarchy or not -- vendoring them was the
    whole point, so this is the assertion carrying that promise. The live
    Omarchy theme is conditional on this specific machine. The plain
    dark/light pair is the true last resort, reached only if even a vendored
    file fails to source, and is exercised directly since nothing on a
    working machine reaches it through apply_theme's normal path.
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

    def assert_engine_theme(want, bg_hex=None):
        assert f"use {want} -> ttk=omarchy active={want}" in log, log
        # `ttk::style theme use` does not update ::ttk::currentTheme, only
        # `ttk::setTheme` does -- and that variable is exactly what a Python
        # tkinter host's Style().theme_use() reads. Regress this and the UI
        # still looks right under `wish` while a Python host reports
        # "default" and every ttk widget stays unthemed underneath it.
        assert "ttk::currentTheme omarchy" in log, log
        if bg_hex is not None:
            assert f"md.t.bg {bg_hex}" in log, log
        # four chapter-stage rows must be four actually-distinct colours: a
        # theme's red/green/yellow/blue are terminal palette slots, not a
        # categorical scale, and some Omarchy themes collapse them (measured
        # elsewhere: osaka-jade's `blue` is literally its `accent`)
        assert "distinct.count 4" in log, log
        # disabled buttons must be legible -- the theme's job, not a local
        # patch (unlike the vendored Forest theme this replaced)
        assert "distinct 1" in log, log

    # vendored: must work on every machine this suite runs on. Exact hex
    # asserted deliberately -- these are checked-in static files we own, so
    # a changed value means either a real edit or accidental corruption,
    # both worth failing loudly on rather than asserting loosely.
    assert_engine_theme("catppuccin-dark", "#29293a")
    assert_engine_theme("catppuccin-light", "#e5e7ed")
    # both vendored themes' four status colours must clear the "tellable
    # apart" threshold -- the first two `spread` lines in the log belong to
    # them, in the order probe_theme.tcl emits it
    spreads = [float(v) for v in re.findall(r"spread ([\d.]+)", log)]
    assert len(spreads) >= 2 and all(s > 25 for s in spreads[:2]), log

    if "omarchy.live_available 1" in log:
        assert_engine_theme("omarchy", None)
    else:
        assert "use omarchy -> SKIPPED" in log, log

    # the true last resort, reached only if a vendored file fails to source
    assert "use dark -> ttk=clam active=dark" in log, log
    assert "use light -> ttk=clam active=light" in log, log
    assert "md.t.bg #313131" in log and "md.t.fg #eeeeee" in log, log   # dark
    assert "md.t.bg #ffffff" in log and "md.t.fg #313131" in log, log   # light

    # auto must land on something real -- never a silent unstyled fallback
    assert re.search(
        r"auto -> ttk=(omarchy|clam) active=(omarchy|catppuccin-dark|catppuccin-light|dark|light)",
        log), log


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
