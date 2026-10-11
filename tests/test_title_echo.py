"""The title the author repeats at the top of the text, and a page title that holds
" - " itself, must not be announced twice or split in the wrong place."""
from bs4 import BeautifulSoup

from webnovel_audio.normalize import Block
from webnovel_audio.providers import royalroad
from webnovel_audio.providers.html import drop_title_echo


def P(text):
    return Block("paragraph", text)


def test_number_and_title_echo_are_dropped_even_with_a_typo():
    blocks = [P("Chapter 17"), P("Plans Manfiest(?) (III)"), P("The undelivered things arrived.")]
    out = drop_title_echo(blocks, "Chapter 17 - Plans Manifest(?) (III)")
    assert [b.text for b in out] == ["The undelivered things arrived."]


def test_numbered_title_in_words_or_digits_and_combined_forms():
    for first in ("1 – A New Skin", "1. A New Skin", "Chapter One", "one – A New Skin", "A New Skin", "1"):
        out = drop_title_echo([P(first), P("Story.")], "1. A New Skin")
        assert [b.text for b in out] == ["Story."], first


def test_story_text_is_never_dropped():
    blocks = [P("Chapter 17 was the one I dreaded."), P("The rain came down.")]
    assert drop_title_echo(blocks, "Chapter 17 - Rain") == blocks
    # only leading blocks: the same words later in the text stay
    blocks = [P("The rain came down."), P("Chapter 17")]
    assert drop_title_echo(blocks, "Chapter 17 - Rain") == blocks
    # a long first paragraph that merely starts with the title
    long = P("Rain " + "word " * 40)
    assert drop_title_echo([long], "Rain") == [long]


def test_title_without_a_number():
    out = drop_title_echo([P("Prologue"), P("It began.")], "Prologue")
    assert [b.text for b in out] == ["It began."]
    assert drop_title_echo([P("Prologue")], "") == [P("Prologue")]


def _soup(h1, og):
    return BeautifulSoup(
        f'<h1 class="font-white break-word">{h1}</h1><meta property="og:title" content="{og}"/>', "lxml")


def test_og_title_with_a_dash_in_the_chapter_title_splits_at_the_h1():
    og = "Chapter 17 - Plans Manifest(?) (III) - Cultivation System: Elder Edition"
    assert royalroad._split_og_title(og, _soup("Chapter 17 - Plans Manifest(?) (III)", og)) == (
        "Chapter 17 - Plans Manifest(?) (III)", "Cultivation System: Elder Edition")


def test_og_title_without_an_h1_match_splits_at_the_last_dash():
    og = "1.1.01 - Is That A Status Window? - I Made A Railgun"
    assert royalroad._split_og_title(og, BeautifulSoup("", "lxml")) == (
        "1.1.01 - Is That A Status Window?", "I Made A Railgun")
    # the old, undashed shape still works
    assert royalroad._split_og_title("4. Deck Four - Salvage Run", BeautifulSoup("", "lxml")) == (
        "4. Deck Four", "Salvage Run")
    assert royalroad._split_og_title("Just A Title", BeautifulSoup("", "lxml")) == ("Just A Title", "")
