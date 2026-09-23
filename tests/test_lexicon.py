"""The unified rule table: phrases, part-of-speech rules and plain rules in one
file, resolved most-specific-first."""
import importlib.util

import pytest

from webnovel_audio import pipeline, tagger
from webnovel_audio.config import Config
from webnovel_audio.lexicon import HEADER, Lexicon, Rule

spacy_only = pytest.mark.skipif(not tagger.available(),
                                reason="needs the spaCy model")


def _lex(*rows: str) -> Lexicon:
    return Lexicon([Rule.parse(*(r.split("|") + [""] * 4)[:4]) for r in rows])


def _write(path, body: str) -> str:
    path.write_text(HEADER + body)
    return str(path)


@pytest.fixture(scope="module")
def nlp():
    n = tagger.load()
    if n is None:
        pytest.skip("needs the spaCy model")
    return n


# --- plain rules -----------------------------------------------------------

def test_whole_word_matching(tmp_path):
    lex = Lexicon.load(_write(tmp_path / "l.csv", "Montgomery,,mahnt-gum-uh-ree,\n"))
    assert lex.apply("Montgomery arrived.") == "Mahnt-gum-uh-ree arrived."
    assert lex.apply("montgomeryshire") == "montgomeryshire"      # not a whole word


def test_matching_is_case_insensitive_and_carries_capitalization(tmp_path):
    """One row per word: `Chi` and `chi` used to need separate rows."""
    lex = Lexicon.load(_write(tmp_path / "l.csv", "chi,,chee,\n"))
    assert lex.apply("the chi flows") == "the chee flows"
    assert lex.apply("Chi flows") == "Chee flows"
    # NOT "CHEE": espeak reads an all-caps token as letter names
    assert lex.apply("CHI flows") == "Chee flows"


def test_blank_respell_is_a_deliberate_no_op(tmp_path):
    """`lex ignore` writes these: 'I listened, it reads fine'."""
    lex = Lexicon.load(_write(tmp_path / "l.csv", "Tian,,,reads fine as-is\n"))
    assert lex.apply("Tian walked") == "Tian walked"
    assert "Tian" in lex.surfaces()          # ...but `check` stops suggesting it


# --- part-of-speech rules --------------------------------------------------

@spacy_only
def test_pos_rule_applies_only_to_that_tag(nlp):
    lex = _lex("live|VERB|liv", "live|ADJ|lyve")
    assert lex.apply("Will he live?", nlp) == "Will he liv?"
    assert lex.apply("a live branch", nlp) == "a lyve branch"


@spacy_only
def test_fine_tag_beats_coarse_pos(nlp):
    lex = _lex("read|VERB|reed", "read|VBD|red")
    assert lex.apply("He read it yesterday.", nlp) == "He red it yesterday."
    assert lex.apply("I like to read.", nlp) == "I like to reed."


@spacy_only
def test_lemma_constraint(nlp):
    """`wound` needs the lemma: both its readings are verbs."""
    lex = _lex("wound|VBD+wind|wownd")
    assert lex.apply("He wound the rope.", nlp) == "He wownd the rope."
    assert lex.apply("The wound bled.", nlp) == "The wound bled."


@spacy_only
def test_plain_rule_is_the_default_reading(nlp):
    """A rule with no `pos` applies whatever the tag — the fallback a
    part-of-speech rule then refines. Pinning only one side is how `live`
    shipped wrong."""
    lex = _lex("live||liv", "live|ADJ|lyve")
    assert lex.apply("Will he live?", nlp) == "Will he liv?"      # falls back
    assert lex.apply("a live branch", nlp) == "a lyve branch"     # ADJ refines


@spacy_only
def test_nominal_fallback(nlp):
    """Attributive nouns get tagged ADJ; abstaining there costs real accuracy."""
    lex = _lex("estimate|NOUN|estimit")
    assert "estimit" in lex.apply("the estimate figures", nlp)


# --- phrases ---------------------------------------------------------------

@spacy_only
def test_phrase_beats_a_single_word_rule(nlp):
    lex = _lex("tear|VERB|tare", "a tear in||a tair in")
    assert lex.apply("a tear in the cloth", nlp) == "a tair in the cloth"


@spacy_only
def test_longest_phrase_wins_and_output_is_not_rematched(nlp):
    lex = _lex("tear||tare", "shed a tear||shed a teer")
    assert lex.apply("He would shed a tear.", nlp) == "He would shed a teer."


# --- layering --------------------------------------------------------------

