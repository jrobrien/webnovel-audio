import types

from webnovel_audio import cli
from webnovel_audio.audio import _ffmeta_chapters


def test_ffmeta_chapters():
    doc = _ffmeta_chapters([(0.0, "am_michael"), (15.5, "af_heart")], total_s=30.0)
    assert doc.startswith(";FFMETADATA1\n")
    assert doc.count("[CHAPTER]") == 2
    assert "START=0\nEND=15500" in doc
    assert "START=15500\nEND=30000" in doc
    assert "title=am_michael" in doc and "title=af_heart" in doc
    # '=' / newlines in a title would corrupt the ffmetadata format
    assert "title=a-b" in _ffmeta_chapters([(0.0, "a=b")], 1.0)
    # unordered input is sorted by start time
    d2 = _ffmeta_chapters([(9.0, "b"), (1.0, "a")], 10.0)
    assert d2.index("title=a") < d2.index("title=b")


def test_voice_label():
    assert cli._voice_label("am_michael") == "American male. Michael."
    assert cli._voice_label("bf_lily") == "British female. Lily."
    assert cli._voice_label("xx_weird").startswith("xx.")


def test_voices_listing(capsys):
    rc = cli._cmd_voices(types.SimpleNamespace(
        demo=False, only=None, out="x.opus", text=None, pause=1200,
        announcer="am_michael", config="config.toml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "American male" in out and "am_michael" in out
    assert "British female" in out
    assert "voices --demo" in out


def test_voices_listing_subset(capsys):
    cli._cmd_voices(types.SimpleNamespace(
        demo=False, only="am_michael,bf_emma", out="x.opus", text=None,
        pause=1200, announcer="am_michael", config="config.toml"))
    out = capsys.readouterr().out
    assert "am_michael" in out and "bf_emma" in out
    assert "af_heart" not in out
