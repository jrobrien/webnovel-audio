"""Segment-cache status and compaction."""
import json
import os

import numpy as np
import pytest
import soundfile as sf

from webnovel_audio import bundle, cache, pipeline
from webnovel_audio.config import Config
from webnovel_audio.db import DB
from webnovel_audio.segment import Segment

from test_bundle import _Fic  # noqa: F401  (shared fixture helpers)


@pytest.fixture
def lib(tmp_path):
    cfg = Config()
    cfg.royalroad.state_db = str(tmp_path / "state.db")
    cfg.royalroad.library_dir = str(tmp_path / "library")
    cfg.general.cache_dir = str(tmp_path / "shared-cache")
    cfg.synth.backend = "null"
    db = DB(cfg.royalroad.state_db)
    fi = _Fic()
    sid = db.upsert_series(fi)
    db.replace_chapters(sid, fi.chapters)
    d = os.path.join(cfg.royalroad.library_dir, fi.slug)
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    db.set_bundle(sid, d, db.get_series("999")["uuid"])
    yield cfg, db, db.get_series("999"), d
    db.close()


def _seg(text="hello there", voice="am_michael"):
    return Segment(text=text, voice=voice)


def _script(d, name, segs):
    json.dump([{"kind": "speech", "text": s.text, "voice": s.voice,
                "style": s.style, "rate": s.rate, "pitch": s.pitch} for s in segs],
              open(os.path.join(d, "chapters", name), "w"))


def _write_wav(root, backend, seg, sr, gen="", seconds=0.5):
    p = pipeline._cache_path(root, backend, seg, sr, gen)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    p = p.replace(pipeline.CACHE_EXT, ".wav")
    sf.write(p, np.linspace(-0.5, 0.5, int(sr * seconds), dtype="float32"),
             sr, subtype="FLOAT")
    return p


# -- fingerprint -----------------------------------------------------------

def test_null_backend_fingerprint_is_stable_and_distinct():
    cfg = Config()
    cfg.synth.sample_rate = 24000
    assert cache.current_fingerprint(cfg, "null") == "null-24000-2.7"
    cfg.synth.sample_rate = 22050
    assert cache.current_fingerprint(cfg, "null") != "null-24000-2.7"


@pytest.mark.skipif(not os.path.exists(
    os.path.expanduser("~/.cache/webnovel-audio/kokoro-v1.0.onnx")),
    reason="needs the Kokoro model files")
def test_kokoro_fingerprint_covers_the_g2p_chain():
    """The cache key misses model bytes, voices, lang and the g2p toolchain.
    An espeak bump must change the generation — that is the whole point."""
    from webnovel_audio.synth import kokoro
    mat = kokoro.fingerprint_material()
    assert len(mat["model"]) == 16 and len(mat["voices"]) == 16
    assert mat["lang"] == "en-us"
    assert mat["espeakng-loader"] and mat["phonemizer"]
    fp = kokoro.fingerprint()
    assert len(fp) == 12 and fp == kokoro.fingerprint()      # stable
    assert kokoro.fingerprint(lang="en-gb") != fp            # lang matters
    # onnxruntime is deliberately excluded: numerics, not semantics
    assert "onnxruntime" not in mat


def test_cache_path_puts_segments_under_a_generation(tmp_path):
    seg = _seg()
    flat = pipeline._cache_path(str(tmp_path), "kokoro", seg, 24000)
    gen = pipeline._cache_path(str(tmp_path), "kokoro", seg, 24000, "abc123")
    assert flat.endswith(pipeline.CACHE_EXT)
    assert os.path.dirname(gen).endswith(os.path.join("kokoro", "abc123"))
    # the key itself is unchanged by the generation — no re-synthesis
    assert os.path.basename(flat) == os.path.basename(gen)


