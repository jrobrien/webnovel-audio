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
