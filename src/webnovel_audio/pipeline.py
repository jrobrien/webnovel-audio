"""text file -> segment script -> per-segment synthesis (cached) -> mastered .opus"""
from __future__ import annotations

from .errors import DependencyMissing

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field

import numpy as np
import soundfile as sf

from .audio import apply_chain, assemble, write_opus
from .config import Config
from .config import resolve_data_path
from .document import Document
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
    skipped: bool = False        # nothing that affects the audio changed: left as it was


def write_markdown(doc: Document | None, stem: str, *, stage: str,
                   md_meta: dict | None = None) -> str | None:
    """The readable `.md` peek artifact for a parsed chapter, from its `Document`.

    The one place that turns a `Document` into that file — `render()` and
    `sync._do_parse` both call this rather than each keeping their own copy of
    the same three lines, so the "parse" and "render" stages can't quietly
    drift apart in how they render the same input.
    """
    if doc is None:
        return None
    from .textout import render_markdown

    path = stem + ".md"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(doc, front_matter_extra=md_meta, stage=stage))
    return path


def document_script(doc: Document, cfg: Config):
    """(blocks, meta) for a Document: its blocks, optionally led by a spoken
    title heading, and the Opus tags it carries."""
    blocks = list(doc.blocks)
    fic = (cfg.general.spoken_title or doc.fiction_title) if cfg.general.speak_series else ""
    chapter = doc.chapter_title if cfg.general.speak_chapter_title else ""
    if cfg.general.speak_title and (chapter or (fic and doc.chapter_title)):
        blocks.insert(0, Block("heading", chapter, meta={"fiction": fic}))
    meta = {k: v for k, v in {
        "title": doc.chapter_title,
        "album": doc.fiction_title,
        "artist": doc.author,
        "comment": doc.url,            # players show this; keep it the source link
        "SOURCE_URL": doc.url,
        "FETCHED_AT": doc.retrieved_at,
        "RAW_SHA256": doc.raw_sha256,
    }.items() if v}
    return blocks, meta


def load_document(source: str, cfg: Config):
    """Return (blocks, meta, doc) for a one-off source (a URL, a saved page,
    a .txt). Tracked chapters don't come through here: `sync` parses their
    cached raw artifact with the series' own provider.

    A plain .txt file is already the artifact, so `doc` is None and no
    Markdown is emitted for it.
    """
    from . import providers

    prov = providers.resolve(source)
    if prov is not None:
        doc = prov.read(source, providers.context(cfg, prov))
        blocks, meta = document_script(doc, cfg)
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


def cache_digest(seg: Segment, sr: int) -> str:
    return hashlib.sha1(f"{seg.cache_key_material()}|{sr}".encode()).hexdigest()


#: Segments are written here. FLAC/PCM_16 rather than float32 wav: 29% of the
#: size, and its -96 dBFS floor is far below what the ~48 kbps Opus encode
#: downstream contributes. soundfile's FLAC has no float subtype.
CACHE_EXT = ".flac"
CACHE_SUBTYPE = "PCM_16"


def _cache_path(cache_dir: str, backend: str, seg: Segment, sr: int,
                fingerprint: str = "") -> str:
    """Where this segment is written. Under a generation directory, so a model
    or g2p bump starts a new generation rather than silently reusing audio the
    old model produced."""
    digest = cache_digest(seg, sr)
    root = os.path.join(cache_dir, backend, fingerprint) if fingerprint \
        else os.path.join(cache_dir, backend)
    return os.path.join(root, f"{digest}{CACHE_EXT}")


def _cache_lookup(cache_dir: str, backend: str, seg: Segment, sr: int,
                  fingerprint: str) -> str | None:
    """An existing cache entry for this segment, or None.

    Tries the current generation first, then the pre-migration flat layout, so
    a tree that hasn't run `cache compact` yet still gets its hits.
    """
    digest = cache_digest(seg, sr)
    base = os.path.join(cache_dir, backend)
    cands = []
    if fingerprint:
        cands += [os.path.join(base, fingerprint, f"{digest}{CACHE_EXT}"),
                  os.path.join(base, fingerprint, f"{digest}.wav")]
    cands += [os.path.join(base, f"{digest}{CACHE_EXT}"),
              os.path.join(base, f"{digest}.wav")]        # pre-compact layout
    return next((c for c in cands if os.path.exists(c)), None)


def _dsp_spec(seg: Segment, cfg: Config) -> dict:
    """Merge effect-chain fields, most-specific key first: speaker -> voice -> style."""
    spec: dict = {}
    for key in (seg.speaker, seg.voice, seg.style):
        for field_, value in cfg.dsp.get(key, {}).items():
            spec.setdefault(field_, value)
    return spec


