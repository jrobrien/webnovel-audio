from __future__ import annotations

import os

# Keep ONNX Runtime / OpenMP from oversubscribing the mobile APU (8 cores).
os.environ.setdefault("OMP_NUM_THREADS", "6")

import argparse  # noqa: E402

from . import __version__  # noqa: E402
from .config import Config  # noqa: E402

from .dialogue import FEMALE_VOICE_POOL, MALE_VOICE_POOL  # noqa: E402
from .dialogue import suggest_voices as _suggest_voices  # noqa: E402

_INPUT_HELP = "text file, .html file, or http(s) chapter URL"

# all English Kokoro voice ids (af/am = US, bf/bm = UK)
_EN_VOICES = sorted(set(MALE_VOICE_POOL + FEMALE_VOICE_POOL) | {
    "am_adam", "am_echo", "am_santa", "af_alloy", "af_jessica", "af_nova", "af_river",
    "bm_daniel", "bf_lily", "bm_george",
})


def _default_config() -> str:
    here = os.path.dirname(__file__)
    for candidate in ("config.toml", os.path.join(here, "..", "..", "config.example.toml")):
        if os.path.exists(candidate):
            return candidate
    return "config.toml"


# --- target + range resolution --------------------------------------------

def _jprint(obj) -> None:
    import json as _json

    print(_json.dumps(obj, ensure_ascii=False))


def _parse_range(s):
    """'', '5', '5-9', '5-', '-9' -> (lo, hi); None means open."""
    s = (s or "").strip()
    if not s:
        return None, None
    if "-" in s:
        a, _, b = s.partition("-")
        return (int(a) if a.strip() else None), (int(b) if b.strip() else None)
    return int(s), int(s)


def _is_file_target(target: str) -> bool:
    """A path or URL is a one-off; anything else is a tracked-series key."""
    return os.path.exists(target) or target.startswith(("http://", "https://"))


def _stage_cmd(stage: str):
    """Build the handler for a pipeline verb — they differ only by stage."""

    def run(args) -> int:
        import json as _json
        import sys

        from . import sync

        cfg = Config.load(args.config)
        want_json = getattr(args, "json", False)
        if want_json:
            try:
                sys.stdout.reconfigure(line_buffering=True)
            except (AttributeError, ValueError):
                pass

        if _is_file_target(args.target):
            return _one_off(stage, args, cfg)

        lo, hi = _parse_range(getattr(args, "range", ""))
        emit = (lambda d: print(_json.dumps(d, ensure_ascii=False), flush=True)) \
            if want_json else None
        try:
            with sync.sync_lock(cfg) if stage == "rendered" else _nullctx():
                res = sync.run_stage(
                    cfg, stage, args.target, lo=lo, hi=hi,
                    limit=getattr(args, "limit", None),
                    backend=getattr(args, "backend", None),
                    dry_run=getattr(args, "dry_run", False),
                    log=(lambda *_: None) if want_json else print, emit=emit)
        except sync.SyncLocked as exc:
            if want_json:
                print(_json.dumps({"event": "locked", "path": exc.path}), flush=True)
            else:
                print(f"! {exc} — wait for it to finish.")
            return 2
        if not want_json:
            verb = {"fetched": "fetched", "parsed": "parsed", "rendered": "rendered"}[stage]
            print(f"\n{verb}: {res.rendered}, {res.errors} error(s)"
                  + (f", {res.skipped} would-run" if res.skipped else ""))
        return 1 if res.errors else 0

    return run


import contextlib  # noqa: E402


@contextlib.contextmanager
def _nullctx():
    yield


def _one_off(stage: str, args, cfg: Config) -> int:
    """A path/URL target: no DB, no state — just run the stage and report."""
    from . import pipeline

    if stage == "rendered":
        out = getattr(args, "out", None) or (os.path.splitext(
            os.path.basename(args.target))[0] + ".opus")
        if getattr(args, "backend", None):
            cfg.synth.backend = args.backend
        rep = pipeline.render(args.target, out, cfg, backend=cfg.synth.backend,
                              dry_run=getattr(args, "dry_run", False))
        print(f"  audio       : {rep.out_path}" if rep.out_path else "  (dry run)")
        if rep.audio_seconds:
            print(f"  duration    : {rep.audio_seconds / 60:.1f} min")
            print(f"  realtime x  : {rep.realtime_factor:.1f}")
        return 0
    if stage == "parsed":
        return _explain(args.target, cfg) if getattr(args, "explain", False) \
            else _write_md(args.target, cfg)
    if stage == "fetched":
        from .ingest import fetch_html
        out = getattr(args, "out", None) or "chapter.html"
        html = fetch_html(args.target)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"saved {out} ({len(html)} bytes)")
        return 0
    return 0


def _write_md(source: str, cfg: Config) -> int:
    from .pipeline import load_document
    from .textout import render_markdown

    _, _, doc = load_document(source, cfg)
    if doc is None:
        print("plain text input — already the readable artifact, nothing to write.")
        return 0
    out = os.path.splitext(os.path.basename(source))[0] + ".md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(doc))
    print(f"wrote {out}")
    return 0


