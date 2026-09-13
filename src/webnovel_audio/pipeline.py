"""text file -> segment script -> per-segment synthesis (cached) -> mastered .opus"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

import numpy as np
import soundfile as sf

from .audio import apply_chain, assemble, write_opus
from .config import Config
from .lexicon import Lexicon
from .normalize import Block, load_blocks_from_text
from .segment import Segment, build_segments, segments_to_json

_WORDS_PER_SEC = 2.7  # rough, for duration estimates only


@dataclass
class Report:
    blocks: int
    segments: int
    speech_segments: int
    audio_seconds: float
    wall_seconds: float
    realtime_factor: float
    script_path: str
    out_path: str = ""
    size_bytes: int = 0
    cached_segments: int = 0     # segments served from the cache, not synthesized
    total_segments: int = 0


def load_document(source: str, cfg: Config):
    """Return (blocks, meta, doc) for a source.

    A content provider (Royal Road, a saved .html, …) yields an ingest.Document
    with provenance stamped; `meta` carries the Opus tags. A plain .txt file is
    already the artifact, so `doc` is None and no Markdown is emitted for it.
    """
    from . import providers

    prov = providers.resolve(source)
    if prov is not None:
        doc = prov.read(source, cfg=cfg)
        blocks = list(doc.blocks)
        if cfg.general.speak_title and doc.chapter_title:
            fic = doc.fiction_title if cfg.general.speak_series else ""
            blocks.insert(0, Block("heading", doc.chapter_title, meta={"fiction": fic}))
        meta = {k: v for k, v in {
            "title": doc.chapter_title,
            "album": doc.fiction_title,
            "artist": doc.author,
            "comment": doc.url,            # players show this; keep it the source link
            "SOURCE_URL": doc.url,
            "FETCHED_AT": doc.retrieved_at,
            "RAW_SHA256": doc.raw_sha256,
        }.items() if v}
        return blocks, meta, doc

    return load_blocks_from_text(open(source, encoding="utf-8").read()), {}, None


def _make_backend(name: str, cfg: Config):
    if name == "null":
        from .synth.null import NullSynth

        return NullSynth(sample_rate=cfg.synth.sample_rate)
    if name == "kokoro":
        from .synth.kokoro import KokoroSynth

        return KokoroSynth(
            default_voice=cfg.voices.narrator,
            cache_dir=cfg.general.models_dir,
            speed=cfg.synth.speed,
        )
    raise SystemExit(f"unknown backend: {name!r}")


def _cache_path(cache_dir: str, backend: str, seg: Segment, sr: int) -> str:
    digest = hashlib.sha1(f"{seg.cache_key_material()}|{sr}".encode()).hexdigest()
    return os.path.join(cache_dir, backend, f"{digest}.wav")


def _dsp_spec(seg: Segment, cfg: Config) -> dict:
    """Merge effect-chain fields, most-specific key first: speaker -> voice -> style."""
    spec: dict = {}
    for key in (seg.speaker, seg.voice, seg.style):
        for field_, value in cfg.dsp.get(key, {}).items():
            spec.setdefault(field_, value)
    return spec


def _load_lexicon(cfg: Config) -> Lexicon | None:
    paths: list[str] = []
    base = getattr(cfg.general, "base_lexicon", "")
    if base and os.path.exists(base):
        paths.append(base)
    path = cfg.general.lexicon
    if path and os.path.exists(path) and path not in paths:
        paths.append(path)                       # per-series rows override the base
    if not paths:
        return None
    lex = Lexicon.load_many(paths)
    return lex if lex.entries else None


def build_script(source: str, cfg: Config):
    blocks, meta, doc = load_document(source, cfg)
    segs = build_segments(blocks, cfg, _load_lexicon(cfg))
    return blocks, segs, meta, doc


def render(
    input_txt: str,
    out_path: str,
    cfg: Config,
    *,
    backend: str = "kokoro",
    dry_run: bool = False,
    jobs: int = 1,
    md_meta: dict | None = None,
    tags: dict | None = None,
    log=print,
) -> Report:
    blocks, segs, doc_meta, doc = build_script(input_txt, cfg)

    stem = os.path.splitext(out_path)[0]
    os.makedirs(os.path.dirname(os.path.abspath(stem)) or ".", exist_ok=True)

    script_path = stem + ".segments.json"
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(segments_to_json(segs))

    if doc is not None:
        from .textout import render_markdown
        with open(stem + ".md", "w", encoding="utf-8") as fh:
            fh.write(render_markdown(doc, front_matter_extra=md_meta))
        log(f"text:   {stem}.md")

    speech = [s for s in segs if s.kind == "speech" and s.text.strip()]
    thought_n = sum(1 for s in speech if s.style == "thought")
    est_seconds = sum(len(s.text.split()) for s in speech) / _WORDS_PER_SEC
    log(f"blocks={len(blocks)}  segments={len(segs)}  speech={len(speech)}  "
        f"thought={thought_n}  est audio ~{est_seconds / 60:.1f} min")
    log(f"script: {script_path}")

    if dry_run:
        return Report(len(blocks), len(segs), len(speech), est_seconds, 0.0, 0.0, script_path)

    # ONNX Runtime already parallelises across all cores, so extra worker threads
    # just oversubscribe the 8-core APU (measured: jobs>1 raises wall time and heat
    # with no gain). Keep OMP bounded and let jobs=1 be the norm for Kokoro.
    if backend != "null" and jobs > 1:
        os.environ["OMP_NUM_THREADS"] = str(max(1, 8 // jobs))

    backend_impl = _make_backend(backend, cfg)
    sr = getattr(backend_impl, "sample_rate", cfg.synth.sample_rate)
    cache_dir = cfg.general.cache_dir
    os.makedirs(os.path.join(cache_dir, backend), exist_ok=True)

    hits = [0]

    def render_one(idx: int, seg: Segment):
        if seg.kind == "cue":
            from .audio import earcon
            return idx, earcon(sr)
        if seg.kind != "speech" or not seg.text.strip():
            return idx, np.zeros(0, dtype="float32")
        cpath = _cache_path(cache_dir, backend, seg, sr)
        if os.path.exists(cpath):
            hits[0] += 1
            data, _ = sf.read(cpath, dtype="float32")
            return idx, data
        audio = backend_impl.synth(seg)
        sf.write(cpath, audio, sr, subtype="FLOAT")
        return idx, audio

    renders: list[np.ndarray | None] = [None] * len(segs)
    t0 = time.time()

    if jobs > 1 and backend != "null":
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(render_one, i, s) for i, s in enumerate(segs)]
            for done, fut in enumerate(futures, 1):
                idx, audio = fut.result()
                renders[idx] = audio
                if done % 20 == 0:
                    log(f"  synth {done}/{len(segs)}")
    else:
        for i, seg in enumerate(segs, 1):
            idx, audio = render_one(i - 1, seg)
            renders[idx] = audio
            if i % 20 == 0:
                log(f"  synth {i}/{len(segs)}")

    backend_impl.close()

    if cfg.audio.dsp:
        renders = [
            apply_chain(r, sr, _dsp_spec(segs[i], cfg))
            if r is not None and r.size and segs[i].kind != "cue" else r
            for i, r in enumerate(renders)
        ]

    wav = assemble(segs, renders, sr, lead_ms=cfg.pauses.lead_ms, tail_ms=cfg.pauses.tail_ms)
    audio_seconds = wav.size / sr
    default_title = os.path.splitext(os.path.basename(out_path))[0]
    write_opus(
        wav, sr, out_path,
        bitrate=cfg.audio.opus_bitrate,
        loud=(cfg.audio.loudness_i, cfg.audio.loudness_tp, cfg.audio.loudness_lra),
        meta={**cfg.metadata, "title": default_title, **doc_meta, **(tags or {})},
    )

    wall = time.time() - t0
    size = os.path.getsize(out_path)
    return Report(
        blocks=len(blocks),
        segments=len(segs),
        speech_segments=len(speech),
        audio_seconds=audio_seconds,
        wall_seconds=wall,
        realtime_factor=(audio_seconds / wall if wall else 0.0),
        script_path=script_path,
        out_path=out_path,
        size_bytes=size,
        cached_segments=hits[0],
        total_segments=len(speech),
    )
