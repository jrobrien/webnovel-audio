import os

import numpy as np

from webnovel_audio.audio import apply_chain, pitch_shift
from webnovel_audio.config import Config
from webnovel_audio.dialogue import Attributor, split_paragraph
from webnovel_audio.providers.royalroad import parse_chapter
from webnovel_audio.normalize import Block
from webnovel_audio.segment import build_segments


def _cfg(**cast):
    c = Config()
    for k, v in cast.items():
        setattr(c.cast, k, v)
    return c


def _speakers(text, cfg=None):
    cfg = cfg or _cfg()
    attr = Attributor(cfg)
    out = []
    for para in text.split("\n\n"):
        for ln in split_paragraph(Block("paragraph", para.strip()), attr, cfg):
            if ln.kind == "dialogue":
                out.append((ln.speaker, ln.text))
    return out


def test_explicit_tag_after_and_before():
    got = _speakers('"Hello there," said Resk.\n\nTovan said, "Get up."')
    assert got[0][0] == "Resk"
    assert got[1][0] == "Tovan"


def test_pronoun_tag_resolves_by_gender():
    text = (
        "Mara wiped his brow and sighed.\n\n"
        '"That is enough for today," he said.'
    )
    got = _speakers(text)
    assert got[0][0] == "Mara"  # "he" -> most recent male subject


def test_untagged_continuation_is_sticky():
    text = (
        '"Alright, lads," Resk began.\n\n'
        '"Pair up and spar. No slacking this time."'
    )
    got = _speakers(text)
    assert got[0][0] == "Resk"
    assert got[1][0] == "Resk"  # no tag -> same speaker


def test_descriptive_referent_becomes_speaker_key():
    text = 'The wrinkled crone smiled. "You have the gift, boy."'
    got = _speakers(text)
    assert got[0][0] == "crone"


def test_narration_then_dialogue_split_in_one_paragraph():
    cfg = _cfg(protagonist="Mara", voices={"Mara": "am_michael"})
    attr = Attributor(cfg)
    lines = split_paragraph(
        Block("paragraph", 'Mara looked at the sword. "Not today," he muttered. The cold lingered.'),
        attr, cfg,
    )
    kinds = [(ln.kind, ln.speaker) for ln in lines]
    assert kinds[0][0] == "narration"
    assert ("dialogue", "Mara") in kinds
    assert kinds[-1][0] == "narration"


def test_build_segments_assigns_cast_voices():
    cfg = _cfg(protagonist="Mara",
               voices={"Mara": "am_michael", "Resk": "bm_lewis"})
    blocks = [
        Block("paragraph", '"Where were you?" Resk asked.'),
        Block("paragraph", '"Training," Mara said.'),
    ]
    segs = build_segments(blocks, cfg)
    by_speaker = {s.speaker: s.voice for s in segs if s.style == "dialogue"}
    assert by_speaker["Resk"] == "bm_lewis"
    assert by_speaker["Mara"] == "am_michael"


def test_heading_segment_gets_terminal_punctuation():
    # an unterminated line makes Kokoro clip the last word ("...Dead Air")
    cfg = Config()
    blocks = [Block("heading", "1. Dead Air", meta={"fiction": "Salvage Run"})]
    seg = next(s for s in build_segments(blocks, cfg) if s.style == "heading")
    assert seg.text == "Salvage Run. Chapter One. Dead Air."
    # a heading that already ends in punctuation is left alone
    blocks = [Block("heading", "Prologue?", meta={})]
    seg = next(s for s in build_segments(blocks, cfg) if s.style == "heading")
    assert seg.text.endswith("?") and not seg.text.endswith("?.")


def test_pitch_shift_keeps_length_changes_content():
    x = np.sin(2 * np.pi * 220 * np.arange(24000) / 24000).astype("float32")
    y = pitch_shift(x, 24000, -2.0)
    assert y.shape == x.shape
    assert not np.allclose(y, x)


def test_apply_chain_gain_only():
    x = np.ones(4000, dtype="float32") * 0.5
    y = apply_chain(x, 24000, {"gain_db": -6.0})
    assert abs(float(np.mean(np.abs(y))) - 0.25) < 0.02


