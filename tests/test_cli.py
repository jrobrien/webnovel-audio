

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