def _load_lexicon(cfg: Config) -> Lexicon | None:
    """The rules to read the text with: the always-on base, then the series'.

    A configured-but-missing base lexicon raises rather than being skipped.
    It used to be skipped, and the failure was invisible in every direction
    that matters -- the render succeeded, the audio sounded fine unless you
    knew the word, and the only trace was an empty `base_lexicon_sha256` in
    the manifest. 23 chapters were published saying "ky" for every `qi`
    before anyone heard it. An exception is not a worse outcome than that; it
    is the only one that gets noticed.
    """
    paths: list[str] = []
    base = resolve_data_path(getattr(cfg.general, "base_lexicon", ""))
    if base:
        if not os.path.exists(base):
            raise DependencyMissing(
                f"base lexicon not found: {base}\n"
                f"  (from general.base_lexicon = "
                f"{getattr(cfg.general, 'base_lexicon', '')!r})\n"
                f"  set it to an absolute path, or to \"\" to render without one.")
        paths.append(base)
    path = cfg.general.lexicon
    if path and os.path.exists(path) and path not in paths:
        paths.append(path)                       # per-series rows override the base
    if not paths:
        return None
    lex = Lexicon.load_many(paths)
    return lex if lex.rules else None


def _load_nlp(cfg: Config):
    """The POS tagger the pronunciation rules are resolved against.

    Required: without it espeak decides heteronyms itself and gets them wrong
    more often than always guessing the commoner reading would. Failing here
    with a fix is better than silently shipping "Will he lyve?".
    """
    from . import tagger

    model = getattr(cfg.general, "tagger", "") or tagger.DEFAULT_MODEL
    nlp = tagger.load(model)
    if nlp is None:
        raise DependencyMissing(
            f"the POS tagger ({model}) is not installed, and pronunciation "
            f"rules need it.\n  install it with: webnovel-audio tagger install")
    return nlp


def preflight(cfg: Config, backend: str | None = None) -> None:
    """Check, once and cheaply, that a render can work at all.

    Raises `DependencyMissing` naming the first thing that is missing. A run calls
    this before its first chapter, so a broken environment stops it with one
    message instead of failing (or hanging on) every chapter.
    """
    from . import tagger
    from .audio import _ffmpeg

    _ffmpeg()
    _load_lexicon(cfg)                       # raises if the configured base lexicon is gone
    model = getattr(cfg.general, "tagger", "") or tagger.DEFAULT_MODEL
    if not tagger.available(model):
        raise DependencyMissing(
            f"the POS tagger ({model}) is not installed, and pronunciation "
            f"rules need it.\n  install it with: webnovel-audio tagger install")
    if (backend or cfg.synth.backend) == "kokoro":
        from .synth.kokoro import check_available
        check_available(cfg.general.models_dir)


#: `build_script(lexicon=...)` default: load the lexicon from `cfg`
_LOAD = object()


def build_script(source: str | Document, cfg: Config, *, lexicon=_LOAD, nlp=None):
    """(blocks, segments, tags, doc) for a chapter: the script a render speaks.

    `lexicon` and `nlp` are loaded from `cfg` unless given. A caller walking many
    chapters of one series (`stale`) loads them once and passes them in; they
    must be what `_load_lexicon(cfg)` / `_load_nlp(cfg)` return, or the script
    is not the one a render would build.
    """
    if isinstance(source, Document):
        doc = source
        blocks, meta = document_script(doc, cfg)
    else:
        blocks, meta, doc = load_document(source, cfg)
    lex = _load_lexicon(cfg) if lexicon is _LOAD else lexicon
    segs = build_segments(blocks, cfg, lex, nlp if nlp is not None else _load_nlp(cfg))
    return blocks, segs, meta, doc


@dataclass
class Job:
    """A chapter that has been synthesized and assembled but not yet mastered.

    `prepare` (synthesis, cache, DSP, assembly) uses every core; `master` (the
    ffmpeg loudness + Opus pass) is single-threaded. Splitting them lets one
    chapter master while the next is still synthesizing.
    """
    out_path: str
    report: Report
    wav: np.ndarray | None = None      # None: a dry run, or an unchanged chapter
    sr: int = 0
    meta: dict = field(default_factory=dict)
    bitrate: str = "32k"
    loud: tuple = (-19.0, -3.0, 11.0)
    mastering: str = "fast"
    recipe: str = ""
    recipe_parts: dict = field(default_factory=dict)
    prepare_seconds: float = 0.0