def _explain(source: str, cfg: Config) -> int:
    """How the parser + segmenter saw this input (the old `inspect`)."""
    from .dialogue import discover
    from .pipeline import _load_lexicon, build_script
    from .segment import find_unknown_names

    blocks, segs, meta, _ = build_script(source, cfg)
    kinds: dict[str, int] = {}
    for b in blocks:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    styles: dict[str, int] = {}
    for s in segs:
        styles[s.style] = styles.get(s.style, 0) + 1
    speech = [s for s in segs if s.kind == "speech" and s.text.strip()]
    print(f"config      : {cfg.general.lexicon or '(base only)'}")
    if meta.get("title"):
        print(f"chapter     : {meta['title']}  ({meta.get('album', '')})")
    print(f"blocks      : {len(blocks)}  {kinds}")
    print(f"segments    : {len(segs)}  {styles}")
    words = sum(len(s.text.split()) for s in speech)
    print(f"words       : {words}   est audio ~{words / 155:.1f} min")

    counts, gender = discover(blocks, cfg)
    if counts:
        print("\ndialogue by speaker -> voice:")
        voices = _suggest_voices(counts, gender, cfg)
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if name:
                print(f"  {n:3d}  {name:<16} {voices.get(name, '?')}")
    lex = _load_lexicon(cfg)
    unknown = find_unknown_names(blocks, lex.surfaces() if lex else set())
    print(f"\ncandidate proper nouns not in lexicon ({len(unknown)}):")
    for name, n in unknown[:20]:
        print(f"  {n:3d}  {name}")
    print("\nfirst 16 segments:")
    for s in segs[:16]:
        if s.kind == "cue":
            print("  [            cue] <earcon>")
        else:
            print(f"  [{s.speaker or s.style:>15}] {s.text[:96]}")
    return 0


def _cmd_check(args) -> int:
    """Cast / heteronyms / unknown names over a range, with optional --write."""
    from . import sync

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)

    if _is_file_target(args.target):
        if want_json:
            _jprint({"ok": False, "error": "check --json needs a tracked series"})
            return 1
        return _explain(args.target, cfg)

    lo, hi = _parse_range(getattr(args, "range", ""))
    report = sync.suggest_cast(cfg, args.target, lo=lo, hi=hi, context=args.context,
                               write_lexicon=False, apply_cast=False,
                               log=(lambda *_: None) if want_json else print)
    if want_json:
        _jprint({"ok": True, **report})
        return 0
    if not report["chapters_sampled"]:
        return 0

    new = [n for n, i in report["cast"].items() if i["new"]]
    print(f"\ncast — {len(report['cast'])} speaker(s), {len(new)} not yet assigned a voice")
    for name, info in sorted(report["cast"].items(), key=lambda kv: -kv[1]["count"])[:15]:
        g = {"m": "male", "f": "female"}.get(info["gender"], "gender unclear")
        tag = "NEW" if info["new"] else "mapped"
        print(f'  {name:<20} {info["count"]:>4} line(s), {g:<14} [{tag}]')
    if new:
        print(f"  add them:  webnovel-audio cast update {report['slug']} {args.range or ''}".rstrip())

    if report["heteronyms"]:
        print("\nheteronyms — context-dependent, judge by ear:")
        for h in report["heteronyms"]:
            print(f"  {h['word']:<10} x{h['count']:<3} \"...{h['context']}...\"")
        print(f'  pin one:  webnovel-audio lex add {report["slug"]} "a tear in" "a tair in"')

    cands = report["lexicon_candidates"]
    if cands:
        shown = cands[:max(0, args.top)]
        print(f"\nproper nouns in no lexicon ({len(cands)}, most frequent first):")
        print(f"  {', '.join(shown)}" + (f"  … +{len(cands) - len(shown)} more"
                                         if len(cands) > len(shown) else ""))
        print(f'  fix one:     webnovel-audio lex add {report["slug"]} Kaelith kay-lith')
        print(f'  silence one: webnovel-audio lex ignore {report["slug"]} '
              f'{" ".join(shown[:3])}')
    return 0


_TOKENIZER = None


def _phonemes(text: str) -> str:
    """The exact phoneme string Kokoro tokenizes for `text` (needs no ONNX model)."""
    global _TOKENIZER
    if _TOKENIZER is None:
        try:
            from kokoro_onnx.tokenizer import Tokenizer
        except ModuleNotFoundError:
            raise SystemExit("`pron` needs the TTS engine:  uv sync --extra kokoro")
        _TOKENIZER = Tokenizer()
    return _TOKENIZER.phonemize(text)


# rough IPA -> ASCII. Multi-codepoint keys first; a single left-to-right pass
# (no chained str.replace — the outputs contain letters that are themselves keys).
# Plain b/d/f/h/k/l/m/n/p/r/s/t/v/w/z and punctuation fall through unchanged.
_GLOSS_MAP = [
    ("eɪ", "ay"), ("aɪ", "y"), ("ɔɪ", "oy"), ("aʊ", "ow"), ("oʊ", "oh"),
    ("ɛə", "air"), ("ɪə", "eer"), ("ʊə", "oor"),
    ("tʃ", "ch"), ("dʒ", "j"), ("ʃ", "sh"), ("ʒ", "zh"), ("θ", "th"), ("ð", "th"),
    ("ŋ", "ng"), ("ɑː", "ah"), ("ɔː", "aw"), ("iː", "ee"), ("uː", "oo"),
    ("ɜː", "ur"), ("ɜ", "ur"), ("ɑ", "ah"), ("ɒ", "o"), ("ɔ", "aw"), ("æ", "a"),
    ("ʌ", "uh"), ("ɐ", "uh"), ("ə", "uh"), ("ɚ", "ur"), ("ɝ", "ur"), ("ɛ", "eh"),
    ("ɪ", "ih"), ("ᵻ", "ih"), ("i", "ee"), ("ʊ", "uu"), ("u", "oo"), ("e", "eh"),
    ("o", "oh"), ("a", "ah"), ("ɹ", "r"), ("ɡ", "g"), ("j", "y"), ("ç", "h"),
    ("x", "kh"), ("ʔ", ""), ("ː", ""),
]


