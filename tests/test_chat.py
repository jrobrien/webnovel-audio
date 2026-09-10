import os

from webnovel_audio.config import Config
from webnovel_audio.ingest import _parse_chat, parse_document
from webnovel_audio.normalize import normalize_chat_message, normalize_username
from webnovel_audio.segment import build_segments


def test_parse_chat_variants():
    a = _parse_chat("[Noobkiller9000 (Earth): First Wick and now Bourne.]")
    assert a == {"user": "Noobkiller9000", "location": "Earth",
                 "message": "First Wick and now Bourne."}
    b = _parse_chat("[Prestigious3horns (Vinox 2): Wick and Bourne?]")
    assert b["location"] == "Vinox 2"
    c = _parse_chat("[[StealthAssassin9 (Earth): Ahhh, it muted another name again.]")
    assert c["user"] == "StealthAssassin9"
    d = _parse_chat("[Justiceistruth: (Earth): We want justice.]")
    assert d["user"] == "Justiceistruth" and d["location"] == "Earth"


def test_parse_chat_rejects_system_lines():
    assert _parse_chat("[Affirmative. Scanning…]") is None
    assert _parse_chat("[Target: Recover the evidence, complete.]") is None
    assert _parse_chat("[This device does not seem to have wireless access.]") is None


def test_username_speakable():
    assert normalize_username("Forever1stCommenter") == "Forever first Commenter"
    assert normalize_username("Noobkiller9000") == "Noobkiller nine thousand"
    assert normalize_username("StealthAssassin9") == "Stealth Assassin nine"


def test_chat_message_normalization():
    # shouting is calmed to normal case; dampen_caps=False keeps it verbatim
    assert normalize_chat_message("THAT'S STILL SO SCIFI.") == "That's Still so Scifi."
    assert "SCIFI" in normalize_chat_message("THAT'S STILL SO SCIFI.", dampen_caps=False)
    assert "@" not in normalize_chat_message("You were saying @1000years of death.")


def test_build_segments_chat_routing():
    cfg = Config()
    blocks = parse_document(_SYNTH).blocks
    segs = build_segments(blocks, cfg)
    styles = [s.style for s in segs]
    kinds = [s.kind for s in segs]
    assert "chat" in styles
    assert "cue" in kinds                       # earcon before the run
    assert styles.count("cue" if False else "chat") >= 3

    chat_voices = {s.speaker: s.voice for s in segs if s.style == "chat" and s.speaker}
    # a two-way exchange must not share a voice
    assert chat_voices["Noobkiller9000"] != chat_voices["Prestigious3horns"]
    # username announced once (speak_username="first"), then bare messages
    lead_lines = [s.text for s in segs if s.style == "chat" and s.speaker == "Noobkiller9000"]
    assert lead_lines[0] == "Noobkiller nine thousand"


def test_chat_earcon_only_at_run_start():
    cfg = Config()
    segs = build_segments(parse_document(_SYNTH).blocks, cfg)
    cue_idx = [i for i, s in enumerate(segs) if s.kind == "cue"]
    # two runs in the fixture -> exactly two earcons
    assert len(cue_idx) == 2


def test_real_fixture_chat_and_system():
    path = os.path.join(os.path.dirname(__file__), "..", "samples", "salvage-run-ch1.html")
    if not os.path.exists(path):
        return
    doc = parse_document(open(path, encoding="utf-8").read())
    kinds = [b.kind for b in doc.blocks]
    assert kinds.count("chat") >= 6            # [Handle (Loc): ...] livestream lines
    assert kinds.count("system") >= 5          # [bracketed] ship-AI lines + stat block
    joined = " ".join(b.text for b in doc.blocks)
    assert "lifted from its home" not in joined  # decoy stripped
    segs = build_segments(doc.blocks, Config())
    assert any(s.style == "chat" for s in segs)
    assert any(s.kind == "cue" for s in segs)


_SYNTH = """<html><head><meta property="og:title" content="Ch 1 - Test"></head><body>
<div class="chapter-content">
<p>Adam opened the box.</p>
<p>[Forever1stCommenter (Earth): Whistle emoji.]</p>
<p>[Noobkiller9000 (Earth): First Wick and now Bourne.]</p>
<p>[Prestigious3horns (Vinox 2): Wick and Bourne?]</p>
<p>He picked up the laptop.</p>
<p>"Ah, here we go."</p>
<p>[AlamoMatador4thewin (Earth): No way he did it?]</p>
<p>[1000yearsofdeath (Earth): The laptop is password-protected.]</p>
<p>[Scan complete. Evidence located.]</p>
</div></body></html>"""
