

def test_handlers_only_read_arguments_their_parser_defines():
    """Catch parser/handler drift statically.

    Every `args.X` a handler reads must be a dest some parser routing to that
    handler actually defines. This class of bug has bitten three times now
    (`--calendar`, `--cookie-header`, and `feed --out-dir` read as `args.out`)
    and only shows up when the command is run for real.
    """
    import argparse
    import ast
    import inspect
    import textwrap

    from webnovel_audio import cli

    parser = cli._build_parser()

    def walk(p, dests=None, inherited=None):
        """yield (handler, {dest names reachable}) for every parser.

        `func` is often set on the *group* parser (`series`) rather than each
        subcommand, so the handler has to be carried down the recursion — a
        child's own get_default("func") is None.
        """
        dests = (dests or set()) | {a.dest for a in p._actions if a.dest != "help"}
        dests |= set(p._defaults)
        fn = p.get_default("func") or inherited
        if fn:
            yield fn, dests
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for child in a.choices.values():
                    yield from walk(child, dests, fn)

    allowed: dict = {}
    for fn, dests in walk(parser):
        allowed.setdefault(fn, set()).update(dests)

    problems = []
    for fn, dests in allowed.items():
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "args" and node.attr not in dests):
                # getattr(args, "x", default) is the guarded form and is fine
                problems.append(f"{fn.__name__} reads args.{node.attr}, "
                                f"not defined by its parser")
    assert not problems, "\n".join(sorted(set(problems)))


def test_outer_flags_are_not_clobbered_by_inner_defaults():
    """`lex -c X list` used to accept X and silently use the default instead.

    argparse parses a subparser into a fresh namespace and copies every value
    it holds onto the parent's, so a concrete default on an inner parser
    overwrites what an outer one already parsed. Both flags live at several
    levels of this tree, so both were affected: `--json` before the subcommand
    printed human text, which the Tcl UI would have failed to parse.
    """
    from webnovel_audio import cli

    ap = cli._build_parser()
    assert ap.parse_args(["lex", "-c", "/tmp/X.toml", "list"]).config == "/tmp/X.toml"
    assert ap.parse_args(["lex", "list", "-c", "/tmp/X.toml"]).config == "/tmp/X.toml"
    # innermost mention still wins when both are given
    assert ap.parse_args(
        ["lex", "-c", "/tmp/A.toml", "list", "-c", "/tmp/B.toml"]).config == "/tmp/B.toml"
    assert ap.parse_args(["lex", "--json", "list"]).json is True
    assert ap.parse_args(["lex", "list", "--json"]).json is True
    # and the fallback still resolves when neither level mentions them
    assert ap.parse_args(["lex", "list"]).config
    assert ap.parse_args(["lex", "list"]).json is False


def test_lex_add_names_every_field():
    """`lex add` took `slug surface respell` positionally while the CSV it writes
    orders its columns differently, so a caller working from the file format
    bound each value one slot off — and the arity matched, so nothing complained.
    Named fields cannot be transposed."""
    import pytest

    from webnovel_audio import cli

    ap = cli._build_parser()
    add = ap.parse_args(["lex", "add", "--scope", "@base",
                         "--surface", "lives", "--respell", "livz", "--pos", "VERB"])
    assert (add.scope, add.surface, add.respell, add.pos) == ("@base", "lives", "livz", "VERB")
    assert add.note == ""

    # the shape the bench models produced is now rejected outright
    for argv in (["lex", "add", "read", "", "red"],
                 ["lex", "add", "_base", "lives", "livz", "--pos", "VERB", "--base"]):
        with pytest.raises(SystemExit) as exc:
            ap.parse_args(argv)
        assert exc.value.code == 2, argv


def test_lex_add_rejects_an_empty_surface():
    """A quoted empty argument is a *present* token: it binds, passes every
    arity check, and used to file a rule under the empty surface that could
    never match. Empty `--respell` stays legal — that is the documented
    'reads fine as-is' no-op."""
    import pytest

    from webnovel_audio import cli

    ap = cli._build_parser()
    with pytest.raises(SystemExit) as exc:
        ap.parse_args(["lex", "add", "--scope", "@base", "--surface", "", "--respell", "x"])
    assert exc.value.code == 2
    ok = ap.parse_args(["lex", "add", "--scope", "@base", "--surface", "inky", "--respell", ""])
    assert ok.respell == ""


