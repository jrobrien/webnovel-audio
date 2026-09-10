import importlib.util
import types

import pytest

from webnovel_audio import cli


def test_translit_single_pass_no_cascade():
    # each IPA symbol maps once; outputs must not be re-translated
    # ("ah" must not become "ahh", "ee" not "eheh", ...)
    assert cli._translit("ɑː") == "ah"
    assert cli._translit("iː") == "ee"
    assert cli._translit("ʌ") == "uh"
    assert cli._translit("ɡɹˈæm") == "grˈam"          # stress mark passes through here
    assert cli._translit("mɑːntɡ") == "mahntg"
    assert "ahh" not in cli._translit("mɑːntɡˈɑːmɚɹi")


def test_gloss_stress_and_onset():
    # one primary stress -> that syllable upcased; vowel-less onset glued on
    assert cli._gloss("ɡɹˈæm") == "GRAM"
    assert cli._gloss("mɑːntɡˈɑːmɚɹi") == "mahntg-AHMURREE"
    assert cli._gloss("dʒˈoʊkwɪn") == "JOHKWIHN"
    # multiple primary stresses (hyphenated respell) -> no upcase, still readable
    assert cli._gloss("mˈɑːntɡˈʌmˈʌɹˈiː") == "mahntg-uhm-uhr-ee"
    # whitespace separates words
    assert cli._gloss("mˈɛt ɡɹˈæm") == "MEHT GRAM"


@pytest.mark.skipif(importlib.util.find_spec("kokoro_onnx") is None,
                    reason="needs the kokoro extra for the espeak g2p")
def test_pron_command_shows_before_and_after(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[general]\nbase_lexicon = "%s"\n'
                   % (tmp_path / "_base.csv"))
    (tmp_path / "_base.csv").write_text("surface,respell,ipa,notes\nGraham,gram,,\n")

    rc = cli._cmd_pron(types.SimpleNamespace(
        text=["Graham"], config=str(cfg), series=None, no_lexicon=False, check=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "phonemes" in out and "with lexicon : gram" in out
    assert "GRAM" in out                                  # the after-lexicon gloss