#: the parts of a recipe, in `.recipe` order; `stale` names a changed one
RECIPE_PARTS = ("segs", "audio", "dsp", "pauses", "sr", "backend", "fp")
#: a Segment's fields, as what is said and who says it how
SEG_TEXT_FIELDS = ("text", "kind", "pause_after_ms")
SEG_VOICE_FIELDS = ("voice", "style", "rate", "pitch", "speaker")


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def audio_recipe_parts(segs: list[Segment], cfg: Config, backend: str,
                       fingerprint: str) -> tuple[str, dict]:
    """(`audio_recipe`, a hash per part of it). The overall hash is computed
    exactly as before parts existed, so a `.recipe` written then still matches."""
    audio = asdict(cfg.audio)
    audio.pop("master_jobs", None)                 # how it is scheduled, not how it sounds
    material = {"v": 1, "segs": [asdict(s) for s in segs], "audio": audio, "dsp": cfg.dsp,
                "pauses": asdict(cfg.pauses), "sr": cfg.synth.sample_rate,
                "backend": backend, "fp": fingerprint}
    parts = {k: _digest(material[k]) for k in RECIPE_PARTS}
    # the script split in two, so a changed one says whether the words or the
    # voices changed (a lexicon fix vs a cast change); not part of the hash
    for name, fields in (("segs_text", SEG_TEXT_FIELDS), ("segs_voice", SEG_VOICE_FIELDS)):
        parts[name] = _digest([[d[f] for f in fields] for d in material["segs"]])
    return _digest(material), parts


def audio_recipe(segs: list[Segment], cfg: Config, backend: str, fingerprint: str) -> str:
    """A hash of everything that decides what a chapter's audio sounds like: the
    segment script plus every setting between it and the file. If it matches the
    one stored beside the .opus, re-rendering would produce the same audio."""
    return audio_recipe_parts(segs, cfg, backend, fingerprint)[0]


def _recipe_path(out_path: str) -> str:
    return os.path.splitext(out_path)[0] + ".recipe"


