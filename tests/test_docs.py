import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
_EDGE_LABEL = re.compile(r"(-->|-\.->|==>|---)\s*\|")


def _markdown_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "library", "node_modules")]
        for f in files:
            if f.endswith(".md"):
                yield os.path.join(base, f)


def test_mermaid_diagrams_have_no_edge_labels():
    """GitHub drew the README's diagram as "Could not find a suitable point for the
    given distance": Mermaid positions an edge label from the edge's measured path,
    and that fails (on every version tried) when the diagram is rendered where
    nothing can be measured. Unlabelled edges draw fine, so put the label in a node
    instead:   A -->|yes| B   becomes   A --> y(["yes"]) --> B."""
    bad = []
    for path in _markdown_files():
        in_block = False
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            if line.startswith("```"):
                in_block = line.startswith("```mermaid") if not in_block else False
            elif in_block and _EDGE_LABEL.search(line):
                bad.append(f"{os.path.relpath(path, ROOT)}:{n}: {line.strip()}")
    assert not bad, "\n".join(bad)
