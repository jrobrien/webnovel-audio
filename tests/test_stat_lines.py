"""Stat readouts written one `[Label: value]` paragraph at a time (not a block in the
source), `???` as an unknown value, and `[...]` dividers."""
from webnovel_audio.config import Config
from webnovel_audio.normalize import normalize_system, normalize_text, normalize_username
from webnovel_audio.providers.html import blocks_from_container, soup_of
from webnovel_audio.segment import build_segments


def _blocks(*paras):
    html = "<div>" + "".join(f"<p>{p}</p>" for p in paras) + "</div>"
    return blocks_from_container(soup_of(html).div)


def _kinds(blocks):
    return [b.kind for b in blocks]


READOUT = ["[Cultivation Creation System is initialized!]", "[Host: Lu Qi]",
           "[Cultivation Realm: Spirit Manifestation Realm]", "[Quest: A New Path!]",
           "[Reward: 1 creation point]", "[...]", "[Creation Points: 0]"]


def test_single_word_labels_inside_a_readout_are_system_lines():
    blocks = _blocks(*READOUT)
    assert _kinds(blocks) == ["system"] * len(READOUT)
    assert blocks[1].text == "Host: Lu Qi" and blocks[4].text == "Reward: 1 creation point"


def test_a_chat_stream_is_still_chat():
    blocks = _blocks("[Sylvia: first!]", "[Mark: lol no way]", "[Dana (Texas): hello]")
    assert _kinds(blocks) == ["chat"] * 3


def test_one_system_line_among_chat_does_not_flip_the_viewers():
    blocks = _blocks("[Sylvia: first!]", "[Mark: lol]", "[Stream paused for ads]", "[Dana: back]")
    assert _kinds(blocks) == ["chat", "chat", "system", "chat"]


def test_a_readout_next_to_prose_is_still_a_run():
    blocks = _blocks("He looked up.", *READOUT[:4], "And that was all.")
    assert _kinds(blocks) == ["paragraph", *["system"] * 4, "paragraph"]


def test_capitalised_labels_are_not_viewers():
    blocks = _blocks("[NAME: (Please name the art)]", "[TYPE: Special Art (+25 CP)]")
    assert _kinds(blocks) == ["system", "system"]


def test_unknown_values():
    assert normalize_system("Talent: Jade(+) (Mortal -> Earth -> Jade -> ???)") == \
        "Talent: Jade plus (Mortal to Earth to Jade to unknown)"
    assert normalize_system("Traits (Common, Rare, ???)") == "Traits (Common, Rare, unknown)"
    assert normalize_system("Reincarnator (???) -- the body").startswith("Reincarnator (unknown)")
    assert normalize_system("??? mastery allows") == "unknown mastery allows"
    # not a stat value
    assert normalize_system("What???") == "What???"
    assert "unknown" not in normalize_text("what ???")      # prose and chat keep their marks
    assert normalize_username("???") == "unknown"


def test_stat_symbols_are_spoken():
    assert normalize_system("Progress: 1/100%") == "Progress: one out of one hundred percent"
    assert normalize_system("COST: 25+5 = 30 CP") == "COST: twenty-five plus five equals thirty CP"
    assert normalize_text("Qi Condensation -> Foundation Establishment") == \
        "Qi Condensation to Foundation Establishment"


def test_an_ellipsis_only_line_is_a_pause_not_speech():
    segs = build_segments(_blocks("[Creation Points: 0]", "[...]", "[Quest: A New Path!]"), Config())
    kinds = [(s.kind, s.text) for s in segs]
    assert [k for k, _ in kinds] == ["speech", "pause", "speech"]
    assert segs[1].pause_after_ms >= Config().pauses.ellipsis_extra_ms
    # an ellipsis inside a sentence is untouched
    segs = build_segments(_blocks("Wait... what?"), Config())
    assert any("…" in s.text for s in segs if s.kind == "speech")