def read_recipe(out_path: str) -> tuple[str, dict] | None:
    """The stored (overall hash, parts) beside `out_path`, or None without a
    `.recipe`. `.recipe` is the overall hash on its first line, then the parts
    as JSON on the second; a file from before parts has only the first line,
    and its parts come back empty."""
    try:
        with open(_recipe_path(out_path), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    parts: dict = {}
    if len(lines) > 1:
        try:
            parts = json.loads(lines[1])
        except ValueError:
            parts = {}
    return (lines[0].strip() if lines else ""), (parts if isinstance(parts, dict) else {})


def _recipe_matches(out_path: str, recipe: str) -> bool:
    """Only the first line counts: the parts line is for explaining a mismatch."""
    stored = read_recipe(out_path)
    return stored is not None and os.path.exists(out_path) and stored[0] == recipe


def _write_recipe(out_path: str, recipe: str, parts: dict | None = None) -> None:
    tmp = _recipe_path(out_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(recipe + "\n")
        if parts:
            fh.write(json.dumps(parts, sort_keys=True) + "\n")
    os.replace(tmp, _recipe_path(out_path))


def prepare(
    source: str | Document,
    out_path: str,
    cfg: Config,
    *,
    backend: str = "kokoro",
    dry_run: bool = False,
    jobs: int = 1,
    md_meta: dict | None = None,
    tags: dict | None = None,
    log=print,
    skip_unchanged: bool = False,
    fingerprint: str = "",
) -> Job:
    """Everything up to (not including) mastering. See `render` for the arguments;
    `skip_unchanged` (with the synth `fingerprint`) leaves a chapter alone when its
    stored recipe matches, which a chapter rendered before recipes existed never
    does, so it is re-rendered once and skippable from then on."""
    blocks, segs, doc_meta, doc = build_script(source, cfg)

    stem = os.path.splitext(out_path)[0]
    os.makedirs(os.path.dirname(os.path.abspath(stem)) or ".", exist_ok=True)

    script_path = stem + ".segments.json"
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(segments_to_json(segs))

    if write_markdown(doc, stem, stage="render", md_meta=md_meta):
        log(f"text:   {stem}.md")

    speech = [s for s in segs if s.kind == "speech" and s.text.strip()]
    thought_n = sum(1 for s in speech if s.style == "thought")
    est_seconds = sum(len(s.text.split()) for s in speech) / _WORDS_PER_SEC
    log(f"blocks={len(blocks)}  segments={len(segs)}  speech={len(speech)}  "
        f"thought={thought_n}  est audio ~{est_seconds / 60:.1f} min")
    log(f"script: {script_path}")

    if dry_run:
        return Job(out_path, Report(len(blocks), len(segs), len(speech), est_seconds, 0.0, 0.0,
                                    script_path))

    recipe, parts = audio_recipe_parts(segs, cfg, backend, fingerprint)
    if skip_unchanged and fingerprint and _recipe_matches(out_path, recipe):
        log("unchanged: the audio recipe matches, not re-rendering")
        return Job(out_path, Report(len(blocks), len(segs), len(speech), 0.0, 0.0, 0.0,
                                    script_path, out_path=out_path,
                                    size_bytes=os.path.getsize(out_path),
                                    cached_segments=len(speech), total_segments=len(speech),
                                    skipped=True), recipe=recipe, recipe_parts=parts)

    # ONNX Runtime already parallelises across all cores, so extra worker threads
    # just oversubscribe the 8-core APU (measured: jobs>1 raises wall time and heat
    # with no gain). Keep OMP bounded and let jobs=1 be the norm for Kokoro.
    if backend != "null" and jobs > 1:
        os.environ["OMP_NUM_THREADS"] = str(max(1, 8 // jobs))

    backend_impl = _make_backend(backend, cfg)
    sr = getattr(backend_impl, "sample_rate", cfg.synth.sample_rate)
    cache_dir = cfg.general.cache_dir
    fp = getattr(backend_impl, "fingerprint", "")
    os.makedirs(os.path.join(cache_dir, backend, fp), exist_ok=True)

    hits = [0]

    def render_one(idx: int, seg: Segment):
        if seg.kind == "cue":
            from .audio import earcon
            return idx, earcon(sr)
        if seg.kind != "speech" or not seg.text.strip():
            return idx, np.zeros(0, dtype="float32")
        found = _cache_lookup(cache_dir, backend, seg, sr, fp)
        if found:
            hits[0] += 1
            data, _ = sf.read(found, dtype="float32")
            return idx, data
        audio = backend_impl.synth(seg)
        cpath = _cache_path(cache_dir, backend, seg, sr, fp)
        sf.write(cpath, audio, sr, subtype=CACHE_SUBTYPE)
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
    default_title = os.path.splitext(os.path.basename(out_path))[0]
    report = Report(
        blocks=len(blocks), segments=len(segs), speech_segments=len(speech),
        audio_seconds=wav.size / sr, wall_seconds=0.0, realtime_factor=0.0,
        script_path=script_path, out_path=out_path,
        cached_segments=hits[0], total_segments=len(speech),
    )
    return Job(
        out_path, report, wav=wav, sr=sr,
        meta={**cfg.metadata, "title": default_title, **doc_meta, **(tags or {})},
        bitrate=cfg.audio.opus_bitrate,
        loud=(cfg.audio.loudness_i, cfg.audio.loudness_tp, cfg.audio.loudness_lra),
        mastering=cfg.audio.mastering, recipe=recipe, recipe_parts=parts, prepare_seconds=time.time() - t0,
    )


def master(job: Job) -> Report:
    """Loudness-master and encode a prepared chapter. Safe to run on a thread:
    it touches only its own audio, a temp dir and its own output file."""
    rep = job.report
    if job.wav is None:                       # a dry run, or an unchanged chapter
        return rep
    t0 = time.time()
    write_opus(job.wav, job.sr, job.out_path, bitrate=job.bitrate, loud=job.loud,
               meta=job.meta, mastering=job.mastering)
    if job.recipe:
        _write_recipe(job.out_path, job.recipe, job.recipe_parts)
    wall = job.prepare_seconds + (time.time() - t0)
    rep.wall_seconds = wall
    rep.realtime_factor = rep.audio_seconds / wall if wall else 0.0
    rep.size_bytes = os.path.getsize(job.out_path)
    job.wav = None                            # free ~150 MB per chapter
    return rep


def render(
    source: str | Document,
    out_path: str,
    cfg: Config,
    *,
    backend: str = "kokoro",
    dry_run: bool = False,
    jobs: int = 1,
    md_meta: dict | None = None,
    tags: dict | None = None,
    log=print,
    skip_unchanged: bool = False,
    fingerprint: str = "",
) -> Report:
    return master(prepare(source, out_path, cfg, backend=backend, dry_run=dry_run, jobs=jobs,
                          md_meta=md_meta, tags=tags, log=log,
                          skip_unchanged=skip_unchanged, fingerprint=fingerprint))
