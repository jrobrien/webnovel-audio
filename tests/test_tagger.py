"""The tagger module is only spaCy plumbing now — the rules live in lexicon.py."""
import pytest

from webnovel_audio import pipeline, tagger
from webnovel_audio.config import Config


def test_known_models_and_default():
    assert tagger.DEFAULT_MODEL in tagger.KNOWN_MODELS
    assert tagger.DEFAULT_MODEL == "en_core_web_sm"      # sm: 96.2% vs md 97.0%


def test_available_never_raises_on_nonsense():
    assert tagger.available("not_a_real_model_xyz") is False
    assert tagger.load("not_a_real_model_xyz") is None


def test_describe_says_which_state_it_is_in():
    assert "tagger:" in tagger.describe()


def test_rendering_refuses_without_a_tagger(monkeypatch):
    """Tagging is required: espeak decides heteronyms worse than always
    guessing the commoner reading, so failing with a fix beats shipping
    "Will he lyve?" quietly."""
    monkeypatch.setattr(tagger, "load", lambda model=tagger.DEFAULT_MODEL: None)
    with pytest.raises(SystemExit) as exc:
        pipeline._load_nlp(Config())
    assert "tagger install" in str(exc.value)


@pytest.mark.skipif(not tagger.available(), reason="needs the spaCy model")
def test_loaded_pipeline_tags_and_lemmatizes():
    nlp = tagger.load()
    tags = {t.text: (t.pos_, t.tag_, t.lemma_) for t in nlp("He wound the rope.")}
    assert tags["wound"][0] == "VERB"
    assert tags["wound"][1] == "VBD"
    assert tags["wound"][2] == "wind"          # the lemmatizer is kept for this


@pytest.mark.skipif(not tagger.available(), reason="needs the spaCy model")
def test_parser_and_ner_are_excluded():
    """They are most of the runtime and neither is used."""
    nlp = tagger.load()
    assert "parser" not in nlp.pipe_names
    assert "ner" not in nlp.pipe_names
    assert "tagger" in nlp.pipe_names


def test_known_models_are_installable_and_ordered_smallest_first():
    """Every listed model must be one `tagger install` can actually fetch —
    the wheel URL is built by pattern, so a name that does not follow it
    would only fail at download time, after the user waited."""
    from webnovel_audio import tagger
    from webnovel_audio.cli import _model_wheel

    assert tagger.KNOWN_MODELS == ("en_core_web_sm", "en_core_web_md",
                                   "en_core_web_lg")
    for model in tagger.KNOWN_MODELS:
        url = _model_wheel(model)
        assert url.startswith("https://github.com/explosion/spacy-models/")
        assert f"/{model}-" in url and url.endswith("-py3-none-any.whl")
