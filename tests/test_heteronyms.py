from webnovel_audio.heteronyms import find_heteronyms


def test_finds_known_heteronyms_with_context():
    text = "She wiped away a tear before the storm, then watched the wind pick up."
    out = find_heteronyms(text, context=3)
    words = {w for w, _, _ in out}
    assert "tear" in words and "wind" in words


def test_counts_and_orders_by_frequency():
    text = "bow bow bow tear"
    out = find_heteronyms(text)
    assert out[0][0] == "bow" and out[0][1] == 3
    assert out[1][0] == "tear" and out[1][1] == 1


def test_case_insensitive_and_punctuation_stripped():
    out = find_heteronyms('"Tear," she said, wiping a Tear.')
    assert out and out[0][0] == "tear" and out[0][1] == 2


def test_ignores_unlisted_words():
    assert find_heteronyms("The quick brown fox jumps over the lazy dog.") == []


def test_context_window_size():
    text = " ".join(f"w{i}" for i in range(20))
    text = text.replace("w10", "tear")
    out = find_heteronyms(text, context=2)
    assert out[0][2] == "w8 w9 tear w11 w12"