def _translit(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        for k, v in _GLOSS_MAP:
            if s.startswith(k, i):
                out.append(v)
                i += len(k)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _gloss(phonemes: str) -> str:
    """A crude how-to-say-it rendering of a phoneme string. Approximate."""
    import re as _re

    words = []
    for w in phonemes.split():
        one_stress = w.count("ˈ") == 1
        parts, stressed, pending = [], None, False
        for tok in _re.split(r"([ˈˌ])", w):
            if tok == "ˈ":
                pending = True
                continue
            if tok in ("ˌ", ""):
                continue
            if pending and one_stress:
                stressed = len(parts)
            pending = False
            parts.append(_translit(tok))
        if len(parts) > 1 and not any(c in "aeiou" for c in parts[0]):
            parts[1] = parts[0] + parts[1]                 # glue a vowel-less onset
            parts.pop(0)
            if stressed:
                stressed -= 1
        if stressed is not None and 0 <= stressed < len(parts):
            parts[stressed] = parts[stressed].upper()
        words.append("-".join(p for p in parts if p))
    return " ".join(words)


def _cmd_pron(args) -> int:
    from . import sync as _sync
    from .pipeline import _load_lexicon

    cfg = Config.load(args.config)
    if args.series:
        cfg = _sync._series_cfg(cfg, args.series)
    lex = None if args.no_lexicon else _load_lexicon(cfg)

    if args.check:
        from .lexicon import Lexicon

        seen, rows = set(), []
        for path in (cfg.general.base_lexicon, cfg.general.lexicon):
            if path and os.path.exists(path):
                rows += Lexicon.load(path).entries
        if not rows:
            print("no lexicon entries to check.")
            return 0
        for e in rows:
            if e.surface in seen:
                continue
            seen.add(e.surface)
            raw = _phonemes(e.surface)
            if not e.respell or e.respell == e.surface:
                print(f"  {e.surface:16} {raw}   (no respell)")
                continue
            new = _phonemes(e.respell)
            flag = "  = unchanged" if new == raw else ""
            print(f"  {e.surface:16} {raw}  ->  {new}   [{_gloss(new)}]{flag}")
        return 0

    text = " ".join(args.text).strip()
    if not text:
        print("give some text:  webnovel-audio pron Montgomery")
        return 1
    raw = _phonemes(text)
    print(f"text      : {text}")
    print(f"phonemes  : {raw}")
    print(f"≈ say     : {_gloss(raw)}")
    if lex is not None:
        applied = lex.apply(text)
        if applied != text:
            new = _phonemes(applied)
            print(f"\nwith lexicon : {applied}")
            print(f"phonemes     : {new}")
            print(f"≈ say        : {_gloss(new)}")
        else:
            print("\n(no lexicon entry changes this text)")
    return 0


_ACCENT = {"am": "American male", "af": "American female",
           "bm": "British male", "bf": "British female"}
_VOICE_SAMPLE = (
    "The old lighthouse keeper counted thirty-seven ships before dawn. "
    "\"Are you certain?\" she asked, not quite believing it. Rain hammered "
    "the glass, and somewhere far off, thunder rolled. It would be a long, "
    "strange night."
)


def _voice_label(v: str) -> str:
    pre, _, name = v.partition("_")
    return f"{_ACCENT.get(pre, pre)}. {name.capitalize() or v}."


def _cmd_voices(args) -> int:
    voices = [v.strip() for v in (args.only or "").split(",")] if args.only else list(_EN_VOICES)
    voices = [v for v in voices if v]

    if getattr(args, "action", "list") != "demo":
        for pre in ("am", "af", "bm", "bf"):
            row = [v for v in voices if v.startswith(pre + "_")]
            if row:
                print(f"{_ACCENT[pre]:16} {'  '.join(row)}")
        extra = [v for v in voices if v.split('_')[0] not in _ACCENT]
        if extra:
            print(f"{'other':16} {'  '.join(extra)}")
        print(f"\n{len(voices)} voices · audition file:  "
              f"webnovel-audio voices demo -o voices.opus")
        return 0

    from .audio import assemble, write_opus
    from .segment import Segment
    from .synth.kokoro import KokoroSynth

    cfg = Config.load(args.config)
    sample = (args.text or _VOICE_SAMPLE).strip()
    segs: list[Segment] = []
    for v in voices:
        segs.append(Segment(text=_voice_label(v), voice=args.announcer,
                            style="heading", pause_after_ms=250))
        segs.append(Segment(text=sample, voice=v, style="narration",
                            pause_after_ms=max(0, args.pause)))

    synth = KokoroSynth(default_voice=args.announcer,
                        cache_dir=cfg.general.models_dir, speed=cfg.synth.speed)
    sr = synth.sample_rate
    print(f"rendering {len(voices)} voices → {args.out} …")
    renders, chapters, t = [], [], 0.3          # 0.3s = assemble lead_ms
    for i, s in enumerate(segs):
        a = synth.synth(s)
        renders.append(a)
        if s.style == "heading":
            chapters.append((t, voices[i // 2]))   # a voice's section starts at its label
            print(f"  {voices[i // 2]}")
        t += (a.size / sr if a is not None else 0.0) + s.pause_after_ms / 1000.0
    synth.close()

    wav = assemble(segs, renders, sr, lead_ms=300, tail_ms=400)
    write_opus(wav, sr, args.out,
               bitrate=cfg.audio.opus_bitrate,
               loud=(cfg.audio.loudness_i, cfg.audio.loudness_tp, cfg.audio.loudness_lra),
               meta={**cfg.metadata, "title": "voice audition", "album": "webnovel-audio"},
               chapters=chapters)
    print(f"{args.out}  ({wav.size / sr:.0f}s, {len(chapters)} chapters — mpv: PgUp/PgDn)")
    return 0


def _cmd_ui(args) -> int:
    import shutil
    import sys

    project = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    tcl = os.path.join(project, "ui", "control.tcl")
    if not os.path.exists(tcl):
        print(f"UI script not found: {tcl}", file=sys.stderr)
        return 1

    env = dict(os.environ)
    exe = shutil.which("webnovel-audio") or os.path.realpath(sys.argv[0])
    if os.path.basename(exe).startswith("webnovel-audio") and os.access(exe, os.X_OK):
        env.setdefault("WEBNOVEL_AUDIO", exe)

    wish = None if args.python else shutil.which("wish")
    if wish:
        os.execve(wish, [wish, tcl], env)     # replace this process with the UI

    try:
        import tkinter
    except Exception:
        print("need `wish` (Arch: pacman -S tk) or Python tkinter to run the UI",
              file=sys.stderr)
        return 1
    os.environ.update(env)
    root = tkinter.Tk()
    root.tk.call("source", tcl)               # `source` (not eval) so it finds json.tcl
    root.mainloop()
    return 0


def _cmd_models(args) -> int:
    from .synth.kokoro import DEFAULT_CACHE, fetch_models, model_paths

    cache = args.cache_dir or DEFAULT_CACHE
    if args.action == "path":
        for p in model_paths(cache):
            print(f"{'ok ' if os.path.exists(p) else '-- '}{p}")
        return 0
    fetch_models(cache)
    return 0


def _editor_open(path: str, stub: str = "") -> int:
    """Open `path` in the user's editor. WEBNOVEL_AUDIO_EDITOR wins, then
    $VISUAL/$EDITOR, then xdg-open — xdg-open guesses from *content*, so an
    empty .csv and a populated one can land in different apps."""
    import shlex
    import subprocess

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if stub and not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stub)
    for var in ("WEBNOVEL_AUDIO_EDITOR", "VISUAL", "EDITOR"):
        cmd = os.environ.get(var, "").strip()
        if cmd:
            return subprocess.call([*shlex.split(cmd), path])
    return subprocess.call(["xdg-open", path])


def _lex_paths(cfg: Config, slug: str | None = None) -> tuple[str, str]:
    base = os.path.expanduser(cfg.general.base_lexicon or "data/lexicons/_base.csv")
    d = os.path.expanduser(cfg.general.lexicon_dir or "data/lexicons")
    return base, (os.path.join(d, f"{slug}.csv") if slug else "")


_LEX_HEADER = "surface,respell,ipa,notes\n"


def _cmd_lex(args) -> int:
    from .lexicon import Lexicon

    cfg = Config.load(args.config)
    base, series = _lex_paths(cfg, getattr(args, "slug", None))

    if args.action == "edit":
        return _editor_open(base if args.base else series, _LEX_HEADER)

    if args.action == "ignore":
        # A row with a blank `respell` is already a no-op substitution that
        # `surfaces()` counts as known — so "I looked at this and it reads fine"
        # needs no new machinery, and shrinks the `check` report next time.
        # Same idea as codespell's ignore-list / cspell's custom dictionary.
        import csv
        path = base if args.base else series
        exists = os.path.exists(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            if not exists:
                fh.write(_LEX_HEADER)
            w = csv.writer(fh)
            for word in args.words:
                w.writerow([word, "", "", "reads fine as-is"])
        print(f"{path}: {len(args.words)} word(s) marked fine as-is")
        return 0

    if args.action == "add":
        path = base if args.base else series
        import csv
        exists = os.path.exists(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            if not exists:
                fh.write(_LEX_HEADER)
            csv.writer(fh).writerow([args.surface, args.respell, "", args.note or ""])
        print(f"{path}: {args.surface} -> {args.respell}")
        return 0

    # list: the merged, effective view
    paths = [p for p in (base, series) if p and os.path.exists(p)]
    if not paths:
        print("no lexicon files yet")
        return 0
    lex = Lexicon.load_many(paths)
    if getattr(args, "json", False):
        _jprint({"entries": [vars(e) for e in lex.entries], "files": paths})
        return 0
    for e in lex.entries:
        print(f"  {e.surface:<20} {e.respell or e.ipa or '(no respell)'}"
              f"{'   # ' + e.notes if e.notes else ''}")
    print(f"\n{len(lex.entries)} entr(ies) from: {', '.join(paths)}")
    return 0


def _series_config_path(cfg: Config, slug: str) -> str:
    return os.path.join(
        os.path.expanduser(cfg.general.series_config_dir or "data/series"), f"{slug}.toml")


def _cmd_cast(args) -> int:
    """The series' voice assignments: edit / merge-in-new / show effective."""
    from . import sync
    from .db import DB

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    db = DB(cfg.royalroad.state_db)
    try:
        s = db.get_series(args.key)
        if not s:
            print(f"no tracked series matching {args.key!r}")
            return 1
        slug = sync._dir_slug(s)
    finally:
        db.close()
    path = _series_config_path(cfg, slug)

    if args.action == "edit":
        return _editor_open(path, sync.pin_defaults_text(cfg, slug))

    if args.action == "show":
        scfg = sync._series_cfg(cfg, slug)
        assigned = {k: v for k, v in scfg.cast.voices.items() if v}
        unassigned = [k for k, v in scfg.cast.voices.items() if not v]
        if want_json:
            _jprint({"slug": slug, "file": path if os.path.exists(path) else None,
                     "narrator": scfg.cast.narrator or scfg.voices.narrator,
                     "thought": scfg.voices.thought,
                     "default": scfg.cast.default or scfg.voices.dialogue_default,
                     "assigned": assigned, "unassigned": unassigned})
            return 0
        print(f"{s['title']}  [{slug}]   {path if os.path.exists(path) else '(no config yet)'}")
        print(f"  narrator {scfg.cast.narrator or scfg.voices.narrator} · "
              f"thought {scfg.voices.thought} · "
              f"default {scfg.cast.default or scfg.voices.dialogue_default}")
        for k, v in sorted(assigned.items()):
            print(f"    {k:<20} {v}")
        for k in sorted(unassigned):
            print(f"    {k:<20} —  unassigned, falls back to default")
        return 0

    # update: sample a range and merge in speakers we don't have yet
    lo, hi = _parse_range(getattr(args, "range", ""))
    report = sync.suggest_cast(cfg, args.key, lo=lo, hi=hi, apply_cast=not args.diff,
                               write_lexicon=False,
                               log=(lambda *_: None) if want_json else print)
    if want_json:
        _jprint({"ok": True, **report})
        return 0
    if not report["chapters_sampled"]:
        return 0
    new = {n: i for n, i in report["cast"].items() if i["new"]}
    if not new:
        print(f"\nno new speakers in that range — {path}")
        return 0
    verb = "would add" if args.diff else report["overlay_action"]
    print(f"\n{verb}: {path}")
    for name, info in sorted(new.items(), key=lambda kv: -kv[1]["count"]):
        g = {"m": "male", "f": "female"}.get(info["gender"], "gender unclear — pick one")
        v = info["voice"] or "\"\"  <- unassigned"
        print(f'  {name:<20} {v:<16} # {info["count"]} line(s), {g}')
    if not args.diff:
        print(f"\n  review it:  webnovel-audio cast edit {slug}")
    return 0


def _cmd_state(args) -> int:
    from .db import DB, STATUSES

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    db = DB(cfg.royalroad.state_db)
    try:
        s = db.get_series(args.key)
        if not s:
            print(f"no tracked series matching {args.key!r}")
            return 1
        lo, hi = _parse_range(getattr(args, "range", ""))

        if args.action == "set":
            if args.status not in STATUSES:
                print(f"status must be one of: {', '.join(STATUSES)}")
                return 1
            rows = db.range(s["id"], lo, hi)
            n = db.set_status([c["id"] for c in rows], args.status)
            (_jprint if want_json else print)(
                {"ok": True, "changed": n, "status": args.status} if want_json
                else f"{s['title']}: {n} chapter(s) -> {args.status}")
            return 0

        if args.action == "reset":
            rows = [c for c in db.range(s["id"], lo, hi) if c["status"] == "error"]
            n = db.set_status([c["id"] for c in rows], "new")
            (_jprint if want_json else print)(
                {"ok": True, "reset": n} if want_json
                else f"{s['title']}: {n} errored chapter(s) -> new")
            return 0

        rows = db.range(s["id"], lo, hi)
        if want_json:
            _jprint({"slug": s["slug"], "chapters": [
                {"number": c["ord"] + 1, "title": c["title"], "status": c["status"],
                 "error_stage": c["error_stage"], "error": c["error"],
                 "duration_s": c["duration_s"]} for c in rows]})
            return 0
        print(f"{s['title']}  [{s['slug']}]")
        for c in rows:
            mark = f"  {c['error_stage'] or ''} {c['error'] or ''}".rstrip() \
                if c["status"] == "error" else ""
            dur = f"  {c['duration_s'] / 60:.0f}m" if c["duration_s"] else ""
            print(f"  #{c['ord'] + 1:<4} {c['status']:<9} {(c['title'] or '')[:52]:<52}"
                  f"{dur}{mark}")
        return 0
    finally:
        db.close()


def _cmd_series(args) -> int:
    from . import sync
    from .db import DB

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)

    if args.action == "add":
        info = sync.add_series(cfg, args.url, start=args.frm,
                               log=(lambda *_: None) if want_json else print)
        if want_json:
            _jprint({"ok": True, **(info or {})})
        return 0

    if args.action == "refresh":
        results = sync.refresh(cfg, args.key,
                               log=(lambda *_: None) if want_json else print)
        if want_json:
            _jprint({"ok": True, "series": results or []})
        return 0

    if args.action in ("enable", "disable", "forget", "show"):
        db = DB(cfg.royalroad.state_db)
        try:
            s = db.get_series(args.key)
            if not s:
                (_jprint if want_json else print)(
                    {"ok": False, "error": f"no series matching {args.key!r}"}
                    if want_json else f"no tracked series matching {args.key!r}")
                return 1
            slug = sync._dir_slug(s)

            if args.action in ("enable", "disable"):
                db.set_enabled(s["id"], args.action == "enable")
                print(f"{s['title']}: sync {args.action}d")
                return 0

            if args.action == "forget":
                if args.purge:
                    import shutil
                    d = os.path.join(os.path.expanduser(cfg.royalroad.library_dir), slug)
                    shutil.rmtree(d, ignore_errors=True)
                    print(f"removed {d}")
                db.forget(s["id"])
                print(f"forgot {s['title']}")
                return 0

            # show
            info = db.summary(args.key)[0]
            if want_json:
                _jprint(info)
                return 0
            print(f"{info['title']}  [{info['slug']}]  {'' if info['enabled'] else '(sync disabled)'}")
            print(f"  {info['url']}")
            print(f"  {info['chapters']} chapters · " +
                  " · ".join(f"{k} {v}" for k, v in info["stages"].items() if v))
            if info["next"]:
                print(f"  next: #{info['next']['number']} {info['next']['title']}")
            return 0
        finally:
            db.close()

    # default: list
    db = DB(cfg.royalroad.state_db)
    rows = db.summary()
    db.close()
    if want_json:
        _jprint({"series": rows})
        return 0
    if not rows:
        print("no tracked series. add one:  webnovel-audio series add <fiction-url>")
    for r in rows:
        flag = "" if r["enabled"] else "  (sync disabled)"
        print(f"{r['title']}{flag}")
        stages = " · ".join(f"{k} {v}" for k, v in r["stages"].items() if v)
        print(f"  {r['chapters']} chapters · {stages}")
        print(f"  {r['pending']} outstanding   [{r['slug']}]  {r['url']}")
    return 0


def _cmd_sync(args) -> int:
    import json as _json
    import sys

    from . import sync

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    if want_json:
        try:                                    # stream events line-by-line down a pipe
            sys.stdout.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

    def emit(d):
        print(_json.dumps(d, ensure_ascii=False), flush=True)

    emit = emit if want_json else None

    # `sync` is the one command that can silently start a multi-hour job off a
    # short line, so say how big it is first. Never prompts when the answer
    # can't be read (--json, --yes, --dry-run, or a non-tty: cron/systemd).
    if not (want_json or args.dry_run or args.yes):
        est = sync.estimate_render(cfg, args.key, limit=args.limit)
        if est["chapters"]:
            print(f"{est['chapters']} chapter(s) to render, "
                  f"~{sync.human_duration(est['seconds'])} of CPU:")
            for s in est["series"]:
                note = "" if s["from_samples"] else "  (no samples yet — rough)"
                print(f"  {s['title']:<32} {s['chapters']:>4} ch  "
                      f"~{sync.human_duration(s['seconds'])}{note}")
            if sys.stdin.isatty() and input("continue? [Y/n] ").strip().lower() in ("n", "no"):
                print("nothing done.")
                return 0
        else:
            print("nothing outstanding.")
            return 0

    try:
        with sync.sync_lock(cfg):
            res = sync.run_sync(cfg, args.key, limit=args.limit, dry_run=args.dry_run,
                                backend=args.backend or cfg.synth.backend,
                                refresh_first=not args.no_refresh,
                                log=(lambda *_: None) if want_json else print, emit=emit)
    except sync.SyncLocked as exc:
        if want_json:
            emit({"event": "locked", "path": exc.path, "message": str(exc)})
        else:
            print(f"! {exc} — wait for it to finish (check `ps` for a stray `sync`).")
        return 2
    if not want_json:
        if args.dry_run:
            print(f"\nsync (dry run): {res.skipped} chapter(s) would render")
        else:
            print(f"\nsync: {res.rendered} rendered, {res.errors} error(s)")
    return 1 if res.errors else 0


def _cmd_config(args) -> int:
    import shutil

    if getattr(args, "action", "show") == "edit":
        path = args.config
        if not os.path.exists(path):
            here = os.path.dirname(__file__)
            ex = os.path.abspath(os.path.join(here, "..", "..", "config.example.toml"))
            if os.path.exists(ex):
                shutil.copy(ex, path)
                print(f"created {path} from config.example.toml")
        return _editor_open(path)

    cfg = Config.load(args.config)
    project = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    venv_bin = os.path.join(project, ".venv", "bin", "webnovel-audio")
    info = {
        "config_path": args.config if os.path.exists(args.config) else None,
        "executable": venv_bin if os.path.exists(venv_bin)
        else (shutil.which("webnovel-audio") or "webnovel-audio"),
        "project_dir": project,
        "state_db": os.path.abspath(os.path.expanduser(cfg.royalroad.state_db)),
        "library_dir": os.path.abspath(os.path.expanduser(cfg.royalroad.library_dir)),
        "lexicon_dir": os.path.abspath(os.path.expanduser(cfg.general.lexicon_dir or "data/lexicons")),
        "base_lexicon": (os.path.abspath(os.path.expanduser(cfg.general.base_lexicon))
                         if cfg.general.base_lexicon else ""),
        "series_config_dir": os.path.abspath(os.path.expanduser(
            cfg.general.series_config_dir or "data/series")),
        "models_dir": cfg.general.models_dir,
        "request_delay": cfg.royalroad.request_delay,
        "backend": cfg.synth.backend,
        "narrator": cfg.cast.narrator or cfg.voices.narrator,
        "voices": _EN_VOICES,
        "serve_port": cfg.serve.port,
    }
    if getattr(args, "json", False):
        _jprint(info)
    else:
        for k, v in info.items():
            print(f"{k:14} {v}")
    return 0


def _cmd_login(args) -> int:
    """Store (or clear) a royalroad.com session cookie.

    Deliberately does not verify: that would need an account page whose markup
    drifts, and a bad cookie is reported honestly where it matters — `fetch`
    says so when a chapter is locked.
    """
    from .royalroad import (SESSION_FILE, clear_session, load_session,
                            parse_cookie_header, parse_cookies_txt, save_session)

    if args.logout:
        clear_session()
        print("session cleared.")
        return 0
    if args.status:
        cookies = load_session()
        if cookies:
            import time
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(os.path.getmtime(SESSION_FILE)))
            print(f"{len(cookies)} cookie(s) stored {when} -> {SESSION_FILE}")
            print("  (not verified — a stale cookie shows up as a failed fetch)")
        else:
            print("no session stored; chapter fetches are anonymous.")
        return 0 if cookies else 1

    if args.cookies_file:
        cookies = parse_cookies_txt(args.cookies_file)
    elif args.cookie:
        cookies = parse_cookie_header(args.cookie)
    else:
        print("paste the Cookie header for royalroad.com (from your browser devtools):")
        cookies = parse_cookie_header(input().strip())
    if not cookies:
        print("no cookies parsed.")
        return 1
    save_session(cookies)
    print(f"saved {len(cookies)} cookie(s) for royalroad.com -> {SESSION_FILE}")
    return 0


def _cmd_serve(args) -> int:
    import sys

    from .serve import serve

    try:                       # so a parent (the control UI) sees startup output live
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    serve(Config.load(args.config), host=args.host, port=args.port)
    return 0


def _cmd_book(args) -> int:
    from . import sync

    lo, hi = _parse_range(getattr(args, "range", ""))
    out = sync.make_book(Config.load(args.config), args.key,
                         first=lo, last=hi, out=args.out)
    print(f"wrote {out}  ({os.path.getsize(out) / 1e6:.1f} MB)")
    return 0


def _cmd_feed(args) -> int:
    from . import sync

    paths = sync.write_feeds(Config.load(args.config), args.key,
                             out_dir=args.out, base_url=args.base_url or "")
    if not paths:
        print("no tracked series.")
        return 1
    return 0


def _cmd_schedule(args) -> int:
    import shutil

    project = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    venv_bin = os.path.join(project, ".venv", "bin", "webnovel-audio")
    if os.path.exists(venv_bin):
        exec_start = f"{venv_bin} sync --yes"
    else:
        exec_start = f"{shutil.which('uv') or 'uv'} run --project {project} webnovel-audio sync --yes"
    service = (
        "[Unit]\nDescription=webnovel-audio: render new Royal Road chapters\n\n"
        "[Service]\nType=oneshot\n"
        f"WorkingDirectory={project}\nExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\nDescription=Run webnovel-audio sync\n\n"
        f"[Timer]\nOnCalendar={args.calendar}\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    if not args.install:
        print("# webnovel-audio-sync.service\n" + service)
        print("# webnovel-audio-sync.timer\n" + timer)
        print("# write these to ~/.config/systemd/user/ then:")
        print("#   systemctl --user daemon-reload && systemctl --user enable --now webnovel-audio-sync.timer")
        return 0
    d = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(d, exist_ok=True)
    for name, body in (("webnovel-audio-sync.service", service),
                       ("webnovel-audio-sync.timer", timer)):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    print(f"installed unit files in {d}")
    print("run:  systemctl --user daemon-reload && systemctl --user enable --now webnovel-audio-sync.timer")
    return 0


def main(argv=None) -> int:
    import sys

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["--help"]                 # bare invocation -> help, never the UI

    ap = argparse.ArgumentParser(
        prog="webnovel-audio",
        description="Offline web-novel TTS, CPU-first (Ryzen 8745HS / no CUDA).",
        epilog="pipeline verbs take <target> [range]; an explicit range means "
               "'do exactly these', no range means 'do what's outstanding'.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _cfg(p):
        p.add_argument("-c", "--config", default=_default_config())

    def _cfg_json(p):
        _cfg(p)
        p.add_argument("--json", action="store_true", help="machine-readable output")

    def _stage_parser(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("target", help="tracked series (slug/id/title), or a file / URL")
        p.add_argument("range", nargs="?", default="",
                       help="N | N-M | N- | -M   (omit: whatever is outstanding)")
        p.add_argument("--limit", type=int, help="stop after N chapters")
        _cfg_json(p)
        return p

    # -- pipeline ----------------------------------------------------------
    f = _stage_parser("fetch", "download chapter source into the raw cache")
    f.add_argument("-o", "--out", help="output path (file/URL target only)")
    f.set_defaults(func=_stage_cmd("fetched"))

    p_ = _stage_parser("parse", "raw -> blocks -> readable .md")
    p_.add_argument("--explain", action="store_true",
                    help="print how it parsed instead of writing (file target)")
    p_.set_defaults(func=_stage_cmd("parsed"))

    ck = sub.add_parser("check", help="cast / heteronyms / unknown names for a range")
    ck.add_argument("target", help="tracked series, or a file / URL")
    ck.add_argument("range", nargs="?", default="",
                    help="N | N-M   (omit: the first [cast] seed_chapters)")
    ck.add_argument("--top", type=int, default=15,
                    help="how many unknown-name candidates to list (default 15)")
    ck.add_argument("--context", type=int, default=6,
                    help="words of context around a flagged heteronym (default 6)")
    _cfg_json(ck)
    ck.set_defaults(func=_cmd_check)

    rd = _stage_parser("render", "synthesize -> mastered .opus")
    rd.add_argument("-o", "--out", help="output path (file/URL target only)")
    rd.add_argument("--backend", choices=["kokoro", "null"])
    rd.add_argument("--dry-run", action="store_true", help="plan only, no audio")
    rd.set_defaults(func=_stage_cmd("rendered"))

    sy = sub.add_parser("sync", help="refresh + render everything outstanding")
    sy.add_argument("key", nargs="?", help="one series, or all enabled if omitted")
    sy.add_argument("--limit", type=int, help="max chapters per series this run")
    sy.add_argument("--backend", choices=["kokoro", "null"])
    sy.add_argument("--dry-run", action="store_true", help="list what would render")
    sy.add_argument("--no-refresh", action="store_true", help="skip re-fetching chapter lists")
    sy.add_argument("-y", "--yes", action="store_true",
                    help="skip the size estimate + confirmation (for scripts/timers)")
    _cfg_json(sy)
    sy.set_defaults(func=_cmd_sync)

    # -- series ------------------------------------------------------------
    se = sub.add_parser("series", help="track and manage series")
    se_sub = se.add_subparsers(dest="action")
    a = se_sub.add_parser("add", help="start tracking (metadata only)")
    a.add_argument("url", help="fiction page URL or id")
    a.add_argument("--from", dest="frm", default="start",
                   help="mark chapters up to here as already-read and skip them: "
                        "start (default) | latest | <N> | <chapter-url>")
    _cfg_json(a)
    for name, helptext in (("list", "all tracked series"),):
        _cfg_json(se_sub.add_parser(name, help=helptext))
    sh = se_sub.add_parser("show", help="one series in detail")
    sh.add_argument("key")
    _cfg_json(sh)
    for name in ("enable", "disable"):
        q = se_sub.add_parser(name, help=f"{name} this series for `sync`")
        q.add_argument("key")
        _cfg_json(q)
    fg = se_sub.add_parser("forget", help="untrack a series")
    fg.add_argument("key")
    fg.add_argument("--purge", action="store_true", help="also delete its library files")
    _cfg_json(fg)
    rf = se_sub.add_parser("refresh", help="re-fetch chapter lists")
    rf.add_argument("key", nargs="?")
    _cfg_json(rf)
    _cfg_json(se)
    se.set_defaults(func=_cmd_series, action=None, frm="start", purge=False)

    # -- state (plumbing) ---------------------------------------------------
    stt = sub.add_parser("state", help="the per-chapter stage machine, by hand")
    stt_sub = stt.add_subparsers(dest="action")
    ss = stt_sub.add_parser("show", help="per-chapter stage table")
    ss.add_argument("key")
    ss.add_argument("range", nargs="?", default="")
    _cfg_json(ss)
    sx = stt_sub.add_parser("set", help="force a status")
    sx.add_argument("key")
    sx.add_argument("range")
    sx.add_argument("status", help="new | fetched | parsed | rendered | skipped")
    _cfg_json(sx)
    sr = stt_sub.add_parser("reset", help="errored chapters -> new, to retry")
    sr.add_argument("key")
    sr.add_argument("range", nargs="?", default="")
    _cfg_json(sr)
    _cfg_json(stt)
    stt.set_defaults(func=_cmd_state, action="show", range="")

    cs = sub.add_parser("cast", help="this series' voice assignments")
    cs_sub = cs.add_subparsers(dest="action")
    ce = cs_sub.add_parser("edit", help="open the series config in $EDITOR")
    ce.add_argument("key")
    _cfg_json(ce)
    cu = cs_sub.add_parser("update", help="merge in speakers found in a chapter range")
    cu.add_argument("key")
    cu.add_argument("range", nargs="?", default="",
                    help="N | N-M   (omit: the first [cast] seed_chapters)")
    cu.add_argument("--diff", action="store_true", help="show what it would add, write nothing")
    _cfg_json(cu)
    cw = cs_sub.add_parser("show", help="the effective cast, including unassigned")
    cw.add_argument("key")
    _cfg_json(cw)
    _cfg_json(cs)
    cs.set_defaults(func=_cmd_cast, action="show", range="", diff=False)

    # -- lexicon / config / voices ------------------------------------------
    lx = sub.add_parser("lex", help="pronunciation lexicons")
    lx_sub = lx.add_subparsers(dest="action")
    le = lx_sub.add_parser("edit", help="open a lexicon CSV in $EDITOR")
    le.add_argument("slug", nargs="?")
    le.add_argument("--base", action="store_true", help="the always-on _base.csv")
    _cfg(le)
    la = lx_sub.add_parser("add", help="append a row")
    la.add_argument("slug")
    la.add_argument("surface", help="word or phrase as it appears in the text")
    la.add_argument("respell", help="sound-it-out spelling, e.g. kay-lith")
    la.add_argument("--note")
    la.add_argument("--base", action="store_true", help="write to _base.csv instead")
    _cfg(la)
    li = lx_sub.add_parser("ignore", help="mark words as 'reads fine' so `check` stops listing them")
    li.add_argument("slug")
    li.add_argument("words", nargs="+")
    li.add_argument("--base", action="store_true", help="write to _base.csv instead")
    _cfg(li)
    ll = lx_sub.add_parser("list", help="effective entries (base + series)")
    ll.add_argument("slug", nargs="?")
    ll.add_argument("--base", action="store_true")
    _cfg_json(ll)
    _cfg_json(lx)
    lx.set_defaults(func=_cmd_lex, action="list", slug=None, base=False)

    co = sub.add_parser("config", help="resolved paths / edit config.toml")
    co_sub = co.add_subparsers(dest="action")
    _cfg_json(co_sub.add_parser("show", help="resolved paths"))
    _cfg(co_sub.add_parser("edit", help="open config.toml in $EDITOR"))
    _cfg_json(co)
    co.set_defaults(func=_cmd_config, action="show")

    pr = sub.add_parser("pron", help="how the TTS will pronounce text")
    pr.add_argument("text", nargs="*")
    pr.add_argument("--series", help="also apply that series' lexicon + overlay")
    pr.add_argument("--no-lexicon", action="store_true", help="raw g2p only")
    pr.add_argument("--check", action="store_true", help="audit every lexicon row")
    _cfg(pr)
    pr.set_defaults(func=_cmd_pron)

    vc = sub.add_parser("voices", help="list voices / render an audition file")
    vc_sub = vc.add_subparsers(dest="action")
    _cfg(vc_sub.add_parser("list", help="the 28 English voice ids"))
    vd = vc_sub.add_parser("demo", help="one chaptered .opus, each voice in turn")
    vd.add_argument("-o", "--out", default="voice-audition.opus")
    vd.add_argument("--text", help="custom sample paragraph")
    vd.add_argument("--only", help="comma-separated subset of voice ids")
    vd.add_argument("--pause", type=int, default=1200, help="ms between voices")
    vd.add_argument("--announcer", default="am_michael")
    _cfg(vd)
    _cfg(vc)
    vc.set_defaults(func=_cmd_voices, action="list", out="voice-audition.opus",
                    text=None, only=None, pause=1200, announcer="am_michael")

    # -- delivery + misc ----------------------------------------------------
    sv = sub.add_parser("serve", help="LAN podcast feeds + audio")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    _cfg(sv)
    sv.set_defaults(func=_cmd_serve)

    bk = sub.add_parser("book", help="stitch rendered chapters into a .m4b")
    bk.add_argument("key")
    bk.add_argument("range", nargs="?", default="", help="N-M (default: all rendered)")
    bk.add_argument("-o", "--out")
    _cfg(bk)
    bk.set_defaults(func=_cmd_book)

    fd = sub.add_parser("feed", help="write static RSS file(s)")
    fd.add_argument("key", nargs="?")
    fd.add_argument("--base-url", default="")
    fd.add_argument("--out-dir")
    _cfg(fd)
    fd.set_defaults(func=_cmd_feed)

    md = sub.add_parser("models", help="the Kokoro ONNX model files")
    md_sub = md.add_subparsers(dest="action")
    for name, helptext in (("fetch", "download (~350 MB, once)"), ("path", "where they live")):
        m = md_sub.add_parser(name, help=helptext)
        m.add_argument("--cache-dir")
    md.set_defaults(func=_cmd_models, action="fetch", cache_dir=None)

    lg = sub.add_parser("login", help="store a royalroad.com session cookie")
    lg.add_argument("--cookies-file", metavar="PATH",
                    help="Netscape cookies.txt exported from your browser")
    lg.add_argument("--cookie-header", dest="cookie", metavar="STR",
                    help="the raw Cookie: header value (devtools -> copy)")
    lg.add_argument("--status", action="store_true",
                    help="report whether a session is stored (does not verify it)")
    lg.add_argument("--logout", action="store_true")
    lg.set_defaults(func=_cmd_login)

    sc = sub.add_parser("schedule", help="systemd-user timer for nightly sync")
    sc.add_argument("--install", action="store_true", help="write the unit files")
    sc.add_argument("--calendar", default="*-*-* 03:00",
                    help="systemd OnCalendar expression (default: 3am daily)")
    _cfg(sc)
    sc.set_defaults(func=_cmd_schedule)

    ui = sub.add_parser("ui", help="launch the Tcl/Tk control UI")
    ui.add_argument("--python", action="store_true",
                    help="use Python's bundled Tcl/Tk instead of `wish`")
    ui.set_defaults(func=_cmd_ui)

    args = ap.parse_args(argv)
    return args.func(args)
