import os

import numpy as np

from webnovel_audio.audio import inner_voice
from webnovel_audio.config import Config
from webnovel_audio.ingest import parse_document
from webnovel_audio.segment import build_segments

SYNTHETIC = """
<html><head>
<meta property="og:title" content="3- The Tomb - Test Fiction">
<style>.x7f{display:none} .other , .h9{ visibility : hidden }</style>
</head><body>
<div class="chapter-content">
  <p>He raised the blade. The knight lunged.</p>
  <p class="x7f">Stolen from Amazon, report piracy.</p>
  <p>Mara grimaced. <em>Ahh well, worth a shot.</em></p>
  <p><em>Man, I am not built for this. Can we go back to counters?</em></p>
  <hr/>
  <p><em>"</em>So, pair up, Tovan.</p>
  <blockquote>HP: 40/50  STR: 12</blockquote>
</div>
</body></html>
"""


def _doc():
    return parse_document(SYNTHETIC, url="http://example/x")


def test_metadata_split():
    doc = _doc()
    assert doc.chapter_title == "3- The Tomb"
    assert doc.fiction_title == "Test Fiction"


def test_hidden_node_removed():
    text = " ".join(b.text for b in _doc().blocks)
    assert "piracy" not in text and "Stolen" not in text


def test_scene_break_and_system_blocks():
    kinds = [b.kind for b in _doc().blocks]
    assert "scene_break" in kinds
    assert "system" in kinds


def test_italic_spans_cover_second_sentence_only():
    blocks = {b.text.split(".")[0]: b for b in _doc().blocks if b.kind == "paragraph"}
    grim = next(b for b in _doc().blocks if b.text.startswith("Mara grimaced"))
    # italic starts after "Mara grimaced. "
    assert grim.italic
    start = grim.italic[0][0]
    assert grim.text[start:].startswith("Ahh well")


def test_thought_routing():
    cfg = Config()
    segs = build_segments(_doc().blocks, cfg)
    by_text = {s.text: s for s in segs}
    # whole-paragraph italic -> thought
    assert by_text["Man, I am not built for this."].style == "thought"
    assert by_text["Can we go back to counters?"].style == "thought"
    # partial paragraph: narration then thought
    assert by_text["Mara grimaced."].style == "narration"
    assert by_text["Ahh well, worth a shot."].style == "thought"
    # a lone italic quotation mark must NOT flip the sentence to thought
    ricktus_line = next(s for s in segs if "pair up, Tovan" in s.text)
    assert ricktus_line.style != "thought"
    # system box routed to the UI voice
    assert any(s.style == "system" for s in segs)


def test_inner_voice_is_quieter_and_darker():
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(24000) * 0.2).astype("float32")
    y = inner_voice(x, 24000)

    def hf_fraction(sig):
        mag = np.abs(np.fft.rfft(sig))
        freq = np.fft.rfftfreq(sig.size, 1 / 24000)
        return mag[freq > 5000].sum() / mag.sum()

    assert np.sqrt((y ** 2).mean()) < np.sqrt((x ** 2).mean())
    assert hf_fraction(y) < hf_fraction(x)


def test_real_royalroad_fixture_if_present():
    path = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-ch1.html")
    if not os.path.exists(path):
        return
    doc = parse_document(open(path, encoding="utf-8").read())
    joined = " ".join(b.text for b in doc.blocks)
    assert "lifted from its home" not in joined        # anti-piracy decoy stripped
    kinds = [b.kind for b in doc.blocks]
    assert kinds.count("chat") >= 6 and kinds.count("system") >= 5 and "scene_break" in kinds
    segs = build_segments(doc.blocks, Config())
    thoughts = {s.text for s in segs if s.style == "thought"}
    assert any(t.startswith("Nothing this far out is worth") for t in thoughts)
    assert not any("Corvid Anselm had been dead" in t for t in thoughts)
