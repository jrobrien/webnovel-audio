from webnovel_audio.normalize import (
    int_to_words,
    is_scene_break,
    load_blocks_from_text,
    normalize_heading,
    normalize_system,
    normalize_text,
)
from webnovel_audio.segment import split_sentences, split_with_offsets


def test_curly_quotes_flattened():
    assert normalize_text("“Hi,” he said’s") == '"Hi," he said\'s'


def test_lvl_expanded():
    assert "level" in normalize_text("reached Lvl 5").lower()
    assert "lvl" not in normalize_text("reached Lvl 5").lower()


def test_integers_to_words():
    assert normalize_text("the 3 knights") == "the three knights"
    assert normalize_text("1,200 soldiers") == "one thousand two hundred soldiers"


def test_ordinals_to_words():
    assert normalize_text("his 70th level") == "his seventieth level"
    assert normalize_text("the 3rd example") == "the third example"


def test_int_to_words_basic():
    assert int_to_words(0) == "zero"
    assert int_to_words(19) == "nineteen"
    assert int_to_words(21) == "twenty-one"
    assert int_to_words(100) == "one hundred"
    assert int_to_words(1234) == "one thousand two hundred thirty-four"


def test_zero_width_stripped():
    assert normalize_text("wo​rd") == "word"


def test_scene_break_detection():
    assert is_scene_break("***")
    assert is_scene_break("* * *")
    assert is_scene_break("- - -")
    assert not is_scene_break("The end.")


def test_blocks_and_scene_break():
    raw = "Para one.\n\n***\n\nPara two.\n"
    blocks = load_blocks_from_text(raw)
    assert [b.kind for b in blocks] == ["paragraph", "scene_break", "paragraph"]


def test_sentence_split_keeps_ellipsis_and_abbrev():
    text = 'Mr. Egbert waved. "Resk didn’t teach us that…" He smiled.'
    parts = split_sentences(normalize_text(text))
    assert parts[0].startswith("Mr. Egbert waved.")
    assert any(p.endswith("…\"") or p.endswith("…") for p in parts)


def test_split_with_offsets_are_exact():
    s = 'Mara grimaced. Ahh well, worth a shot.'
    parts = split_with_offsets(s)
    for sent, a, b in parts:
        assert s[a:b] == sent
    assert [p[0] for p in parts] == ["Mara grimaced.", "Ahh well, worth a shot."]


def test_decimals_and_symbols():
    assert normalize_text("it hit 2.5 times") == "it hit two point five times"
    assert normalize_text("down 50% now") == "down fifty percent now"
    assert normalize_text("weapons e.g. swords") == "weapons for example swords"
    assert normalize_text("wait . . . what") == "wait … what"
    assert normalize_text("HP is 20 & rising").endswith("and rising")


def test_heading_normalization():
    assert normalize_heading("1- Blade Of Old") == "Chapter One. Blade Of Old"
    assert normalize_heading("Chapter 12: The Return") == "Chapter Twelve. The Return"
    assert normalize_heading("Prologue") == "Prologue"


def test_system_line_normalization():
    out = normalize_system("HP: 40/50  STR: 12  [Skill] -> [Power]")
    assert "hit points" in out
    assert "forty out of fifty" in out
    assert "Strength" in out
    assert "->" not in out and "[" not in out


def test_foreign_scripts_and_inverted_punct_dropped():
    # non-Latin letters can't be spoken; the g2p emits "chinese letter …"
    assert normalize_text("He said 你好 to me, Kevin?") == "He said to me, Kevin?"
    assert normalize_text("The word Привет appeared.") == "The word appeared."
    # inverted ¿ ¡ removed; the trailing ? ! (and its intonation) stay
    assert normalize_text("¡Hola! ¿Cómo estás?") == "Hola! Cómo estás?"
    # accented Latin is kept — espeak reads it in English, no accent switch
    assert normalize_text("Renée met señor Núñez. Nǐ hǎo, Li?") == \
        "Renée met señor Núñez. Nǐ hǎo, Li?"


def test_allcaps_shouting_is_calmed():
    # espeak spells short all-caps tokens as letters (IT -> "eye-tee")
    assert normalize_text('"DAMN IT!"') == '"damn it!"'
    assert normalize_text("Help US, please.") == "Help us, please."
    assert normalize_text("GET OUT OF MY HOUSE.") == "get out of my house."
    assert normalize_text("I said IT, not US.") == "I said it, not us."
    # real acronyms and roman numerals survive (lone, 3+ letters / numeral chars)
    assert normalize_text("The FBI called about a USB drive.") == \
        "The FBI called about a USB drive."
    assert "IV" in normalize_text("Chapter IV: The Return")
    # stat boxes keep their acronyms
    assert "DR" in normalize_system("DR: 5  Block: 10")