def test_cache_lookup_falls_back_to_the_pre_compact_layout(tmp_path):
    seg = _seg()
    root = str(tmp_path)
    assert pipeline._cache_lookup(root, "kokoro", seg, 24000, "gen1") is None
    old = _write_wav(root, "kokoro", seg, 24000)              # flat float32 wav
    assert pipeline._cache_lookup(root, "kokoro", seg, 24000, "gen1") == old
    new = pipeline._cache_path(root, "kokoro", seg, 24000, "gen1")
    os.makedirs(os.path.dirname(new), exist_ok=True)
    sf.write(new, np.zeros(10, dtype="float32"), 24000, subtype=pipeline.CACHE_SUBTYPE)
    # current generation wins once it exists
    assert pipeline._cache_lookup(root, "kokoro", seg, 24000, "gen1") == new


# -- live keys / status ----------------------------------------------------

def test_live_keys_are_per_series(lib):
    cfg, db, s, d = lib
    segs = [_seg("one"), _seg("two")]
    _script(d, "001-a.segments.json", segs)
    live = cache.live_keys(cfg, db)
    assert len(live) == 2
    assert all(owners == {"test-series"} for owners in live.values())
    assert pipeline.cache_digest(segs[0], cfg.synth.sample_rate) in live


def test_status_counts_and_flags_unreachable(lib):
    cfg, db, s, d = lib
    sr = cfg.synth.sample_rate
    keep, drop = _seg("kept"), _seg("dropped")
    _script(d, "001-a.segments.json", [keep])
    root = os.path.join(d, ".cache")
    _write_wav(root, "null", keep, sr)
    _write_wav(root, "null", drop, sr)

    st = cache.status(cfg, db)
    assert st["files"] == 2
    assert st["format"]["wav"] == 2 and st["format"]["flac"] == 0
    row = [r for r in st["series"] if r["slug"] == "test-series"][0]
    assert row["files"] == 2 and row["reclaimable"] == 1
    assert "(unmigrated)" in st["generations"]


def test_status_rejects_an_unknown_series(lib):
    cfg, db, s, d = lib
    with pytest.raises(bundle.BundleError) as exc:
        cache.status(cfg, db, "nope")
    assert exc.value.code == "no_such_series"


# -- compact ---------------------------------------------------------------

def test_compact_converts_to_flac_under_the_current_generation(lib):
    cfg, db, s, d = lib
    sr = cfg.synth.sample_rate
    seg = _seg("hello")
    _script(d, "001-a.segments.json", [seg])
    root = os.path.join(d, ".cache")
    old = _write_wav(root, "null", seg, sr, seconds=2.0)
    before = os.path.getsize(old)

    r = cache.compact(cfg, db, log=lambda *_: None)
    assert r["converted"] == 1 and r["failed"] == 0
    assert not os.path.exists(old)

    gen = cache.current_fingerprint(cfg)
    new = pipeline._cache_path(root, "null", seg, sr, gen)
    assert os.path.isfile(new)
    assert os.path.getsize(new) < before * 0.6          # measured ~29%
    assert r["bytes_after"] < r["bytes_before"]


def test_compact_preserves_the_audio_within_16_bit(lib):
    """The key is unchanged and the audio must still be the same sound —
    PCM_16's -96 dBFS floor is far below the Opus encode downstream."""
    cfg, db, s, d = lib
    sr = cfg.synth.sample_rate
    seg = _seg("hello")
    _script(d, "001-a.segments.json", [seg])
    root = os.path.join(d, ".cache")
    old = _write_wav(root, "null", seg, sr, seconds=1.0)
    original, _ = sf.read(old, dtype="float32")

    cache.compact(cfg, db, log=lambda *_: None)
    found = pipeline._cache_lookup(root, "null", seg, sr,
                                   cache.current_fingerprint(cfg))
    back, _ = sf.read(found, dtype="float32")
    assert len(back) == len(original)
    assert np.abs(back - original).max() < 2e-5        # ~-96 dBFS


