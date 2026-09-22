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


def _run(script, *args, python=None, timeout=90, ui_conf=None):
    """Run one probe under the UI's own host.

    `ui_conf` redirects the UI's config file. Always pass it for a probe that
    reads or writes settings: control.tcl loads the config at *source* time,
    so a probe cannot redirect the path itself, and without this it both
    asserts against and overwrites the developer's real ui.conf.
    """
    env = dict(os.environ, WEBNOVEL_AUDIO_UI_CONF=str(ui_conf or ""))
    return subprocess.run([python or _host_python(), HOST, script, *args],
                          cwd=ROOT, timeout=timeout, capture_output=True,
                          text=True, env=env)


@needs_display
def test_ui_handles_every_event_shape(tmp_path):
    out = tmp_path / "events.log"
    r = _run(SCRIPT, str(out), ui_conf=tmp_path / "ui.conf")
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
def test_font_zoom_moves_everything_and_survives_a_restart(tmp_path):
    """Ctrl +/-/0 resizes the whole window, and the level is remembered.

    Everything here rides on the UI using only Tk's *named* fonts, so a
    single `font configure` per name moves the entire window. The moment
    someone hardcodes a font on a widget, that widget stops following and
    this test's size table stops being uniform.
    """
    out = tmp_path / "fonts.log"
    r = _run(os.path.join(ROOT, "tests", "ui", "probe_fonts.tcl"), str(out),
             ui_conf=tmp_path / "ui.conf")
    log = out.read_text() if out.exists() else ""
    assert r.returncode == 0, f"{r.stderr}\n{log}"
    assert "DONE" in log, log
    assert "ERROR" not in log, log

    def snap(label):
        m = re.search(rf"^{re.escape(label)} zoom (-?\d+)\n  sizes (.*)\n"
                      rf"  rowheight (\d+)$", log, re.M)
        assert m, f"no {label!r} block in:\n{log}"
        parts = m.group(2).split()
        sizes = dict(zip(parts[::2], (int(v) for v in parts[1::2])))
        return int(m.group(1)), sizes, int(m.group(3))

    boot_zoom, boot, boot_row = snap("boot")
    assert boot_zoom == 0, log

    # three "larger" keys -- and deliberately three different spellings of
    # them, since ctrl-plus arrives as <Control-equal> or <Control-plus>
    # depending on shift, and the numpad sends its own keysym entirely
    zoom, sizes, row = snap("after 3 larger")
    assert zoom == 3, log
    assert all(sizes[f] == boot[f] + 3 for f in boot), log
    # ttk computes Treeview -rowheight once, from the font as it was when the
    # theme was set up, and does not revisit it. Miss that and zooming in
    # clips every row of both trees.
    assert row > boot_row, log
    # the keys must not also type into the editable pane they were sent to
    assert "editor.text 'seed'" in log, log

    zoom, sizes, _ = snap("after 2 smaller")
    assert zoom == 1, log
    assert all(sizes[f] == boot[f] + 1 for f in boot), log

    # the level has to survive a restart, through the real save/load
    assert "roundtrip saved 1 reloaded 1" in log, log
    # ...and a hand-edited ui.conf must not take the UI down at startup,
    # which happens before the log pane exists to explain itself
    assert "junk.survived 1" in log, log

    zoom, sizes, row = snap("after reset")
    assert (zoom, sizes, row) == (0, boot, boot_row), log

    # The limits refuse rather than clamp silently, but what actually matters
    # is that nothing is left at an unreadable size in either direction.
    _, floor, _ = snap("floor")
    _, ceiling, _ = snap("ceiling")
    assert all(6 <= v <= 42 for v in floor.values()), log
    assert all(6 <= v <= 42 for v in ceiling.values()), log
    assert all(floor[f] < boot[f] < ceiling[f] for f in boot), log

    assert snap("final")[1] == boot, log


