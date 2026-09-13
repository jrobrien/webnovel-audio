import importlib.util

import pytest

from webnovel_audio import pipeline
from webnovel_audio.config import Config
from webnovel_audio.lexicon import Lexicon


def test_apply_whole_word_case_sensitive(tmp_path):
    p = tmp_path / "lex.csv"
    p.write_text("surface,respell,ipa,notes\nMontgomery,mahnt-gum-uh-ree,,\n")
    lex = Lexicon.load(str(p))
    assert lex.apply("Montgomery arrived.") == "mahnt-gum-uh-ree arrived."
    assert lex.apply("Montgomery's car") == "mahnt-gum-uh-ree's car"   # 's boundary
    assert lex.apply("montgomeryshire") == "montgomeryshire"          # not whole word
    assert lex.apply("MONTGOMERY") == "MONTGOMERY"                    # case-sensitive


def test_load_many_later_file_wins(tmp_path):
    base = tmp_path / "_base.csv"
    base.write_text("surface,respell,ipa,notes\n"
                    "Montgomery,mahnt-gum-uh-ree,,\nEleanor,ell-uh-nor,,\n")
    series = tmp_path / "s.csv"
    series.write_text("surface,respell,ipa,notes\nEleanor,el-ee-uh-nor,,\nKael,kale,,\n")

    lex = Lexicon.load_many([str(base), str(series)])
    out = lex.apply("Montgomery, Eleanor and Kael")
    assert out == "mahnt-gum-uh-ree, el-ee-uh-nor and kale"          # series overrode Eleanor

    # order matters: base alone keeps its own value
    assert Lexicon.load_many([str(base)]).apply("Eleanor") == "ell-uh-nor"
    assert Lexicon.load_many([str(tmp_path / "missing.csv")]).entries == []


def test_pipeline_stacks_base_under_series(tmp_path):
    base = tmp_path / "_base.csv"
    base.write_text("surface,respell,ipa,notes\nMontgomery,mahnt-gum-uh-ree,,\n")
    series = tmp_path / "series.csv"
    series.write_text("surface,respell,ipa,notes\nMontgomery,monty,,\nBrannoch,bran-ock,,\n")

    cfg = Config()
    cfg.general.base_lexicon = str(base)
    cfg.general.lexicon = ""
    assert pipeline._load_lexicon(cfg).apply("Montgomery met Brannoch") \
        == "mahnt-gum-uh-ree met Brannoch"                            # base only

    cfg.general.lexicon = str(series)
    lex = pipeline._load_lexicon(cfg)
    assert lex.apply("Montgomery met Brannoch") == "monty met bran-ock"   # series wins

    cfg.general.base_lexicon = ""
    cfg.general.lexicon = ""
    assert pipeline._load_lexicon(cfg) is None


def _phonemize(s):
    from kokoro_onnx.tokenizer import Tokenizer
    return Tokenizer().phonemize(s)


@pytest.mark.skipif(importlib.util.find_spec("kokoro_onnx") is None,
                    reason="needs the kokoro extra for the espeak g2p")
def test_shipped_lexicons_are_loadable_and_phonemize():
    """Every shipped lexicon parses, and every respell in it phonemizes without
    error. Cheap guard against a typo that only shows up mid-render."""
    import csv
    import glob
    import os

    seen = 0
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "..",
                                       "data", "lexicons", "*.csv")):
        Lexicon.load(path)                      # parses
        for row in csv.DictReader(open(path, encoding="utf-8")):
            respell = (row.get("respell") or "").strip()
            if respell:
                assert _phonemize(respell), f"{path}: {respell!r} phonemized to nothing"
                seen += 1
    assert seen, "no respellings found to check"


@pytest.mark.skipif(importlib.util.find_spec("kokoro_onnx") is None,
                    reason="needs the kokoro extra for the espeak g2p")
def test_heteronym_respellings_hit_their_target_vowel():
    """The respellings used for live/tear disambiguation must produce the
    intended vowel. Regression for `lyive` (lˈɪɪv, wrong) vs `lyve` (lˈaɪv)."""
    assert "aɪ" in _phonemize("lyve")        # live, as in "live wire"
    assert "ɪ" in _phonemize("liv") and "aɪ" not in _phonemize("liv")   # to live
    assert "ɛ" in _phonemize("tare")         # tear, as in rip
    assert "ɪ" in _phonemize("teer")         # tear, as in crying
    assert _phonemize("lyive") != _phonemize("lyve")     # the actual bug
