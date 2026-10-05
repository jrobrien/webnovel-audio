"""The docs' command lines must be commands the parser accepts.

The README once documented a positional style (`render <slug> 1-10`) that the
flag-based parser rejects, and nothing noticed. Two checks keep that from
recurring: every command line in a fenced block must parse, and every `--flag`
named in an inline-code command must exist on that command."""
import argparse
import os
import re
import shlex

import pytest

from webnovel_audio import cli

ROOT = os.path.join(os.path.dirname(__file__), "..")
DOCS = ["README.md", "SETUP.md", os.path.join("docs", "DESIGN.md")]
_PREFIX = re.compile(r"^(?:uv run )?webnovel-audio\s+")


def _read(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


def _fenced_commands(text):
    """Logical command lines inside ```sh blocks (continuations joined)."""
    out, in_block, buf = [], False, ""
    for line in text.splitlines():
        if line.startswith("```"):
            in_block = line.startswith(("```sh", "```bash")) if not in_block else False
            continue
        if not in_block:
            continue
        buf += line.rstrip()
        if buf.endswith("\\"):
            buf = buf[:-1] + " "
            continue
        if _PREFIX.match(buf.strip()):
            out.append(buf.strip())
        buf = ""
    return out


def _parse(line):
    line = _PREFIX.sub("", line)
    if line.strip() in ("--version", "--help"):
        return None
    line = line.split("|")[0]  # `… --json | jq …`
    argv = shlex.split(line, comments=True)
    parser = cli._build_parser()
    # argparse exits on error; turn that into a failure with its message.
    err = []
    parser.error = lambda msg: err.append(msg) or (_ for _ in ()).throw(SystemExit(2))
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                sub.error = parser.error
                for a in sub._actions:
                    if isinstance(a, argparse._SubParsersAction):
                        for s2 in a.choices.values():
                            s2.error = parser.error
    try:
        parser.parse_args(argv)
    except SystemExit:
        return err[0] if err else "exit"
    return None


def _command_flags():
    """{('series','add'): {'--url', ...}, ('render',): {...}}"""
    table = {}

    def walk(parser, path):
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        flags = {f for a in parser._actions for f in a.option_strings}
        table[path] = flags
        for sp in subs:
            for name, child in sp.choices.items():
                walk(child, path + (name,))

    walk(cli._build_parser(), ())
    return table


_CASES = [(d, c) for d in DOCS for c in _fenced_commands(_read(d))]


@pytest.mark.parametrize("doc,line", _CASES, ids=[f"{d}:{c[:50]}" for d, c in _CASES])
def test_fenced_command_parses(doc, line):
    assert _parse(line) is None, f"{doc}: `{line}` -> {_parse(line)}"


def test_inline_command_flags_exist():
    table = _command_flags()
    verbs = {p[0] for p in table if p}
    bad = []
    for d in DOCS:
        for span in re.findall(r"`([^`\n]+)`", _read(d)):
            words = span.replace("\\|", " ").split()  # `series enable\|disable`
            if len(words) > 2 and words[1] in ("enable", "disable"):
                words = [words[0], words[1]] + words[3:]
            if not words or words[0] not in verbs or "--" not in span:
                continue
            path = ()
            for w in words:
                if w.startswith("-"):
                    break
                if path + (w,) in table:
                    path += (w,)
                else:
                    break
            if not path:
                continue
            for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", span):
                if flag not in table[path] and flag != "--help":
                    bad.append(f"{d}: `{span}`: no {flag} on `{' '.join(path)}`")
    assert not bad, "\n".join(bad)
