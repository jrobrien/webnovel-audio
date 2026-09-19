"""Host the Tcl/Tk control UI in a Python interpreter.

Deliberately dependency-free — it imports nothing from webnovel_audio. The UI
shells out to the `webnovel-audio` CLI for every piece of data it shows, so the
interpreter hosting it needs a working `tkinter` and nothing else. That is what
lets `webnovel-audio ui` run the UI under a *different* interpreter than the
one the CLI itself is installed in, which is how it escapes a bundled Tk that
cannot render text (see _tk_probe in cli.py).

    python ui/host.py ui/control.tcl
    python ui/host.py tests/ui/feed_events.tcl /tmp/events.log

Trailing arguments are handed to the script as Tcl's `argv`, so a test driver
runs through exactly the same host as the real UI.
"""
import sys
import tkinter


def main(argv):
    if len(argv) < 2:
        print(f"usage: {argv[0]} <script.tcl> [args...]", file=sys.stderr)
        return 2
    script, rest = argv[1], argv[2:]
    root = tkinter.Tk()
    # Tkinter converts a Python list into a proper Tcl list, so a path with a
    # space survives; building the string by hand would not.
    root.tk.call("set", "argv0", script)
    root.tk.call("set", "argc", len(rest))
    root.tk.call("set", "argv", rest)
    # `source` rather than eval so [info script] resolves, which is how
    # control.tcl finds json.tcl and the vendored theme next to itself.
    root.tk.call("source", script)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