@needs_display
@pytest.mark.parametrize("tk_version", sorted(_tk_interpreters()) or [""])
def test_colours_come_from_the_resource_database(tmp_path, tk_version):
    """Colour must work under every Tk here, with no theme package involved.

    tk-omarchy-theme deleted its ttk theme package; the X resource database
    is the whole contract now. That splits this two ways. "dark" and "light"
    are checked-in override fragments (ui/theme), so they must work
    unconditionally on any machine, Omarchy or not, and their values are
    asserted exactly. "system" is whatever this particular machine's root
    window says, so it is asserted for shape rather than for hexes.

    The Tk-version axis is the point of the parametrisation: Tk 9's ttk
    `default` theme reads the resource database itself, Tk 8.6's does not and
    comes up compiled-in grey. xres::style_ttk is what makes the two agree,
    and only running this under both catches it regressing.
    """
    interps = _tk_interpreters()
    if not tk_version:
        pytest.skip("no interpreter with tkinter")
    out = tmp_path / "theme.log"
    r = _run(os.path.join(ROOT, "tests", "ui", "probe_theme.tcl"), str(out),
             python=interps[tk_version], ui_conf=tmp_path / "ui.conf")
    log = out.read_text() if out.exists() else ""
    assert r.returncode == 0, f"{r.stderr}\n{log}"
    assert "DONE" in log, log

    def block(mode, nth=0):
        """The probe's report for one `apply_theme <mode>`, as a dict."""
        blocks = re.findall(rf"^use {mode} -> (.*?)(?=^use |^DONE)", log,
                            re.S | re.M)
        assert len(blocks) > nth, log
        got = {}
        for line in blocks[nth].splitlines():
            parts = line.split()
            if parts:
                got[parts[0]] = parts[1] if len(parts) > 1 else ""
        return got

    for mode, bg, fg, field in (("dark", "#1e1e2e", "#cdd6f4", "#29293a"),
                                ("light", "#eff1f5", "#4c4f69", "#e5e7ed")):
        b = block(mode)
        # ttk must agree with the resource database under BOTH Tk versions.
        # On 9 the default theme already read it; on 8.6 nothing did, and an
        # override is invisible to ttk on either -- `option add` does not
        # retint an existing style. xres::style_ttk is the only reason these
        # two lines hold, which is why it runs unconditionally.
        assert b["ttk.bg"] == bg, log
        assert b["ttk.fg"] == fg, log
        # classic widgets, which honour the database directly on every Tk
        assert b["md.t.bg"] == field, log
        assert b["md.t.fg"] == fg, log
        # The vendored clamx theme (ui/vendor) must actually be selected --
        # on Tk 9 it is easy to miss its absence, since the stock `default`
        # theme reads the same resources and looks nearly right.
        #
        # Asserted on ::ttk::currentTheme rather than `ttk::style theme use`
        # because only `ttk::setTheme` updates that variable, and it is
        # exactly what a Python tkinter host's Style().theme_use() reads.
        # Select with `theme use` instead and the UI looks right under wish
        # while the host reports the wrong theme.
        assert b["ttk::currentTheme"] == "clamx", log
        # four chapter-stage rows must be four actually-distinct colours.
        # The picker that used to guarantee this went away with the theme
        # package, so this now rests on the fragments' own hues -- which is
        # exactly why they are checked in and asserted rather than derived.
        assert b["distinct.count"] == "4", log
        # disabled buttons must stay legible against the background
        assert b["disabled.distinct"] == "1", log
        # Nothing may still be answering with one of Tk's compiled-in greys.
        # This is the general form of a bug reported from the real UI: the
        # window was correct except for a row of grey notebook tabs, because
        # the unselected tab is a separate style from the strip behind it and
        # `ttk::style configure TNotebook` does not reach it. Only visible on
        # Tk 8.6, where ttk reads no resources of its own.
        assert b["greys"] == "0", log
        # and the tabs specifically, recessed against the selected one
        assert b["tab.bg"] != b["tab.bg.selected"], log
        assert b["tab.bg.selected"] == bg, log

    # Switching back and forth must land the same way every time. Tk has no
    # `option delete`, so an override can only ever be outranked by a later
    # one; get the priorities wrong and the second `dark` differs from the
    # first, or `system` keeps the override's colours.
    assert block("dark", 0) == block("dark", 1), log

    sysb = block("system")
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", sysb["ttk.bg"]), log
    assert sysb["distinct.count"] == "4", log
    # "system" must not be left wearing the override that ran before it: after
    # dark/light/dark it has to come back to what the desktop itself says.
    #
    # Compared against the boot snapshot rather than asserted to differ from
    # a hardcoded hex -- the earlier spelling here was `!= "#1e1e2e"`, which
    # turned into a false failure the day the desktop theme was switched to
    # catppuccin and the live palette legitimately became Mocha. The
    # invariant is "system equals the desktop", and on a machine themed to
    # match a fragment those are the same colour.
    boot_bg = re.search(r"boot\.background (#[0-9a-fA-F]{6})\n", log)
    assert boot_bg, log
    assert sysb["ttk.bg"] == boot_bg.group(1), log

    # the light/dark signal: the explicit *omarchyMode resource where the
    # desktop publishes one, luminance of the background where it does not.
    # Must always answer, including on a machine with a bare root window.
    assert re.search(r"boot\.is_dark [01]\n", log), log

    # The floor under a bare root window is safe only because of its
    # priority. Where this machine has a desktop palette, the floor must lose
    # to it and a light/dark override must beat the floor -- get that
    # backwards and every machine silently pins to Mocha.
    if "floor.loses_to_desktop" in log:
        assert "floor.loses_to_desktop 1" in log, log
        assert "override.beats_floor 1" in log, log

    # clamx must still match clam's geometry. It is `-parent clam`, which
    # carries layouts and elements but NOT style settings, so a child theme
    # silently loses -relief, -padding, -width and -font: buttons then render
    # as flat text barely wider than a label (measured: 52x22 against clam's
    # 93x32). clamx restates them, and this asserts a re-vendored copy still
    # does -- independently of upstream's own suite.
    #
    # Measured on a real widget rather than read from `ttk::style lookup`,
    # which does not traverse theme parents and so cannot distinguish an
    # unset option from an inherited one. And kept separate from the
    # zero-grey sweep, which is blind to this entire class: nothing about it
    # shows up in colour.
    geom = dict(re.findall(r"geometry\.(\w+) (\d+x\d+)\n", log))
    assert geom.get("clam") == geom.get("clamx"), log
    assert "geometry.relief 'raised'" in log, log

    # The vendored copy must be the version xres.tcl was written against.
    # Upstream bumps it on every change that alters what the file produces,
    # so an unequal version is the signal to re-read its notes rather than
    # let a vendored copy drift quietly out of step.
    assert re.search(r"clamx\.version (\S+) wanted \1\n", log), log


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
