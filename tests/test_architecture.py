"""Site knowledge lives in `providers/` and nowhere else.

Core modules reach a provider through `providers.get` / `providers.resolve`
and the `Provider` interface. If one of them names a site, imports a concrete
provider module, or hard-codes a site's markup, adding the next site means
editing core again -- which is how Royal Road assumptions spread through
sync, textout, feed, config and db before 0.3.
"""
import ast
import os
import re

from webnovel_audio import providers

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "webnovel_audio")
GENERIC = {"base", "local-html"}


def _core_modules():
    for name in sorted(os.listdir(SRC)):
        if name.endswith(".py"):
            yield name, open(os.path.join(SRC, name), encoding="utf-8").read()


def _site_words():
    words = set()
    for p in providers.PROVIDERS:
        if p.name in GENERIC:
            continue
        words.add(p.name)
        words.add(type(p).__module__.rsplit(".", 1)[-1])
    return words


def test_core_never_names_a_site():
    words = _site_words()
    assert words, "no site providers registered?"
    pattern = re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)
    hits = []
    for name, text in _core_modules():
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) or re.search(r"royal\s+road", line, re.I):
                hits.append(f"{name}:{i}: {line.strip()}")
    assert not hits, "site knowledge outside providers/:\n" + "\n".join(hits)


def test_core_imports_only_the_provider_interface():
    allowed = {"webnovel_audio.providers", "webnovel_audio.providers.http",
               "webnovel_audio.providers.session"}
    bad = []
    for name, text in _core_modules():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = ("webnovel_audio." + node.module.lstrip(".")) if node.level else node.module
                if mod.startswith("webnovel_audio.providers") and mod not in allowed:
                    bad.append(f"{name}: from {'.' * node.level}{node.module} import ...")
    assert not bad, "\n".join(bad)


def test_every_provider_has_a_stable_distinct_guid():
    guids = [p.guid for p in providers.PROVIDERS]
    assert len(guids) == len(set(guids))
    # published feeds depend on this exact value
    assert providers.get("royalroad").guid == "rr"