def test_real_fixture_cast_if_present():
    path = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-ch1.html")
    if not os.path.exists(path):
        return
    cfg = _cfg(protagonist="Mara",
               voices={"Mara": "af_heart", "Resk": "am_michael"})
    doc = parse_chapter(open(path, encoding="utf-8").read())
    segs = build_segments(doc.blocks, cfg)
    dlg = [(s.speaker, s.text) for s in segs if s.style == "dialogue"]
    speakers = {sp for sp, _ in dlg}
    assert {"Mara", "Resk"} <= speakers
    # explicit tag: '"Depth check," Resk said ...'
    assert any(sp == "Resk" and t.startswith("Depth check") for sp, t in dlg)
    # pronoun tag: '"...," she said' -> Mara
    assert any(sp == "Mara" and "The ship still has a voice" in t for sp, t in dlg)


def test_unclear_gender_is_left_unassigned():
    """A speaker with no pronoun signal (descriptive referents, honorifics) gets
    an empty voice, not a coin flip — a wrong guess reads as a decision, an
    empty value reads as unfinished."""
    from webnovel_audio.dialogue import suggest_voices

    cfg = Config()
    out = suggest_voices({"Mara": 9, "girl": 5, "Resk": 3},
                         {"Mara": "f", "Resk": "m"}, cfg)
    assert out["Mara"].startswith(("af_", "bf_"))
    assert out["Resk"].startswith(("am_", "bm_"))
    assert out["girl"] == ""                       # listed, deliberately unassigned


def test_unassigned_voice_falls_back_to_default():
    from webnovel_audio.dialogue import suggest_voices

    cfg = _cfg(voices={"crone": ""}, default="am_onyx")
    blocks = [Block("paragraph", 'The wrinkled crone smiled. "You have the gift, boy."')]
    segs = {s.speaker: s.voice for s in build_segments(blocks, cfg)
            if s.style == "dialogue"}
    assert segs["crone"] == "am_onyx"              # empty -> [cast] default, not ""
    # an unassigned entry must never be counted as a voice already in use
    nxt = suggest_voices({"other": 2}, {"other": "f"}, cfg)
    assert nxt["other"].startswith(("af_", "bf_"))


# -- machine voice (`//…//`) -------------------------------------------------

def _machine_cfg():
    c = Config()
    c.synth.machine_marker = "//"
    c.voices.machine = "af_kore"
    return c


def _styles(blocks, cfg):
    return [(s.style, s.text) for s in build_segments(blocks, cfg) if s.kind == "speech"]


def test_machine_marker_inline_and_single_paragraph():
    blocks = [
        Block("paragraph", "Evie answered. //Paul Dorgan, alone.//"),
        Block("paragraph", "//Awaiting input…//", italic=[(0, 19)]),
    ]
    got = _styles(blocks, _machine_cfg())
    assert got == [("narration", "Evie answered."), ("machine", "Paul Dorgan, alone."),
                   ("machine", "Awaiting input…")]
    assert all("/" not in t for _, t in got)


def test_machine_run_spans_blocks_and_closes():
    blocks = [
        Block("paragraph", "//Status:", italic=[(0, 9)]),
        Block("system", "Aura Pool: 5/5"),
        Block("paragraph", "End Report//.", italic=[(0, 13)]),
        Block("paragraph", "He blinked."),
    ]
    got = _styles(blocks, _machine_cfg())
    assert [s for s, _ in got] == ["machine", "machine", "machine", "narration"]
    assert got[0][1] == "Status."


def test_unclosed_machine_run_ends_at_plain_narration():
    blocks = [
        Block("paragraph", "//Brawler selected.", italic=[(0, 19)]),
        Block("paragraph", "Notes:", italic=[(0, 6)]),
        Block("paragraph", "//Awaiting directive…//", italic=[(0, 23)]),
        Block("paragraph", "He cleared his throat."),
    ]
    got = _styles(blocks, _machine_cfg())
    assert [s for s, _ in got] == ["machine", "machine", "machine", "narration"]


def test_machine_marker_off_by_default_and_spares_urls():
    blocks = [Block("paragraph", "//Awaiting input.//")]
    assert _styles(blocks, Config())[0][0] == "narration"
    got = _styles([Block("paragraph", "See https://example.com now.")], _machine_cfg())
    assert got[0][0] == "narration"


def test_robot_chain_keeps_length_and_level():
    sr = 24000
    t = np.arange(sr) / sr
    x = (0.3 * np.sin(2 * np.pi * 220 * t * (1 + 0.2 * t))).astype("float32")
    y = apply_chain(x, sr, Config().dsp["machine"])
    assert y.shape == x.shape and np.isfinite(y).all()
    assert 0.3 < np.sqrt(np.mean(y * y)) / np.sqrt(np.mean(x * x)) < 1.5