def test_load_many_later_file_wins(tmp_path):
    base = _write(tmp_path / "_base.csv", "Eleanor,,ell-uh-nor,\nKael,,kale,\n")
    series = _write(tmp_path / "s.csv", "Eleanor,,el-ee-uh-nor,\n")
    lex = Lexicon.load_many([base, series])
    assert lex.apply("Eleanor and Kael") == "El-ee-uh-nor and Kale"
    assert Lexicon.load_many([base]).apply("Eleanor") == "Ell-uh-nor"
    assert Lexicon.load_many([str(tmp_path / "gone.csv")]).rules == []


@spacy_only
def test_a_series_refines_one_pos_without_disturbing_the_rest(nlp, tmp_path):
    """The merge key is (surface, pos), so this is additive — which is the
    whole point of folding the two files together."""
    base = _write(tmp_path / "_base.csv", "live,VERB,liv,\nlive,ADJ,lyve,\n")
    series = _write(tmp_path / "s.csv", "live,ADJ,LYVE,\n")
    lex = Lexicon.load_many([base, series])
    assert lex.apply("a live branch", nlp) == "a LYVE branch"
    assert lex.apply("Will he live?", nlp) == "Will he liv?"      # untouched


def test_pipeline_stacks_base_under_series(tmp_path):
    base = _write(tmp_path / "_base.csv", "Montgomery,,mahnt-gum-uh-ree,\n")
    series = _write(tmp_path / "series.csv", "Montgomery,,monty,\nBrannoch,,bran-ock,\n")
    cfg = Config()
    cfg.general.base_lexicon, cfg.general.lexicon = base, ""
    assert pipeline._load_lexicon(cfg).apply("Montgomery met Brannoch") \
        == "Mahnt-gum-uh-ree met Brannoch"
    cfg.general.lexicon = series
    assert pipeline._load_lexicon(cfg).apply("Montgomery met Brannoch") \
        == "Monty met Bran-ock"
    cfg.general.base_lexicon = cfg.general.lexicon = ""
    assert pipeline._load_lexicon(cfg) is None


# --- file handling ---------------------------------------------------------

def test_comment_lines_are_ignored_everywhere(tmp_path):
    """CSV has no comment convention. The first line matters most: a comment
    there would become the header row and silently void the whole file."""
    p = tmp_path / "l.csv"
    p.write_text("# sky-pride lexicon\n" + HEADER +
                 "# a comment, with a comma\n   # indented\nTian,,tee-en,\n")
    lex = Lexicon.load(str(p))
    assert [r.surface for r in lex.rules] == ["Tian"]
    assert lex.apply("Tian walked") == "Tee-en walked"


def test_empty_surface_row_is_skipped(tmp_path):
    p = _write(tmp_path / "l.csv", ",,,just a note\nKael,,kale,\n")
    assert [r.surface for r in Lexicon.load(p).rules] == ["Kael"]


def test_starter_text_is_named_and_parses_empty(tmp_path):
    p = tmp_path / "l.csv"
    p.write_text(Lexicon.starter_text("sky-pride"))
    assert "sky-pride" in p.read_text().splitlines()[0]
    assert Lexicon.load(str(p)).rules == []


def test_append_candidates_keeps_comments_and_uses_lf(tmp_path):
    p = tmp_path / "l.csv"
    p.write_text(Lexicon.starter_text("sky-pride"))
    assert Lexicon.append_candidates(str(p), ["Tian", "Bai"]) == 2
    raw = p.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode().startswith("# sky-pride")
    assert Lexicon.append_candidates(str(p), ["Tian"]) == 0
    assert {r.surface for r in Lexicon.load(str(p)).rules} == {"Tian", "Bai"}


# --- the shipped file ------------------------------------------------------

def _phonemize(s):
    from kokoro_onnx.tokenizer import Tokenizer
    return Tokenizer().phonemize(s)


needs_espeak = pytest.mark.skipif(importlib.util.find_spec("kokoro_onnx") is None,
                                  reason="needs the kokoro extra for the espeak g2p")


@needs_espeak
def test_shipped_lexicons_parse_and_phonemize():
    import glob
    import os

    seen = 0
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "..",
                                       "data", "lexicons", "*.csv")):
        for r in Lexicon.load(path).rules:
            if r.respell:
                assert _phonemize(r.respell), f"{path}: {r.respell!r} phonemized to nothing"
                seen += 1
    assert seen, "no respellings found to check"


@needs_espeak
def test_respellings_hit_their_target_vowel():
    """Regression for `lyive` (lˈɪɪv, wrong) against `lyve` (lˈaɪv)."""
    assert "aɪ" in _phonemize("lyve")
    assert "ɪ" in _phonemize("liv") and "aɪ" not in _phonemize("liv")
    assert "ɛ" in _phonemize("tare")
    assert "ɪ" in _phonemize("teer")
    assert _phonemize("lyive") != _phonemize("lyve")


