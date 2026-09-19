"""Whether espeak-ng can find its data, and a readable answer when it cannot.

espeak-ng keeps the path to its data directory in a fixed-size buffer
(`N_PATH_HOME`, 160 bytes) and builds `<data>/phontab`, `<data>/phondata`,
`<data>/voices/...` inside it. Install it deep enough and the path no longer
fits, `espeak_Initialize` quietly ignores the path it was handed, and the
library falls back to the location it was *compiled* with — which, for the
wheel on PyPI, is the build machine's:

    Error processing file '/home/runner/work/espeakng-loader/espeakng-loader/
    espeak-ng/_dynamic/share/espeak-ng-data/phontab': No such file or directory.

espeak then calls exit(). Not an exception — the process is gone, with no
traceback, from inside what looks like an ordinary phonemize call. The
message names a path nobody has ever had, which sends you looking at the
wrong thing entirely.

Two things are worth knowing about the threshold. It is on the *canonical*
path, so a short symlink to a deep directory does not help. And it is a
buffer overrun, so behaviour either side of it is not crisp: measured here,
139 characters worked on one run and failed on the next.

That last point is why this refuses rather than warns, and why the limit is
a conservative round number instead of the largest value that happens to
work. There is no reliable boundary to sit next to. Being told to move the
project is a worse morning than not being told; it is a much better one than
a render that dies at chapter 40 quoting a path from a CI machine.

The limit is espeak's, not ours, and it is being fixed upstream: after
1.52.0 the POSIX path buffer grows to PATH_MAX. So the check is gated on the
version of the bundled library and lifts itself the moment a fixed one is
installed — rather than outliving the bug and constraining where the project
may live for no reason. The version is read from the soname, which needs no
call into the library, because calling into it is the thing that aborts.
"""
from __future__ import annotations

import os
import subprocess
import sys

#: The longest data path we will run with, on a library that still has the
#: small buffer. espeak's is 160 bytes and it appends filenames to it;
#: failures were already intermittent at 139. Not tuned to the maximum on
#: purpose — see the module docstring.
SAFE_LEN = 120

#: Releases after this one raise the POSIX path limit to PATH_MAX, so the
#: length check does not apply to them.
PATH_FIX_AFTER = (1, 52, 0)

_PROBE = (
    "import espeakng_loader;"
    "from phonemizer.backend.espeak.wrapper import EspeakWrapper as W;"
    "W.set_library(espeakng_loader.get_library_path());"
    "W.set_data_path(espeakng_loader.get_data_path());"
    "from phonemizer import phonemize;"
    "print(phonemize('test', language='en-us', backend='espeak'))"
)


def data_path() -> str:
    """The espeak data directory as espeak will see it, or "" if unavailable."""
    try:
        import espeakng_loader
    except ModuleNotFoundError:
        return ""
    try:
        return os.path.realpath(espeakng_loader.get_data_path())
    except Exception:
        return ""


def library_version() -> tuple[int, ...] | None:
    """espeak-ng's version, from the bundled soname. None if not determinable.

    Read from the filename rather than `espeak_Info`, because asking the
    library its version means initializing it, and initializing it is exactly
    what dies when the data path is too long.
    """
    try:
        import espeakng_loader
        lib = espeakng_loader.get_library_path()
    except Exception:                       # noqa: BLE001 - absent is fine
        return None
    best: tuple[int, ...] | None = None
    d = os.path.dirname(lib)
    stem = os.path.basename(lib) + "."      # libespeak-ng.so.
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for name in names:
        if not name.startswith(stem):
            continue
        parts = name[len(stem):].split(".")
        if parts and all(x.isdigit() for x in parts):
            v = tuple(int(x) for x in parts)
            if best is None or len(v) > len(best):
                best = v                    # prefer 1.52.0 over the 1 symlink
    return best


def too_long() -> str:
    """The problem with the data path, or "" if there isn't one."""
    version = library_version()
    if version is not None and version > PATH_FIX_AFTER:
        return ""                           # upstream raised the limit
    path = data_path()
    if not path or len(path) <= SAFE_LEN:
        return ""
    return (
        f"the espeak-ng data directory is {len(path)} characters deep, over the "
        f"{SAFE_LEN} this supports:\n"
        f"  {path}\n"
        f"espeak-ng holds that path in a 160-byte buffer. Past it, it ignores the\n"
        f"path it was given, reads from the directory it was compiled in on a CI\n"
        f"machine, prints an error about /home/runner/... and kills the process --\n"
        f"no traceback, mid-render.\n"
        f"Move the project somewhere shorter and re-run `uv sync`. Symlinking will\n"
        f"not help: the limit applies to the resolved path."
    )


def require_usable(where: str = "") -> None:
    """Refuse to start if the data path is too deep. Raises SystemExit.

    Called before anything constructs the phonemizer, because after that
    point there is nothing to catch: espeak exits the interpreter itself.
    """
    if problem := too_long():
        raise SystemExit(f"{where}{'\n' if where else ''}{problem}")


def probe() -> tuple[bool, str]:
    """Actually phonemize a word, in a subprocess. Returns (ok, detail).

    A subprocess because the failure is an abort, not an exception: espeak
    exits the interpreter from under us, so this cannot be a try/except.
    """
    try:
        p = subprocess.run([sys.executable, "-c", _PROBE],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if p.returncode == 0:
        return True, (p.stdout.strip().splitlines() or [""])[-1]
    detail = (p.stderr.strip() or p.stdout.strip()).splitlines()
    return False, (detail[-1] if detail else f"exited {p.returncode}")
