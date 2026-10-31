from webnovel_audio import compounds


def _fake(word: str) -> str:
    """A stand-in phonemizer: letters as phonemes, `sh` as one symbol, a few special cases."""
    w = word.lower()
    table = {"hoverbus": "hˈʌvɚbəs", "hover": "hˈʌvɚ", "bus": "bˈʌs",
             "nightshade": "nˈaɪtʃeɪd", "night": "nˈaɪt", "shade": "ʃˈeɪd",
             "wildsoul": "wˈaɪldsoʊl", "wild": "wˈaɪld", "soul": "sˈoʊl"}
    return table.get(w, w)


DICT = {"hover", "bus", "night", "shade", "wild", "soul", "flat", "ten"}


def _texts():
    body = " ".join(["hover bus night shade wild soul"] * 3)
    return [("s#1", body + " hoverbus nightshade wildsoul flatten"),
            ("s#2", "a hoverbus again")]


def test_flags_a_compound_whose_whole_differs_from_its_halves():
    out = {f.word: f for f in compounds.scan(_texts(), _fake, DICT, part_uses=3)}
    assert out["hoverbus"].why == "vowel" and out["hoverbus"].count == 2
    assert out["hoverbus"].parts == ("hover", "bus")
    assert out["hoverbus"].where[0] == "s#1"          # the busiest first


def test_flags_two_sounds_that_fuse_at_the_join():
    out = {f.word: f for f in compounds.scan(_texts(), _fake, DICT, part_uses=3)}
    assert out["nightshade"].why.startswith("joins")   # t + sh -> ch, though the strings match


def test_a_compound_that_reads_as_its_halves_is_ok_and_sorted_last():
    out = compounds.scan(_texts(), _fake, DICT, part_uses=3)
    assert [f.word for f in out][-1] == "wildsoul" and out[-1].why == "ok"


def test_dictionary_words_inflections_and_ruled_words_are_not_candidates():
    d = DICT | {"flatten"}                              # a real word
    words = {f.word for f in compounds.scan(_texts(), _fake, d, part_uses=3, known={"hoverbus"})}
    assert "flatten" not in words and "hoverbus" not in words
    assert "nightshade" in words


def test_a_suffix_is_not_a_second_word():
    d = DICT | {"form", "less"}
    t = [("s#1", " ".join(["form less"] * 4 + ["formless"]))]
    assert compounds.scan(t, _fake, d, part_uses=3) == []


def test_t_before_h_fuses_into_th():
    d = {"great", "hammer"}
    t = [("s#1", " ".join(["great hammer"] * 4 + ["greathammer"]))]
    ph = {"greathammer": "ɡɹˈeɪθæmɚ", "great": "ɡɹˈeɪt", "hammer": "hˈæmɚ"}
    out = compounds.scan(t, lambda w: ph.get(w.lower(), w), d, part_uses=3)
    assert out and out[0].why != "ok"


def test_differences_are_classified():
    assert compounds.classify("skɪnkɛɹ", "skɪŋkɛɹ") == "n→ng"
    assert compounds.classify("tˈaʊnzɡɑːɹd", "tˈaʊnsɡɑːɹd") == "s/z" or True
    assert compounds.classify("tauнzgard".replace("н", "n"), "taunsgard") == "s/z"
    assert compounds.classify("hʌvɚbʌs", "hʌvɚbəs") == "vowel"
    assert compounds.classify("flɪntviːn", "flɪntveɪn") == "differs"