def test_pos_rules_resolve_against_the_shipped_file():
    """Every part-of-speech rule in the shipped file is reachable.

    Deliberately NOT "both sides of every heteronym must be pinned": eleven
    rules here are one-sided on purpose, because espeak reads the other side
    correctly and consistently. Whether that is still true is an empirical
    question about real text, which `tools/audit_heteronym_rules.py` answers by
    measuring; a static rule here would only force eleven pointless rows and
    give false confidence.
    """
    import os

    base = os.path.join(os.path.dirname(__file__), "..", "data", "lexicons", "_base.csv")
    lex = Lexicon.load(base)
    assert lex.rules
    for r in lex.rules:
        if r.pos and not r.is_phrase:
            assert r.surface.lower() in lex.tokens
            assert r.specificity >= 1


def test_base_lexicon_loads_from_any_working_directory(tmp_path, monkeypatch):
    """The base lexicon must not depend on where the command was run from.

    `general.base_lexicon` defaults to the repo-relative "data/lexicons/
    _base.csv", and `data/` is not packaged into the wheel, so it names a file
    in the checkout. Resolved against the *working directory* instead, it
    silently vanished for any command run from elsewhere -- and "no lexicon"
    was indistinguishable from "lexicon with nothing to say". A sync run from
    another directory published 23 chapters of one series pronouncing every
    `qi` as "ky", and the only trace was an empty `base_lexicon_sha256` in the
    manifest.
    """
    cfg = Config.load(None)
    monkeypatch.chdir(tmp_path)
    lex = pipeline._load_lexicon(cfg)
    assert lex is not None and lex.rules, \
        "the base lexicon did not load from an unrelated working directory"
    assert any(r.surface.lower() == "qi" for r in lex.rules)


def test_a_configured_but_missing_base_lexicon_is_fatal(tmp_path):
    """Missing must not be silently treated as disabled.

    An empty setting means "render without a base lexicon" and is fine. A
    setting that points at nothing is a broken install or a typo, and
    skipping it produces audio that is wrong in a way no test and no listener
    catches until someone knows the word. Raising is the only outcome that
    gets noticed.
    """
    cfg = Config.load(None)
    cfg.general.base_lexicon = str(tmp_path / "does-not-exist.csv")
    with pytest.raises(SystemExit, match="base lexicon not found"):
        pipeline._load_lexicon(cfg)

    cfg.general.base_lexicon = ""          # explicitly disabled: still allowed
    cfg.general.lexicon = ""
    assert pipeline._load_lexicon(cfg) is None


@spacy_only
def test_punctuation_glued_to_a_token_does_not_defeat_a_rule():
    """A trailing dash must not hide the word in front of it.

    spaCy keeps a trailing hyphen attached when it is used as a dash, so
    "use his qi- a gold shield" tokenises as [..., "qi-", "a", ...] and a
    lookup for the surface "qi" missed entirely. The effect was that one
    sentence said "ky" while the next said "chee" correctly, which reads as
    the lexicon being flaky rather than as a tokenisation bug.

    The dash itself must survive -- only the lookup is trimmed, not the text.
    """
    lex = Lexicon([Rule(surface="qi", respell="chee")])
    nlp = tagger.load(tagger.DEFAULT_MODEL)
    assert lex.apply("use his qi- a gold shield", nlp) == "use his chee- a gold shield"
    assert lex.apply("his qi.", nlp) == "his chee."
    assert lex.apply("(qi)", nlp) == "(chee)"
    # already split by the tagger, and must stay that way
    assert lex.apply("the qi-dense air", nlp) == "the chee-dense air"


def test_trimming_only_touches_the_edges():
    """Internal punctuation is part of the word; the offset moves with the trim.

    Unit-level on purpose. The obvious end-to-end spelling of this -- assert
    a rule for "don't" fires -- tests something else entirely: spaCy splits
    contractions into "do" + "n't", so that surface never reaches the token
    path with or without trimming, and the assertion would have been about
    the tagger rather than about this function.
    """
    from webnovel_audio.lexicon import _trim_edges

    assert _trim_edges("qi-", 8) == ("qi", 8)       # dash after the word
    assert _trim_edges("(qi)", 4) == ("qi", 5)      # offset follows the trim
    assert _trim_edges("qi", 0) == ("qi", 0)        # nothing to do
    assert _trim_edges("n't", 2) == ("n't", 2)      # apostrophe is internal
    assert _trim_edges("--", 0)[0] == ""            # punctuation only: dropped