def test_lex_rejects_a_scope_that_names_nothing(tmp_path, capsys):
    """An unknown slug used to be accepted silently: the write landed in a
    bundle for a series that does not exist, exit 0."""
    import json

    from webnovel_audio import cli

    cfgp = tmp_path / "cfg.toml"
    cfgp.write_text(
        f'[royalroad]\nstate_db = "{tmp_path / "state.db"}"\n'
        f'library_dir = "{tmp_path / "lib"}"\n')
    rc = cli.main(["lex", "add", "--scope", "not-a-series", "--surface", "x",
                   "--respell", "y", "--json", "--config", str(cfgp)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and out["ok"] is False
    assert out["error"]["code"] == "no_such_scope"
    assert "@base" in out["error"]["reserved"]


def _tracked(tmp_path):
    """A config with one tracked series and a bundle directory on disk."""
    from webnovel_audio.db import DB
    from webnovel_audio.royalroad import FictionInfo

    lib = tmp_path / "lib"
    (lib / "solo").mkdir(parents=True)
    (lib / "solo" / "lexicon.csv").write_text("surface,pos,respell,notes\n")
    db = DB(str(tmp_path / "state.db"))
    db.upsert_series(FictionInfo(rr_id="solo", slug="solo", title="Solo",
                                author="A", url="https://rr/solo"))
    db.con.commit()
    db.close()
    cfgp = tmp_path / "cfg.toml"
    cfgp.write_text(f'[royalroad]\nstate_db = "{tmp_path / "state.db"}"\n'
                    f'library_dir = "{lib}"\n')
    return str(cfgp), lib / "solo"


def test_purge_refuses_without_consent_and_deletes_nothing(tmp_path, capsys, monkeypatch):
    """--purge destroys the rendered audio, the chapter text and the hand-tuned
    lexicon, none of which can be regenerated. It was the only destructive verb
    with no guard at all, while `cache prune` — which deletes segments that can
    simply be re-rendered — had the full set."""
    import json
    import sys

    from webnovel_audio import cli

    cfgp, bundle_dir = _tracked(tmp_path)

    rc = cli.main(["series", "forget", "solo", "--purge", "--json", "--config", cfgp])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and out["error"]["code"] == "needs_confirmation"
    assert bundle_dir.exists()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = cli.main(["series", "forget", "solo", "--purge", "--config", cfgp])
    assert rc == 1 and "needs_confirmation" in capsys.readouterr().err
    assert bundle_dir.exists()


def test_purge_dry_run_reports_without_deleting(tmp_path, capsys):
    import json

    from webnovel_audio import cli

    cfgp, bundle_dir = _tracked(tmp_path)
    rc = cli.main(["series", "forget", "solo", "--purge", "-n", "--json",
                   "--config", cfgp])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["dry_run"] is True
    assert out["would_remove"] == str(bundle_dir)
    assert bundle_dir.exists()
    # still tracked: a dry run must not forget it either
    rc = cli.main(["series", "show", "solo", "--json", "--config", cfgp])
    assert rc == 0


def test_pron_rejects_a_scope_that_names_nothing(tmp_path, capsys):
    """`pron` is the only way to ask what the TTS will actually say, so a wrong
    answer here is worse than anywhere else. An unknown `--series` used to fall
    back to the base lexicon and still print 'with lexicon', so a typo'd slug
    produced a confident preview missing exactly the rules being checked."""
    import json

    from webnovel_audio import cli

    cfgp = tmp_path / "cfg.toml"
    cfgp.write_text(f'[royalroad]\nstate_db = "{tmp_path / "s.db"}"\n'
                    f'library_dir = "{tmp_path / "lib"}"\n')
    rc = cli.main(["pron", "he lives there", "--scope", "nope", "--json",
                   "--config", str(cfgp)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and out["error"]["code"] == "no_such_scope"


def test_pron_refuses_a_flag_combination_that_would_ignore_an_argument():
    """--no-lexicon means 'no rules at all', which makes a scope meaningless;
    --check audits the lexicon and never reads text. Both used to accept the
    now-dead argument silently."""
    import pytest

    from webnovel_audio import cli

    ap = cli._build_parser()
    with pytest.raises(SystemExit) as exc:
        ap.parse_args(["pron", "x", "--scope", "solo", "--no-lexicon"])
    assert exc.value.code == 2

    # --check + text is caught in the handler, where the text is visible
    ns = ap.parse_args(["pron", "ignored", "--check"])
    assert ns.check and ns.text == ["ignored"]


def test_pron_speaks_json(tmp_path, capsys):
    """An agent had to regex the '≈ say' line out of human output; `pron` was
    the one diagnostic verb with no machine-readable form at all."""
    import json

    from webnovel_audio import cli

    base = tmp_path / "_base.csv"
    base.write_text("surface,pos,respell,notes\nqi,,chee,\n")
    cfgp = tmp_path / "cfg.toml"
    cfgp.write_text(f'[general]\nbase_lexicon = "{base}"\n')
    rc = cli.main(["pron", "qi", "--json", "--config", str(cfgp)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["ok"] is True
    assert out["text"] == "qi" and out["changed"] is True
    assert out["applied"] == "chee" and out["say"] and out["applied_say"]


def test_schema_carries_exclusivity_not_just_shapes():
    """Exclusivity constrains a *set* of arguments, so it lives in
    `_mutually_exclusive_groups`, not on any action — a walker that only
    iterates `_actions` reports `--scope` and `--no-lexicon` as freely
    combinable. A schema should describe what makes a call valid, not only
    what parses."""
    import json
    import types

    from webnovel_audio import cli

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._cmd_schema(types.SimpleNamespace())
    d = json.loads(buf.getvalue())
    pron = next(n for n in d["detail"] if n["path"] == ["pron"])
    assert ["no_lexicon", "scope"] in pron["mutually_exclusive"]


def test_every_argument_a_parser_defines_is_read_somewhere():
    """The mirror of the drift test above, and it catches a different bug.

    That test finds a handler reading an argument no parser defines. This finds a
    parser *defining* an argument nothing reads — advertised in --help, accepted
    without complaint, and silently inert. `voices list --json` was exactly that,
    and `--backend` had decayed into it. Being accepted is indistinguishable from
    being honoured, so only a static check finds these.

    Deliberately module-wide rather than per-handler: handlers delegate to
    helpers that take `args` (`_cmd_lex` -> `_lex_promote`), so a per-function
    check reports a dozen false positives while catching nothing extra.
    """
    import argparse
    import ast
    import inspect

    from webnovel_audio import cli

    tree = ast.parse(inspect.getsource(cli))
    read = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "args"):
            read.add(node.attr)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "args"
                and isinstance(node.args[1], ast.Constant)):
            read.add(node.args[1].value)

    defined: dict[str, str] = {}

    def walk(p, path):
        sub = next((a for a in p._actions
                    if isinstance(a, argparse._SubParsersAction)), None)
        if sub:
            for name, child in sub.choices.items():
                walk(child, f"{path} {name}".strip())
        for a in p._actions:
            # --help and --version are handled inside argparse: they print
            # and exit, so no handler ever reads them.
            if isinstance(a, (argparse._SubParsersAction, argparse._HelpAction,
                              argparse._VersionAction)):
                continue
            defined.setdefault(a.dest, f"{path or '(root)'} "
                                       f"{'/'.join(a.option_strings) or '<positional>'}")

    walk(cli._build_parser(), "")
    # `func` is the dispatch target, not an input; `cmd`/`action` name the branch.
    inert = {d: where for d, where in defined.items()
             if d not in read and d not in {"func", "cmd", "action"}}
    assert not inert, "defined but never read: " + "; ".join(
        f"{d} ({w})" for d, w in sorted(inert.items()))


def test_every_argument_has_help_text():
    """An argument with no help is a question the caller cannot answer.

    `-c/--config` shipped bare for a long time, so nothing in `--help` said what
    it was for; `state show key` said only "key". Both were reported as confusing
    by a reader rather than found by a test. Help text is the cheapest part of an
    interface to get right and the easiest to forget on the next command added.
    """
    import argparse

    from webnovel_audio import cli

    missing = []

    def walk(p, path=""):
        sub = next((a for a in p._actions
                    if isinstance(a, argparse._SubParsersAction)), None)
        if sub:
            for name, child in sub.choices.items():
                walk(child, f"{path} {name}".strip())
        for a in p._actions:
            # argparse writes its own help for these two.
            if isinstance(a, (argparse._SubParsersAction, argparse._HelpAction,
                              argparse._VersionAction)):
                continue
            if not a.help:
                missing.append(f"{path or '(root)'}: {a.dest}")

    walk(cli._build_parser())
    assert not missing, "no help text on: " + ", ".join(missing)


def test_the_series_argument_reads_the_same_everywhere():
    """25 commands take the same identifier. Describing it 25 times is how help
    drifts, so they share one definition -- which is also what would make
    renaming it a single edit instead of 25."""
    import argparse

    from webnovel_audio import cli

    helps = set()

    def walk(p):
        sub = next((a for a in p._actions
                    if isinstance(a, argparse._SubParsersAction)), None)
        if sub:
            for child in sub.choices.values():
                walk(child)
        for a in p._actions:
            if a.dest == "key" and not a.option_strings:
                helps.add(a.help)

    walk(cli._build_parser())
    assert helps, "no `key` positionals found -- did the spelling change?"
    # every one names the series and offers the same two ways to identify it
    for h in helps:
        assert h.startswith("the series to"), h
        assert "slug, id, or a title substring" in h, h