def test_compact_is_idempotent(lib):
    cfg, db, s, d = lib
    seg = _seg("hello")
    _script(d, "001-a.segments.json", [seg])
    _write_wav(os.path.join(d, ".cache"), "null", seg, cfg.synth.sample_rate)
    first = cache.compact(cfg, db, log=lambda *_: None)
    second = cache.compact(cfg, db, log=lambda *_: None)
    assert first["converted"] == 1
    assert second["converted"] == 0 and second["skipped"] == 1


def test_compact_dry_run_writes_nothing(lib):
    cfg, db, s, d = lib
    seg = _seg("hello")
    _script(d, "001-a.segments.json", [seg])
    root = os.path.join(d, ".cache")
    old = _write_wav(root, "null", seg, cfg.synth.sample_rate)
    r = cache.compact(cfg, db, dry_run=True, log=lambda *_: None)
    assert r["converted"] == 1 and os.path.isfile(old)
    assert not os.path.isdir(os.path.join(root, "null",
                                          cache.current_fingerprint(cfg)))


def test_compact_writes_a_readable_fingerprint_file(lib):
    cfg, db, s, d = lib
    seg = _seg("hello")
    _script(d, "001-a.segments.json", [seg])
    _write_wav(os.path.join(d, ".cache"), "null", seg, cfg.synth.sample_rate)
    cache.compact(cfg, db, log=lambda *_: None)
    p = os.path.join(d, ".cache", "null", cache.current_fingerprint(cfg),
                     cache.FINGERPRINT_FILE)
    mat = json.load(open(p))
    assert mat["fingerprint"] == cache.current_fingerprint(cfg)
    assert mat["backend"] == "null"


def test_compact_files_a_stale_generation_into_the_current_one(lib):
    cfg, db, s, d = lib
    sr = cfg.synth.sample_rate
    seg = _seg("hello")
    _script(d, "001-a.segments.json", [seg])
    root = os.path.join(d, ".cache")
    _write_wav(root, "null", seg, sr, gen="oldgen")
    cache.compact(cfg, db, log=lambda *_: None)
    assert os.path.isfile(pipeline._cache_path(root, "null", seg, sr,
                                               cache.current_fingerprint(cfg)))
    assert not os.path.isdir(os.path.join(root, "null", "oldgen"))


def test_compact_survives_a_corrupt_entry(lib):
    cfg, db, s, d = lib
    seg = _seg("hello")
    _script(d, "001-a.segments.json", [seg])
    root = os.path.join(d, ".cache")
    p = _write_wav(root, "null", seg, cfg.synth.sample_rate)
    open(p, "wb").write(b"not audio at all")
    r = cache.compact(cfg, db, log=lambda *_: None)
    assert r["failed"] == 1 and r["converted"] == 0
    assert os.path.isfile(p)                  # the bad source is left in place


# -- mixed generations -----------------------------------------------------

def test_mixed_generations_flags_an_inconsistent_series(lib):
    cfg, db, s, d = lib
    rows = db.chapters(s["id"])
    db.mark_stage(rows[0]["id"], "rendered", synth_fingerprint="aaa")
    db.mark_stage(rows[1]["id"], "rendered", synth_fingerprint="aaa")
    assert cache.mixed_generations(db) == []
    db.mark_stage(rows[2]["id"], "rendered", synth_fingerprint="bbb")
    mixed = cache.mixed_generations(db)
    assert len(mixed) == 1
    assert mixed[0]["generations"] == {"aaa": 2, "bbb": 1}


def test_render_records_the_fingerprint_and_round_trips_it(lib):
    cfg, db, s, d = lib
    rows = db.chapters(s["id"])
    db.mark_stage(rows[0]["id"], "rendered", synth_fingerprint="gen-xyz")
    assert db.chapters(s["id"])[0]["synth_fingerprint"] == "gen-xyz"
    # and it survives the bundle export/import round trip
    assert "synth_fingerprint" in bundle._CHAPTER_COLS
