from __future__ import annotations

import os

# Keep ONNX Runtime / OpenMP from oversubscribing the mobile APU (8 cores).
os.environ.setdefault("OMP_NUM_THREADS", "6")

import argparse  # noqa: E402

from . import __version__  # noqa: E402
from .config import Config, resolve_data_path  # noqa: E402

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


def _fail(code: str, message: str, *, hint: str = "", json_mode: bool = False,
          rc: int = 1, **extra) -> int:
    """Report a failure the same way to humans and to programs.

    An agent driving this needs to branch on *what* went wrong, not parse
    English. `code` is the stable identifier; `message` is the human line;
    `hint` names the command that fixes it.
    """
    if json_mode:
        _jprint({"ok": False, "error": {"code": code, "message": message,
                                        "hint": hint, **extra}})
    else:
        import sys
        print(f"error [{code}]: {message}", file=sys.stderr)
        if hint:
            print(f"  try: {hint}", file=sys.stderr)
    return rc


def _no_series(key, json_mode: bool = False) -> int:
    return _fail("no_such_series", f"no tracked series matching {key!r}",
                 hint="webnovel-audio series list", json_mode=json_mode)


#: Reserved scope values. Sigil-prefixed so they can never collide with a series
#: slug, however a series comes to be named. `@base` is the shared lexicon;
#: `@all` is "every enabled series", which several commands already mean by
#: omitting the argument -- spelling it lets a caller say so on purpose.
_SCOPE_BASE = "@base"
_SCOPE_ALL = "@all"


def _nonempty(s: str) -> str:
    """An argparse `type` for a field where empty is meaningless.

    A quoted empty argument is a *present* token, so it binds positionally and
    passes every arity check -- which is how `lex add read "" red` used to file
    a rule under the empty surface. Rejecting it at the field is the cheap fix,
    and it stays correct now that the fields are named. Note this is deliberately
    not applied to `--respell`, where empty is the documented "reads fine" no-op.
    """
    if not s.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return s


def _series_or_all(cfg, key: str | None, want_json: bool):
    """Validate an optional series identifier. Returns the resolved key, or an int.

    The commands that take a *required* series all validated it; the four that
    took an optional one -- `sync`, `progress`, `retag`, `series refresh` -- did
    not, because "omitted, so act on all" and "given, but matching nothing" fell
    into the same branch. Optionality is what hid the gap. The result was output
    that read as reassuring: `sync <typo>` reported "nothing outstanding", which
    is what a fully caught-up series looks like.

    `None` and `@all` both mean every enabled series and pass through as None.
    Anything else must name something.
    """
    from .db import DB

    if not key or key == _SCOPE_ALL:
        return None
    db = DB(cfg.royalroad.state_db)
    try:
        row = db.get_series(key)
    finally:
        db.close()
    if not row:
        return _fail("no_such_series", f"no tracked series matching {key!r}",
                     hint=f"webnovel-audio series list, or {_SCOPE_ALL} "
                          "for every enabled series",
                     json_mode=want_json, reserved=[_SCOPE_ALL])
    return key


def _parse_range(s):
    """A chapter selection -> a list of (lo, hi) spans; None means open-ended.

        ""          -> []                    (no range: the declarative form)
        "5"         -> [(5, 5)]
        "5-9"       -> [(5, 9)]
        "4-"        -> [(4, None)]
        "-6"        -> [(None, 6)]
        "1-3,7,20-" -> [(1, 3), (7, 7), (20, None)]

    Comma lists matter for the UI: a treeview with `-selectmode extended` hands
    back scattered chapters, and this lets that become one command instead of a
    serialized pile of them (the flock would refuse concurrent runs anyway).
    """
    spans: list[tuple[int | None, int | None]] = []
    for part in (s or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            spans.append(((int(a) if a else None), (int(b) if b else None)))
        else:
            spans.append((int(part), int(part)))
    return spans


def _span_bounds(spans):
    """(min lo, max hi) across spans — for callers that need one contiguous
    range (`book`), or just 'is anything selected'."""
    if not spans:
        return None, None
    los = [lo for lo, _ in spans if lo is not None]
    his = [hi for _, hi in spans if hi is not None]
    open_lo = any(lo is None for lo, _ in spans)
    open_hi = any(hi is None for _, hi in spans)
    return (None if open_lo or not los else min(los),
            None if open_hi or not his else max(his))


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
        # These run for minutes to hours. Python block-buffers stdout whenever it
        # isn't a tty, so `render … > log` (or nohup) showed nothing at all until
        # the process exited. Line-buffer both output modes, not just --json.
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

        if _is_file_target(args.target):
            return _one_off(stage, args, cfg)

        spans = _parse_range(getattr(args, "range", ""))
        emit = (lambda d: print(_json.dumps(d, ensure_ascii=False), flush=True)) \
            if want_json else None
        try:
            with sync.sync_lock(cfg) if stage == "rendered" else _nullctx():
                res = sync.run_stage(
                    cfg, stage, args.target, spans=spans,
                    limit=getattr(args, "limit", None),
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
        rep = pipeline.render(args.target, out, cfg, backend=cfg.synth.backend,
                              dry_run=getattr(args, "dry_run", False))
        print(f"  audio       : {rep.out_path}" if rep.out_path else "  (dry run)")
        if rep.audio_seconds:
            print(f"  duration    : {rep.audio_seconds / 60:.1f} min")
            print(f"  realtime x  : {rep.realtime_factor:.1f}")
        if rep.total_segments:
            print(f"  segments    : {rep.total_segments} "
                  f"({rep.cached_segments} from cache)")
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
        fh.write(render_markdown(doc, stage="parse"))
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

    spans = _parse_range(getattr(args, "range", ""))
    report = sync.suggest_cast(cfg, args.target, spans=spans, context=args.context,
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
        from . import tagger as _tagger
        from .pipeline import _load_lexicon

        print("\nheteronyms — context-dependent, judge by ear:")
        # Mark the ones a part-of-speech rule already decides, so the list
        # reads as a to-do rather than a wall of words that may need nothing.
        _lex = _load_lexicon(cfg)
        _auto = _lex.covered() if _lex else set()
        print(f"  ({_tagger.describe(getattr(cfg.general, 'tagger', '') or _tagger.DEFAULT_MODEL)})")
        for h in report["heteronyms"]:
            mark = " [tagger]" if h["word"].lower() in _auto else ""
            print(f"  {h['word']:<10} x{h['count']:<3} \"...{h['context']}...\"{mark}")
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
        # espeak exits the process rather than raising when it cannot reach
        # its data, so this has to refuse first or not at all.
        from .espeak import require_usable
        require_usable("cannot phonemize:")
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
    want_json = getattr(args, "json", False)
    scope = getattr(args, "scope", None)

    # A scope that names nothing used to fall back to the base lexicon and still
    # print "with lexicon", so a typo'd slug produced a confident preview missing
    # exactly the rules you were checking. Validate it the way `lex` does.
    if scope and scope != _SCOPE_BASE:
        resolved = _resolve_scope(cfg, scope, want_json)
        if isinstance(resolved, int):
            return resolved
        _, _, slug = resolved
        cfg = _sync._series_cfg(cfg, slug, _bundle_dir_for(cfg, slug))

    lex = None if args.no_lexicon else _load_lexicon(cfg)

    if args.check and args.text:
        return _fail("text_ignored",
                     "--check audits the lexicon and does not read text",
                     hint="drop the text, or drop --check to sound it out",
                     json_mode=want_json, text=" ".join(args.text))

    if args.check:
        from .lexicon import Lexicon

        seen, rows, audit = set(), [], []
        for path in (cfg.general.base_lexicon, cfg.general.lexicon):
            if path and os.path.exists(path):
                rows += Lexicon.load(path).rules
        if not rows:
            if want_json:
                _jprint({"ok": True, "rules": []})
            else:
                print("no lexicon entries to check.")
            return 0
        for e in rows:
            key = (e.surface, e.pos)
            if key in seen:
                continue
            seen.add(key)
            raw = _phonemes(e.surface)
            tag = f"/{e.pos}" if e.pos else ""
            if not e.respell or e.respell == e.surface:
                audit.append({"surface": e.surface, "pos": e.pos, "phonemes": raw,
                              "respell": None, "unchanged": None})
                if not want_json:
                    print(f"  {e.surface + tag:20} {raw}   (no respell)")
                continue
            new = _phonemes(e.respell)
            audit.append({"surface": e.surface, "pos": e.pos, "phonemes": raw,
                          "respell": e.respell, "respelled_phonemes": new,
                          "say": _gloss(new), "unchanged": new == raw})
            if not want_json:
                flag = "  = unchanged" if new == raw else ""
                print(f"  {e.surface + tag:20} {raw}  ->  {new}   [{_gloss(new)}]{flag}")
        if want_json:
            _jprint({"ok": True, "rules": audit})
        return 0

    text = " ".join(args.text).strip()
    if not text:
        return _fail("no_text", "no text to sound out",
                     hint="webnovel-audio pron Montgomery",
                     json_mode=want_json)
    raw = _phonemes(text)
    out = {"ok": True, "text": text, "scope": scope,
           "phonemes": raw, "say": _gloss(raw), "changed": False}
    if not want_json:
        print(f"text      : {text}")
        print(f"phonemes  : {raw}")
        print(f"≈ say     : {_gloss(raw)}")
    if lex is not None:
        from . import tagger as _tagger
        applied = lex.apply(text, _tagger.load(getattr(cfg.general, "tagger", "")
                                               or _tagger.DEFAULT_MODEL))
        if applied != text:
            new = _phonemes(applied)
            out.update(changed=True, applied=applied,
                       applied_phonemes=new, applied_say=_gloss(new))
            if not want_json:
                print(f"\nwith lexicon : {applied}")
                print(f"phonemes     : {new}")
                print(f"≈ say        : {_gloss(new)}")
        elif not want_json:
            print("\n(no lexicon entry changes this text)")
    if want_json:
        _jprint(out)
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
        if getattr(args, "json", False):
            _jprint({"ok": True, "voices": voices,
                     "groups": {_ACCENT[pre]: [v for v in voices
                                               if v.startswith(pre + "_")]
                                for pre in ("am", "af", "bm", "bf")}})
            return 0
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


#: Reports the Tk build an interpreter would give the UI: its patchlevel and
#: how many font families it can see. The family count is the tell. A Tk
#: compiled without Xft/fontconfig — which is what python-build-standalone
#: ships, and therefore what uv-managed interpreters have — falls back to the
#: X11 core font `fixed` and reports exactly one family. Nothing can be
#: configured to fix that; the only cure is a different interpreter, which is
#: what _pick_tk_host goes looking for.
#:
#: Measured on uv's cpython-3.14.7 (Tk 9.0.4), because "one font family"
#: reads as milder than it is:
#:
#:   - `font create big -family Helvetica -size 24 -weight bold` resolves to
#:     `-family fixed -size 10 -weight normal`. Size and weight requests are
#:     not approximated, they are discarded — no bold and no headings
#:     anywhere in the UI, not just in prose.
#:   - Missing glyphs render at ZERO width rather than as a box: "em—dash"
#:     (7 chars) measures 6 glyphs wide, "curly’quote" (11) measures 10,
#:     "ellipsis…" (9) measures 8. The character is silently absent from the
#:     line, which is worse than a visible tofu because nothing marks the gap.
#:
#: The preference survives even though Tk 9 would otherwise be worth having:
#: its ttk `default` theme reads the X resource database natively, which is
#: exactly what this project's colour handling wants (see ui/xres.tcl). It is
#: not worth unreadable prose, and there is no third option here — Arch ships
#: tk 8.6.16 and has no tk9 package, so on this machine Tk 9 and Xft are
#: mutually exclusive. To see it: `ui --interpreter <a uv python>`.
_TK_PROBE = (
    "import tkinter;r=tkinter.Tk();r.withdraw();"
    "print(r.tk.eval('info patchlevel'),"
    "len(r.tk.splitlist(r.tk.eval('font families'))))"
)


def _tk_probe(python: str):
    """(tk_version, font_families) for `python`, or None if it has no usable Tk."""
    import subprocess
    try:
        p = subprocess.run([python, "-c", _TK_PROBE], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    parts = p.stdout.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1])


def _pick_tk_host(forced: str = ""):
    """(interpreter, warning) for hosting the UI.

    Prefers this interpreter and probes no further when its Tk is healthy, so
    the common case costs one subprocess. Only a crippled Tk sends us looking
    at the system Python, which on a distro build links Xft and renders
    properly.
    """
    import shutil
    import sys

    if forced:
        return forced, ""

    cands = [sys.executable]
    for c in (shutil.which("python3"), "/usr/bin/python3"):
        if c and c not in cands:
            cands.append(c)

    first = None
    for py in cands:
        got = _tk_probe(py)
        if got is None:
            continue
        version, families = got
        if first is None:
            first = (py, version, families)
        if families > 1:
            warn = ""
            if py != sys.executable:
                warn = (f"note: hosting the UI on {py} (Tk {version}); "
                        f"{sys.executable} has a Tk with no Xft, which renders "
                        f"text without quotes or dashes.")
            return py, warn
    if first is None:
        return None, ""
    py, version, families = first
    return py, (f"warning: Tk {version} here sees {families} font family and "
                f"will render text in the X11 'fixed' bitmap font — apostrophes, "
                f"quotes and dashes will be missing. Install a Python with an "
                f"Xft-enabled Tk (Arch: pacman -S tk python) for readable text.")


def _cmd_ui(args) -> int:
    import shutil
    import sys

    project = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    tcl = os.path.join(project, "ui", "control.tcl")
    host = os.path.join(project, "ui", "host.py")
    for path in (tcl, host):
        if not os.path.exists(path):
            print(f"UI script not found: {path}", file=sys.stderr)
            return 1

    env = dict(os.environ)
    exe = shutil.which("webnovel-audio") or os.path.realpath(sys.argv[0])
    if os.path.basename(exe).startswith("webnovel-audio") and os.access(exe, os.X_OK):
        env.setdefault("WEBNOVEL_AUDIO", exe)

    python, warn = _pick_tk_host(getattr(args, "interpreter", "") or "")
    if python is None:
        print("no interpreter here has a working tkinter — install one "
              "(Arch: pacman -S tk python)", file=sys.stderr)
        return 1
    if warn:
        print(warn, file=sys.stderr)
    # replace this process with the UI: the host needs none of our imports
    os.execve(python, [python, host, tcl], env)


def _cmd_retag(args) -> int:
    """Refresh Vorbis tags on rendered chapters without re-encoding."""
    from .retag import retag_series

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    checked = _series_or_all(cfg, args.scope, want_json)
    if isinstance(checked, int):
        return checked
    r = retag_series(cfg, checked, dry_run=args.dry_run,
                     log=(lambda *_: None) if want_json else print)
    if getattr(args, "json", False):
        _jprint({"ok": True, **r})
    else:
        print(f"\n{r['retagged']} retagged, {r['failed']} failed "
              f"({r['skipped']} not rendered)")
    return 1 if r["failed"] else 0


def _cmd_schema(args) -> int:
    """Emit the command surface as JSON.

    So an agent can plan against this tool without being handed the whole
    `--help` tree — it lists every command, its arguments, types and choices.
    Built by walking argparse itself, so it cannot drift from the real parser.
    """
    ap = _build_parser()

    def walk(parser, path, help_text=""):
        sub = next((a for a in parser._actions
                    if isinstance(a, argparse._SubParsersAction)), None)
        opts, pos = [], []
        for a in parser._actions:
            if isinstance(a, (argparse._SubParsersAction, argparse._HelpAction)):
                continue
            item = {"name": a.dest, "help": a.help or "",
                    "required": bool(a.required)}
            if a.choices:
                item["choices"] = list(a.choices)
            if a.option_strings:
                item["flags"] = list(a.option_strings)
                item["takes_value"] = a.nargs != 0
                opts.append(item)
            else:
                item["nargs"] = a.nargs if a.nargs is not None else 1
                pos.append(item)
        node = {"path": path, "help": help_text or parser.description or "",
                "positional": pos, "options": opts}
        # Exclusivity is a constraint on a *set* of arguments, so it lives in
        # `_mutually_exclusive_groups` rather than on any single action -- and a
        # walker that only iterates `_actions` cannot see it. Without this an
        # agent reads `--scope` and `--no-lexicon` as freely combinable, which is
        # precisely the kind of thing a schema exists to rule out. A schema
        # should describe what makes a call valid, not only what parses.
        excl = [sorted(a.dest for a in g._group_actions)
                for g in parser._mutually_exclusive_groups if g._group_actions]
        if excl:
            node["mutually_exclusive"] = excl
        out = [node] if path else []
        if sub:
            node["subcommands"] = sorted(sub.choices)
            # argparse keeps each subparser's help on the parent, not the child
            helps = {a.dest: (a.help or "") for a in sub._choices_actions}
            for name, p2 in sub.choices.items():
                out += walk(p2, [*path, name], helps.get(name, ""))
        return out

    cmds = walk(ap, [])
    root = next((a for a in ap._actions
                 if isinstance(a, argparse._SubParsersAction)), None)
    _jprint({
        "tool": "webnovel-audio", "version": __version__,
        "conventions": {
            "target": "a tracked series (slug/id/title substring), or a path/URL "
                      "for a one-off",
            "range": "N | N-M | N- | -M | comma list (1-3,7,20-25); omitting it "
                     "means 'whatever is outstanding', giving it means "
                     "'exactly these, whatever their recorded state'",
            "json": "--json on series/state/cast/lex/check/config and the "
                    "pipeline verbs; sync/fetch/render stream one event per line",
            "exit_codes": {"0": "ok", "1": "failed", "2": "another render/sync "
                                                          "holds the lock"},
            "errors": "with --json a failure is "
                      '{"ok": false, "error": {"code", "message", "hint"}}',
        },
        "commands": sorted(root.choices) if root else [],
        "detail": cmds,
    })
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


_TAGGER_ALIASES = {"sm": "en_core_web_sm", "md": "en_core_web_md"}


def _tagger_write_config(path: str, model: str) -> bool:
    """Set (or clear) `general.tagger` in config.toml, preserving everything else.

    A text edit rather than a parse-and-dump: the file is hand-written and
    heavily commented, and round-tripping it through a TOML writer would throw
    all of that away.
    """
    import re

    from . import sync

    if not os.path.exists(path):
        return False
    text = open(path, encoding="utf-8").read()
    line = f"tagger = {sync._toml_str(model)}" if model else 'tagger = ""'
    rx = re.compile(r"^tagger\s*=.*$", re.M)
    if rx.search(text):
        text = rx.sub(line, text, count=1)
    elif re.search(r"^\[general\]\s*$", text, re.M):
        text = re.sub(r"^\[general\]\s*$", "[general]\n" + line, text, count=1, flags=re.M)
    else:
        text = f"[general]\n{line}\n\n" + text
    open(path, "w", encoding="utf-8").write(text)
    return True


def _cmd_tagger(args) -> int:
    """Install / inspect / remove the POS tagger the rules resolve against."""
    import sys

    from . import tagger
    from .pipeline import _load_lexicon

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    configured = getattr(cfg.general, "tagger", "")
    model = configured or tagger.DEFAULT_MODEL

    if args.action == "status":
        lex = _load_lexicon(cfg)
        rules = lex.rules if lex else []
        info = {"configured": configured, "model": model,
                "installed": tagger.available(model),
                "rules": len(rules),
                "pos_rules": sum(1 for r in rules if r.pos),
                "phrase_rules": sum(1 for r in rules if r.is_phrase)}
        if want_json:
            _jprint({"ok": True, **info})
            return 0
        print(f"model {model:<16}: {'installed' if info['installed'] else 'NOT INSTALLED'}")
        print(f"config general.tagger : {configured or '(default)'}")
        print(f"rules                 : {info['rules']} "
              f"({info['pos_rules']} keyed on a part of speech, "
              f"{info['phrase_rules']} phrase)")
        if not info["installed"]:
            print(f"\n  rendering will refuse until this is installed:"
                  f"\n  webnovel-audio tagger install --model {model}")
        return 0

    if args.action == "test":
        nlp = tagger.load(model)
        if nlp is None:
            return _fail("tagger_unavailable", f"{model} is not installed",
                         hint="webnovel-audio tagger install", json_mode=want_json)
        from .segment import _finish

        out = _finish(args.text, _load_lexicon(cfg), nlp)
        if want_json:
            _jprint({"ok": True, "before": args.text, "after": out,
                     "changed": out != args.text})
            return 0
        print(f"  in  : {args.text}")
        print(f"  out : {out}")
        if out == args.text:
            print("  (no rule applied)")
        else:
            print("  tags:", ", ".join(f"{t.text}/{t.pos_}" for t in nlp(args.text)
                                       if not t.is_punct and not t.is_space))
        return 0

    model = _TAGGER_ALIASES.get(getattr(args, "model", ""), getattr(args, "model", ""))

    if args.action == "remove":
        if not args.yes:
            if want_json or not sys.stdin.isatty():
                return _fail("needs_confirmation",
                             "remove uninstalls the spaCy model from this venv",
                             hint="webnovel-audio tagger remove --yes", json_mode=want_json)
            if input("uninstall spaCy + model and disable the tagger? [y/N] ").strip().lower() \
                    not in ("y", "yes"):
                print("aborted.")
                return 0
        target = model
        rc = _pip(["uninstall", target, "spacy"], check=False)
        _tagger_write_config(args.config, "")
        print(f"removed {target}; general.tagger cleared" if rc == 0
              else f"uninstall reported an error; general.tagger cleared anyway")
        return 0

    # -- install ---------------------------------------------------------
    model = model or tagger.DEFAULT_MODEL
    if model not in tagger.KNOWN_MODELS:
        return _fail("unknown_model", f"not a known model: {model}",
                     hint=f"one of: {', '.join(tagger.KNOWN_MODELS)}",
                     json_mode=want_json, valid=list(tagger.KNOWN_MODELS))
    if not args.yes:
        # This mutates the venv the renderer runs in. Doing it mid-render
        # would change pronunciations under a job already in flight.
        if want_json or not sys.stdin.isatty():
            return _fail("needs_confirmation",
                         f"install {model} into this venv (do not run during a render)",
                         hint="webnovel-audio tagger install --yes", json_mode=want_json)
        print(f"installs spacy + {model} into this venv (~{'15' if model.endswith('sm') else '57'} MB)")
        print("do NOT run this while a render is in flight — it changes pronunciations")
        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 0

    # Only one model is ever installed: switching just replaces it. The
    # download is small enough that keeping a cache of both is not worth the
    # extra state to reason about.
    for other in tagger.KNOWN_MODELS:
        if other != model and tagger.available(other):
            print(f"replacing {other}")
            _pip(["uninstall", other], check=False)

    if _pip(["install", "spacy"]) != 0:
        return _fail("install_failed", "could not install spacy", json_mode=want_json)
    url = _model_wheel(model)
    print(f"downloading {model} …")
    if _pip(["install", url]) != 0:
        return _fail("install_failed", f"could not download {model}",
                     hint="check network access", json_mode=want_json, url=url)
    if not tagger.available(model):
        return _fail("install_failed", f"{model} still not importable",
                     json_mode=want_json)
    if not _tagger_write_config(args.config, model):
        print(f"installed, but {args.config} not found — set general.tagger = \"{model}\" by hand")
        return 0
    print(f"\ninstalled {model}; general.tagger set in {args.config}")
    print('try: webnovel-audio tagger test "He read the book yesterday."')
    return 0


def _model_wheel(model: str) -> str:
    """The release wheel URL for `model`, matched to the installed spaCy.

    Deliberately not `spacy download`: that shells out to whichever installer
    it finds and resolves the *ambient* environment, so it will happily drop
    the model into the wrong venv. Installing the wheel through `_pip` keeps
    it pinned to this interpreter. Model releases track spaCy's minor version.
    """
    ver = "3.8"
    try:
        import spacy

        ver = ".".join(spacy.about.__version__.split(".")[:2])
    except Exception:                       # noqa: BLE001 - fall back to 3.8
        pass
    tag = f"{model}-{ver}.0"
    return ("https://github.com/explosion/spacy-models/releases/download/"
            f"{tag}/{tag}-py3-none-any.whl")


def _run(cmd: list[str], check: bool = True) -> int:
    import subprocess

    try:
        return subprocess.run(cmd).returncode
    except OSError:
        return 1


def _pip(argv: list[str], check: bool = True) -> int:
    """Install into the running interpreter's environment, uv first.

    `uv pip` is how this project is set up (`uv sync --extra kokoro`), but a
    plain venv must work too, so fall back to pip.
    """
    import shutil
    import sys

    if shutil.which("uv"):
        extra = ["--python", sys.executable]
        quiet = ["-q"] if argv[0] == "install" else []
        return _run(["uv", "pip", *argv[:1], *quiet, *extra, *argv[1:]], check)
    flags = ["-y"] if argv[0] == "uninstall" else []
    return _run([sys.executable, "-m", "pip", *argv[:1], *flags, *argv[1:]], check)


def _cmd_progress(args) -> int:
    """What a sync is doing right now. Read-only; safe to run mid-render."""
    import sys
    import time

    from . import progress

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    checked = _series_or_all(cfg, args.scope, want_json)
    if isinstance(checked, int):
        return checked
    try:
        snap = progress.snapshot(cfg, checked, recent=args.recent)
    except FileNotFoundError as exc:
        return _fail("no_state_db", f"no state database at {exc}",
                     hint="track a series first: webnovel-audio series add <url>",
                     json_mode=want_json)
    if want_json:
        _jprint({"ok": True, **snap})
        return 0
    if not args.watch:
        print(progress.render_text(snap))
        return 0

    # --watch redraws in place. It only ever reads, so it is safe to leave
    # running beside a sync for as long as you like.
    try:
        while True:
            sys.stdout.write("\x1b[H\x1b[2J")
            print(progress.render_text(snap))
            print(f"\n(refreshing every {args.watch}s — ctrl-c to stop)")
            sys.stdout.flush()
            time.sleep(args.watch)
            snap = progress.snapshot(cfg, args.scope or None, recent=args.recent)
    except KeyboardInterrupt:
        print()
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


def _bundle_dir_for(cfg: Config, slug: str) -> str:
    """Look the series up so a relocated bundle resolves. Falls back to the
    library layout for a slug that isn't tracked."""
    from . import bundle
    from .db import DB
    db = DB(cfg.royalroad.state_db)
    try:
        s = db.get_series(slug)
        return bundle.bundle_dir(cfg, s) if s else os.path.join(
            os.path.expanduser(cfg.royalroad.library_dir), slug)
    finally:
        db.close()


def _resolve_scope(cfg, scope: str | None, want_json: bool):
    """A `--scope` value -> (base_path, target_path, slug) or an exit code.

    An unknown slug used to be accepted silently: the old `_lex_paths` built a
    path for it and the write landed in a bundle for a series that does not
    exist, exit 0. An identifier that names nothing is an error, so it is checked
    here against the tracked set -- the same rule the rest of the CLI already
    applies via `_no_series`.
    """
    from .db import DB

    base = os.path.expanduser(cfg.general.base_lexicon or "data/lexicons/_base.csv")
    if scope is None or scope == _SCOPE_BASE:
        return base, base, None

    db = DB(cfg.royalroad.state_db)
    try:
        row = db.get_series(scope)
    finally:
        db.close()
    if not row:
        return _fail(
            "no_such_scope",
            f"no tracked series matching {scope!r}",
            hint=f"webnovel-audio series list, or --scope {_SCOPE_BASE} "
                 "for the shared lexicon",
            json_mode=want_json, scope=scope, reserved=[_SCOPE_BASE])

    from . import bundle, sync
    slug = sync._dir_slug(row)
    target = bundle.resolve(cfg, slug, _bundle_dir_for(cfg, slug), "lexicon")
    return base, target, slug


from .lexicon import HEADER as _LEX_HEADER


def _lex_header_for(path: str, slug: str | None, cfg: Config) -> str:
    """Header for a lexicon being created from scratch. A per-series file gets
    the named, self-documenting starter so it is identifiable in an editor;
    the base lexicon keeps its bare header."""
    from .lexicon import Lexicon
    if not slug:
        return _LEX_HEADER
    return Lexicon.starter_text(slug, cfg.general.base_lexicon)


def _lex_promote(args, base: str, series: str) -> int:
    """Move one rule from a series lexicon to the always-on base file.

    A rule earns this once it turns out to be a generic g2p mistake rather
    than something particular to that series (`diviner`, not `Broadsky`).
    Reads and rewrites the series CSV by hand rather than through `csv`
    end-to-end, so the leading `#` comment block survives untouched.
    """
    import csv

    from .lexicon import Rule

    if not series or not os.path.exists(series):
        print(f"no lexicon file for {args.scope}")
        return 1

    with open(series, encoding="utf-8") as fh:
        raw_lines = fh.readlines()
    comments = [ln for ln in raw_lines if ln.lstrip().startswith("#")]
    data_lines = [ln for ln in raw_lines if not ln.lstrip().startswith("#")]
    if not data_lines:
        print(f"{series}: no rules")
        return 1

    rows = list(csv.DictReader(data_lines))          # data_lines[0] is the header
    target_pos, _, target_lemma = args.pos.strip().partition("+")
    target_pos, target_lemma = target_pos.strip(), target_lemma.strip().lower()

    matches = []
    for i, row in enumerate(rows):
        surface = (row.get("surface") or "").strip()
        if surface.lower() != args.surface.lower():
            continue
        rule = Rule.parse(surface, row.get("pos") or "", row.get("respell") or "",
                          row.get("notes") or "")
        if args.pos and (rule.pos != target_pos or rule.lemma != target_lemma):
            continue
        matches.append((i, rule))

    if not matches:
        print(f"no rule for {args.surface!r} in {series}"
              + (f" with pos {args.pos!r}" if args.pos else ""))
        return 1
    if len(matches) > 1:
        opts = ", ".join(r.pos or "(any)" for _, r in matches)
        print(f"{args.surface!r} has multiple rules in {series}: {opts}\n"
              f"  disambiguate:  webnovel-audio lex promote "
              f"--scope {args.scope} --surface {args.surface} --pos <POS>")
        return 1
    idx, rule = matches[0]

    base_exists = os.path.exists(base)
    if base_exists:
        from .lexicon import Lexicon as _Lexicon
        dupe = any(r.surface.lower() == rule.surface.lower() and r.pos == rule.pos
                  and r.lemma == rule.lemma for r in _Lexicon.load(base).rules)
        if dupe and not args.force:
            tag = f" ({rule.pos})" if rule.pos else ""
            print(f"{base} already has a rule for {rule.surface!r}{tag} — edit it "
                  "there directly, or pass --force to add a duplicate row")
            return 1

    os.makedirs(os.path.dirname(os.path.abspath(base)) or ".", exist_ok=True)
    with open(base, "a", newline="", encoding="utf-8") as fh:
        if not base_exists:
            fh.write(_LEX_HEADER)
        pos_field = f"{rule.pos}+{rule.lemma}" if rule.lemma else rule.pos
        csv.writer(fh, lineterminator="\n").writerow(
            [rule.surface, pos_field, rule.respell, rule.notes])

    del data_lines[idx + 1]                            # +1: header at index 0
    with open(series, "w", newline="", encoding="utf-8") as fh:
        fh.writelines(comments + data_lines)

    tag = f" ({rule.pos})" if rule.pos else ""
    print(f"promoted {rule.surface}{tag} -> {base}\n  removed from {series}")
    return 0


def _cmd_lex(args) -> int:
    from .lexicon import Lexicon

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    scope = getattr(args, "scope", None)

    resolved = _resolve_scope(cfg, scope, want_json)
    if isinstance(resolved, int):          # validation failed; it already reported
        return resolved
    base, path, slug = resolved
    is_base = slug is None

    if args.action == "edit":
        return _editor_open(path, _LEX_HEADER)

    if args.action == "promote":
        if is_base:
            return _fail("nothing_to_promote",
                         f"--scope {_SCOPE_BASE} is already the base lexicon",
                         hint="promote from a series: --scope <slug>",
                         json_mode=want_json)
        return _lex_promote(args, base, path)

    if args.action == "ignore":
        # A row with a blank `respell` is already a no-op substitution that
        # `surfaces()` counts as known — so "I looked at this and it reads fine"
        # needs no new machinery, and shrinks the `check` report next time.
        # Same idea as codespell's ignore-list / cspell's custom dictionary.
        import csv
        exists = os.path.exists(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            if not exists:
                fh.write(_lex_header_for(path, slug, cfg))
            w = csv.writer(fh, lineterminator="\n")   # LF; csv defaults to CRLF
            for word in args.words:
                w.writerow([word, "", "", "reads fine as-is"])
        if want_json:
            _jprint({"ok": True, "file": path, "scope": scope,
                     "ignored": list(args.words)})
        else:
            print(f"{path}: {len(args.words)} word(s) marked fine as-is")
        return 0

    if args.action == "add":
        import csv
        exists = os.path.exists(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            if not exists:
                fh.write(_lex_header_for(path, slug, cfg))
            csv.writer(fh, lineterminator="\n").writerow(
                [args.surface, args.pos or "", args.respell, args.note or ""])
        if want_json:
            _jprint({"ok": True, "file": path, "scope": scope,
                     "surface": args.surface, "pos": args.pos or "",
                     "respell": args.respell, "note": args.note or ""})
        else:
            pos = f" ({args.pos})" if args.pos else ""
            print(f"{path}: {args.surface}{pos} -> {args.respell}")
        return 0

    # list: the merged, effective view. `path == base` when no series scope was
    # given, so dedupe rather than loading the base file twice.
    paths = [p for p in dict.fromkeys((base, path)) if p and os.path.exists(p)]
    if not paths:
        print("no lexicon files yet")
        return 0
    lex = Lexicon.load_many(paths)
    if want_json:
        _jprint({"rules": [vars(r) for r in lex.rules], "files": paths})
        return 0
    for r in sorted(lex.rules, key=lambda r: (r.surface.lower(), r.pos)):
        pos = f"{r.pos}+{r.lemma}" if r.lemma else (r.pos or "any")
        print(f"  {r.surface:<22} {pos:<10} {r.respell or '(no respell)'}"
              f"{'   # ' + r.notes if r.notes else ''}")
    print(f"\n{len(lex.rules)} rule(s) from: {', '.join(paths)}")
    return 0


def _series_config_path(cfg: Config, slug: str) -> str:
    from . import bundle
    return bundle.resolve(cfg, slug, _bundle_dir_for(cfg, slug), "config")


def _cmd_cast(args) -> int:
    """The series' voice assignments: edit / merge-in-new / show effective."""
    from . import sync
    from .db import DB

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    db = DB(cfg.royalroad.state_db)
    try:
        s = db.get_series(args.scope)
        if not s:
            return _no_series(args.scope, want_json)
        slug = sync._dir_slug(s)
    finally:
        db.close()
    path = _series_config_path(cfg, slug)

    if args.action == "edit":
        return _editor_open(path, sync.pin_defaults_text(cfg, slug))

    if args.action == "set":
        if args.voice and args.voice not in _EN_VOICES:
            return _fail("unknown_voice", f"no such voice: {args.voice}",
                         hint="run `webnovel-audio voices list`",
                         json_mode=want_json, valid=_EN_VOICES[:8])
        r = sync.set_cast_voice(cfg, slug, args.speaker, args.voice,
                               _bundle_dir_for(cfg, slug))
        if want_json:
            _jprint({"ok": True, **r})
        else:
            print(f"{r['action']}: {args.speaker} -> {args.voice or '(unassigned)'}"
                  f"  in {r['path']}")
        return 0

    if args.action == "show":
        scfg = sync._series_cfg(cfg, slug, _bundle_dir_for(cfg, slug))
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
    spans = _parse_range(getattr(args, "range", ""))
    report = sync.suggest_cast(cfg, args.scope, spans=spans, apply_cast=not args.diff,
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
        s = db.get_series(args.scope)
        if not s:
            return _no_series(args.scope, want_json)
        spans = _parse_range(getattr(args, "range", ""))

        if args.action == "set":
            if args.status not in STATUSES:
                return _fail("bad_status",
                             f"unknown status {args.status!r}",
                             hint=f"one of: {', '.join(STATUSES)}",
                             json_mode=want_json, valid=list(STATUSES))
            rows = db.select(s["id"], spans)
            n = db.set_status([c["id"] for c in rows], args.status)
            (_jprint if want_json else print)(
                {"ok": True, "changed": n, "status": args.status} if want_json
                else f"{s['title']}: {n} chapter(s) -> {args.status}")
            return 0

        if args.action == "reset":
            rows = [c for c in db.select(s["id"], spans) if c["status"] == "error"]
            n = db.set_status([c["id"] for c in rows], "new")
            (_jprint if want_json else print)(
                {"ok": True, "reset": n} if want_json
                else f"{s['title']}: {n} errored chapter(s) -> new")
            return 0

        rows = db.select(s["id"], spans)
        vmap = db.volume_map(s["id"])
        if want_json:
            _jprint({"slug": s["slug"], "title": s["title"], "chapters": [
                {"number": c["ord"] + 1, "title": c["title"], "status": c["status"],
                 "error_stage": c["error_stage"], "error": c["error"],
                 "duration_s": c["duration_s"], "url": c["url"],
                 "published_at": c["published_at"],
                 "fetched_at": c["fetched_at"], "parsed_at": c["parsed_at"],
                 "rendered_at": c["rendered_at"],
                 "render_started_at": c["render_started_at"],
                 "render_ended_at": c["render_ended_at"],
                 "audio_path": c["audio_path"], "text_path": c["text_path"],
                 "narrator": c["narrator"],
                 "volume_chapter": c["volume_chapter"],
                 "volume_index": (vmap.get(c["volume_rr_id"]) or {}).get("index"),
                 "volume_title": (vmap.get(c["volume_rr_id"]) or {}).get("title"),
                 "unlocked": bool(c["unlocked"])} for c in rows]})
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


def _cmd_cache_destructive(args, cfg, db, bundle, cache, want_json: bool) -> int:
    """`prune` and `clear`.

    Deleting needs explicit consent — `-y`, or a "yes" at a terminal. `--json`
    does NOT imply it: a machine caller that meant to delete can pass `-y`, and
    one that didn't should not lose data because it asked for JSON.

    Both take the sync lock: unlinking a key a running render just wrote is the
    one way these can corrupt rather than merely delete.
    """
    import sys

    from . import sync

    def run(dry):
        quiet = want_json or dry
        if args.action == "prune":
            return cache.prune(cfg, db, args.scope, stale=args.stale, dry_run=dry,
                               force=args.force,
                               log=(lambda *_: None) if quiet else print)
        return cache.clear(cfg, db, args.scope, dry_run=dry,
                           log=(lambda *_: None) if quiet else print)

    try:
        preview = run(True)                     # always look before leaping
    except cache.CacheError as exc:
        return _fail(exc.code, exc.message, hint=exc.hint, json_mode=want_json)

    n = preview["deleted"]
    if not n or args.dry_run:
        if want_json:
            # the probe always runs dry; only report it as such if that is
            # what was actually asked for
            _jprint({"ok": True, **preview, "dry_run": bool(args.dry_run)})
        elif not n:
            print("nothing to do.")
        else:
            print(f"would delete {n} entry(s), "
                  f"{bundle.human_bytes(preview['bytes'])}")
            for p in preview.get("paths", [])[:10]:
                print(f"    {p}")
            if n > 10:
                print(f"    … and {n - 10} more")
            print("\nnothing written. re-run without -n to apply.")
        return 0

    if not args.yes:
        summary = f"{n} entry(s), {bundle.human_bytes(preview['bytes'])}"
        cost = (f"   re-synthesizing what is already rendered would take "
                f"~{sync.human_duration(preview['resynth_seconds'])}"
                if args.action == "clear" else "")
        # a pipe, cron or systemd cannot answer; refuse rather than assume
        if want_json or not sys.stdin.isatty():
            return _fail("needs_confirmation",
                         f"{args.action} would delete {summary}",
                         hint=f"webnovel-audio cache {args.action} --yes",
                         json_mode=want_json, deleted=n, bytes=preview["bytes"])
        print(f"{summary}")
        if cost:
            print(cost)
        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 0

    try:
        with sync.sync_lock(cfg):
            res = run(False)
    except sync.SyncLocked as exc:
        return _fail("cache_locked", str(exc),
                     hint="wait for the render to finish", json_mode=want_json, rc=2)
    except cache.CacheError as exc:
        return _fail(exc.code, exc.message, hint=exc.hint, json_mode=want_json)
    if want_json:
        _jprint({"ok": True, **res})
    else:
        print(f"\ndeleted {res['deleted']} entry(s), "
              f"{bundle.human_bytes(res['bytes'])} freed")
    return 0


def _cmd_cache(args) -> int:
    """Segment-cache maintenance. Read-only unless you say otherwise."""
    from . import bundle, cache
    from .db import DB

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    db = DB(cfg.royalroad.state_db)
    try:
        if args.action in ("prune", "clear"):
            return _cmd_cache_destructive(args, cfg, db, bundle, cache, want_json)

        if args.action == "compact":
            if not want_json:
                print(("would compact" if args.dry_run else "compacting")
                      + f" into generation {cache.current_fingerprint(cfg)}:")
            r = cache.compact(cfg, db, args.scope, dry_run=args.dry_run,
                              log=(lambda *_: None) if want_json else print)
            if want_json:
                _jprint({"ok": True, **r})
            else:
                saved = r["bytes_before"] - r["bytes_after"]
                print(f"\n  {r['converted']} converted, {r['moved']} filed, "
                      f"{r['skipped']} already current"
                      + (f", {r['failed']} failed" if r["failed"] else ""))
                print(f"  {bundle.human_bytes(r['bytes_before'])} -> "
                      f"{bundle.human_bytes(r['bytes_after'])}"
                      f"   ({bundle.human_bytes(saved)} "
                      + ("would be " if args.dry_run else "") + "freed)")
                if args.dry_run:
                    print("\nnothing written. re-run without -n to apply.")
            return 0

        # status
        st = cache.status(cfg, db, args.scope)
        if want_json:
            _jprint(st)
            return 0
        print(f"segment cache   generation {st['current_generation']}  (current)")
        mat = st["material"]
        print("  " + "  ".join(f"{k}={mat[k]}" for k in
                               ("lang", "kokoro-onnx", "phonemizer",
                                "espeakng-loader") if mat.get(k)))
        print(f"\n  {st['files']:>7} segments   {bundle.human_bytes(st['bytes'])}"
              f"   avg {bundle.human_bytes(st['avg_bytes'])}")
        print(f"  {st['format']['flac']:>7} flac / {st['format']['wav']} wav")
        if len(st["generations"]) > 1 or any(
                not g["current"] for g in st["generations"].values()):
            print("\n  generations")
            for name, g in sorted(st["generations"].items()):
                mark = "  (current)" if g["current"] else "  (stale)"
                print(f"    {name:<16} {g['files']:>7} files  "
                      f"{bundle.human_bytes(g['bytes'])}{mark}")
        print("\n  by series")
        for row in st["series"]:
            extra = (f"   {row['reclaimable']} reclaimable "
                     f"({bundle.human_bytes(row['reclaimable_bytes'])})"
                     if row["reclaimable"] else "")
            print(f"    {row['slug']:<18} {row['files']:>7} files  "
                  f"{bundle.human_bytes(row['bytes'])}{extra}")
        if st["reclaimable"]:
            print(f"\n  {st['reclaimable']} entry(s), "
                  f"{bundle.human_bytes(st['reclaimable_bytes'])} unreachable"
                  "   -> cache prune")
        if st["format"]["wav"]:
            print(f"  {st['format']['wav']} float32 wav(s) "
                  "   -> cache compact")
        for m in st["mixed"]:
            gens = ", ".join(f"{k} x{v}" for k, v in m["generations"].items())
            print(f"\n  ! {m['slug']} rendered across "
                  f"{len(m['generations'])} synth generations  ({gens})")
            print(f"    -> render {m['slug']} --force   to unify")
        return 0
    except bundle.BundleError as exc:
        return _fail(exc.code, exc.message, hint=exc.hint, json_mode=want_json)
    finally:
        db.close()


def _cmd_series_bundle(args, cfg, db, bundle, want_json: bool) -> int:
    """The offline half of `series`: write, adopt, locate. No network."""
    if args.action == "export":
        rows = [db.get_series(args.scope)] if args.scope else db.list_series()
        if args.scope and not rows[0]:
            return _no_series(args.scope, want_json)
        out = []
        for s in filter(None, rows):
            res = bundle.sync_bundle(cfg, db, s)
            out.append({"slug": s["slug"], "title": s["title"], **res})
            if not want_json:
                mark = "" if res["ok"] else f"   ({res['reason']})"
                print(f"{s['title']}: {res['path']}{mark}")
        if want_json:
            _jprint({"ok": True, "exported": out})
        return 0

    if args.action == "migrate":
        rows = [db.get_series(args.scope)] if args.scope else db.list_series()
        if args.scope and not rows[0]:
            return _no_series(args.scope, want_json)
        if not want_json:
            print(("would migrate" if args.dry_run else "migrating")
                  + " into the bundle layout:")
        out = bundle.migrate(cfg, db, args.scope, dry_run=args.dry_run,
                             log=(lambda *_: None) if want_json else print)
        if want_json:
            _jprint({"ok": True, "dry_run": args.dry_run, "series": out})
        elif args.dry_run:
            print("\nnothing written. re-run without -n to apply.")
        return 0

    if args.action == "archive":
        res = bundle.archive(cfg, db, args.scope, out=args.out,
                             with_cache=args.with_cache,
                             log=(lambda *_: None) if want_json else print)
        if want_json:
            _jprint(res)
        return 0

    if args.action == "reclaim":
        r = bundle.reclaim_cache(cfg, db, dry_run=args.dry_run)
        if want_json:
            _jprint({"ok": True, "dry_run": args.dry_run, **r})
        else:
            verb = "would drop" if args.dry_run else "dropped"
            print(f"{verb} {r['unlinked']} duplicate cache link(s), "
                  f"{bundle.human_bytes(r['bytes'])} now held only by the bundles")
            print(f"  {r['kept']} entry(s) left in the shared cache "
                  f"(unclaimed, or not yet in every owning bundle)")
        return 0

    if args.action == "path":
        s = db.get_series(args.scope)
        if not s:
            return _no_series(args.scope, want_json)
        d, state = bundle.bundle_dir(cfg, s), bundle.status(cfg, s)
        if want_json:
            _jprint({"ok": True, "slug": s["slug"], "path": d, "bundle": state,
                     "uuid": s["uuid"]})
        else:
            print(d)            # bare, so `cd "$(… series path x)"` works
        return 0

    if args.action == "import":
        res = bundle.import_bundle(cfg, db, args.path, dry_run=args.dry_run)
        if want_json:
            _jprint(res)
        else:
            verb = {"add": "imported", "update": "updated"}[res["action"]]
            print(f"{'would ' if args.dry_run else ''}{verb}: {res['title']} "
                  f"({res['chapters']} chapters, {res['volumes']} volumes)")
            print(f"  {res['path']}")
        return 0

    # scan
    root = args.root or os.path.expanduser(cfg.royalroad.library_dir)
    results = bundle.scan(cfg, db, root, dry_run=args.dry_run)
    if want_json:
        _jprint({"ok": True, "root": root, "results": results})
        return 0
    if not results:
        print(f"no bundles under {root}")
    for r in results:
        if not r.get("ok"):
            print(f"  ! {r['path']}: [{r['code']}] {r['message']}")
        elif r["action"] == "missing":
            # never inferred as a deletion: an unplugged drive is not a decision
            print(f"  missing  {r['title']}  {r['path']}")
            print(f"           still tracked; `series forget {r['slug']}` to drop it")
        else:
            print(f"  {r['action']:<7}  {r['title']}  ({r['chapters']} chapters)")
    return 0


def _cmd_series(args) -> int:
    import sys

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
        checked = _series_or_all(cfg, args.scope, want_json)
        if isinstance(checked, int):
            return checked
        results = sync.refresh(cfg, checked,
                               log=(lambda *_: None) if want_json else print)
        if want_json:
            _jprint({"ok": True, "series": results or []})
        return 0

    if args.action in ("export", "import", "scan", "path", "migrate",
                       "archive", "reclaim"):
        from . import bundle
        db = DB(cfg.royalroad.state_db)
        try:
            return _cmd_series_bundle(args, cfg, db, bundle, want_json)
        except bundle.BundleError as exc:
            return _fail(exc.code, exc.message, hint=exc.hint, json_mode=want_json)
        finally:
            db.close()

    if args.action in ("enable", "disable", "priority", "forget", "show"):
        db = DB(cfg.royalroad.state_db)
        try:
            s = db.get_series(args.scope)
            if not s:
                (_jprint if want_json else print)(
                    {"ok": False, "error": f"no series matching {args.scope!r}"}
                    if want_json else f"no tracked series matching {args.scope!r}")
                return 1
            slug = sync._dir_slug(s)

            if args.action in ("enable", "disable"):
                on = args.action == "enable"
                db.set_enabled(s["id"], on)
                if want_json:
                    _jprint({"ok": True, "slug": s["slug"], "enabled": on,
                             "title": s["title"]})
                else:
                    print(f"{s['title']}: sync {'resumed' if on else 'paused'}")
                return 0

            if args.action == "priority":
                db.set_priority(s["id"], args.value)
                if want_json:
                    _jprint({"ok": True, "slug": s["slug"], "title": s["title"],
                             "priority": args.value,
                             "enabled": bool(s["enabled"])})
                else:
                    where = "" if s["enabled"] else "  (paused, so still not synced)"
                    print(f"{s['title']}: priority {args.value}{where}")
                return 0

            if args.action == "forget":
                from . import bundle

                # --purge destroys the rendered audio, the chapter text and the
                # hand-tuned lexicon, none of which the tool can regenerate.
                # Preview and confirm before touching any of it; a caller with
                # no tty gets a needs_confirmation error rather than a hang.
                if args.purge:
                    bdir = bundle.bundle_dir(cfg, s)
                    st = bundle.status(cfg, s)
                    if args.dry_run:
                        info = {"ok": True, "dry_run": True, "slug": s["slug"],
                                "would_remove": bdir, "bundle": st}
                        if want_json:
                            _jprint(info)
                        else:
                            print(f"would remove {bdir}")
                            print(f"would forget {s['title']}")
                        return 0
                    if not args.yes:
                        if want_json or not sys.stdin.isatty():
                            return _fail(
                                "needs_confirmation",
                                f"--purge deletes {bdir}, including the series "
                                "lexicon and every rendered chapter",
                                hint=f"webnovel-audio series forget {args.scope} "
                                     "--purge --yes (or --dry-run first)",
                                json_mode=want_json, path=bdir)
                        print(f"deletes {bdir}")
                        print("  chapters, rendered audio, cast config and lexicon")
                        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                            print("aborted.")
                            return 0

                # one directory holds everything now — before the bundle
                # layout this could not reach the segment cache at all
                pg = bundle.purge(cfg, db, s) if args.purge else None
                db.forget(s["id"])
                if want_json:
                    _jprint({"ok": True, "slug": s["slug"], "forgot": s["title"],
                             "purged": pg["path"] if pg and pg["removed"] else None,
                             "freed_bytes": pg["bytes"] if pg else 0})
                else:
                    if pg and pg["removed"]:
                        print(f"removed {pg['path']}"
                              f"  ({pg['files']} files, "
                              f"{bundle.human_bytes(pg['bytes'])})")
                    elif pg:
                        print(f"nothing at {pg['path']}")
                    print(f"forgot {s['title']}")
                return 0

            # show
            from . import bundle
            info = db.summary(args.scope)[0]
            bdir = bundle.bundle_dir(cfg, s)
            info["bundle"] = bundle.status(cfg, s)
            info["paths"] = {"dir": bdir,
                             "config": bundle.resolve(cfg, slug, bdir, "config"),
                             "lexicon": bundle.resolve(cfg, slug, bdir, "lexicon")}
            if want_json:
                _jprint(info)
                return 0
            from . import bundle
            print(f"{info['title']}  [{info['slug']}]  {'' if info['enabled'] else '(sync disabled)'}")
            print(f"  {info['url']}")
            bstate = bundle.status(cfg, s)
            print(f"  bundle: {bundle.bundle_dir(cfg, s)}"
                  + ("" if bstate == "ok" else f"   ({bstate})"))
            print(f"  {info['chapters']} chapters · " +
                  " · ".join(f"{k} {v}" for k, v in info["stages"].items() if v))
            if info["next"]:
                print(f"  next: #{info['next']['number']} {info['next']['title']}")
            return 0
        finally:
            db.close()

    # default: list
    from . import bundle
    db = DB(cfg.royalroad.state_db)
    rows = db.summary()
    db.close()
    for r in rows:
        r["bundle"] = bundle.status(cfg, r)
    if want_json:
        _jprint({"series": rows})
        return 0
    if not rows:
        print("no tracked series. add one:  webnovel-audio series add <fiction-url>")
    for r in rows:
        flag = "" if r["enabled"] else "  (sync disabled)"
        # only worth the noise once it has been set away from the default
        if r.get("priority", 100) != 100:
            flag += f"  (priority {r['priority']})"
        # a bundle deleted out from under us is a supported state, not an error
        flag += "" if r["bundle"] == "ok" else f"  (bundle {r['bundle']})"
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
    # Before anything else: a key that names nothing is an error, not an empty
    # result. The pre-flight estimate below drops an unresolved row silently and
    # reports "nothing outstanding", so the real guard inside run_stage was
    # unreachable for the ordinary path.
    checked = _series_or_all(cfg, args.scope, want_json)
    if isinstance(checked, int):
        return checked
    args.scope = checked
    try:                    # long-running: never block-buffer into a pipe or log
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    def emit(d):
        print(_json.dumps(d, ensure_ascii=False), flush=True)

    emit = emit if want_json else None

    # `sync` is the one command that can silently start a multi-hour job off a
    # short line, so say how big it is first. Never prompts when the answer
    # can't be read (--json, --yes, --dry-run, or a non-tty: cron/systemd).
    if getattr(args, "estimate", False):
        est = sync.estimate_render(cfg, args.scope, limit=args.limit)
        if want_json:
            _jprint({"ok": True, **est,
                     "human": sync.human_duration(est["seconds"])})
        else:
            print(f"{est['chapters']} chapter(s), ~{sync.human_duration(est['seconds'])}")
            for s in est["series"]:
                print(f"  {s['title']:<32} {s['chapters']:>4} ch  "
                      f"~{sync.human_duration(s['seconds'])}")
        return 0

    if not (want_json or args.dry_run or args.yes):
        est = sync.estimate_render(cfg, args.scope, limit=args.limit)
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
            res = sync.run_sync(cfg, args.scope, limit=args.limit, dry_run=args.dry_run,
                                backend=cfg.synth.backend,
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
        "lexicon_dir": resolve_data_path(cfg.general.lexicon_dir or "data/lexicons"),
        "base_lexicon": (os.path.abspath(os.path.expanduser(cfg.general.base_lexicon))
                         if cfg.general.base_lexicon else ""),
        "series_config_dir": resolve_data_path(
            cfg.general.series_config_dir or "data/series"),
        "models_dir": cfg.general.models_dir,
        "request_delay": cfg.royalroad.request_delay,
        "backend": cfg.synth.backend,
        "narrator": cfg.cast.narrator or cfg.voices.narrator,
        "voices": _EN_VOICES,
        "serve_port": cfg.serve.port,
    }
    # The one check here that runs something rather than reporting a setting:
    # espeak's data path is the failure that cannot report itself, because it
    # takes the process down before any handler sees it.
    from . import espeak as _espeak
    ep = _espeak.data_path()
    if ep:
        ok, detail = _espeak.probe()
        info["espeak_data"] = ep
        info["espeak_ok"] = ok
        if not ok:
            info["espeak_error"] = detail
    if getattr(args, "json", False):
        _jprint(info)
    else:
        for k, v in info.items():
            print(f"{k:14} {v}")
        if ep and not info.get("espeak_ok", True):
            print(f"\n! espeak-ng cannot read its data ({len(ep)} character path).")
            print(f"  {info.get('espeak_error', '')}")
            print("  Rendering will abort with no traceback. Move the project or")
            print("  its venv somewhere shorter and re-run `uv sync`.")
    return 0


def _cmd_login(args) -> int:
    """Store (or clear) a royalroad.com session cookie.

    Deliberately does not verify: that would need an account page whose markup
    drifts, and a bad cookie is reported honestly where it matters — `fetch`
    says so when a chapter is locked.
    """
    from .royalroad import (SESSION_FILE, clear_session, load_session,
                            parse_cookie_header, parse_cookies_txt, save_session)

    want_json = getattr(args, "json", False)

    if args.logout:
        clear_session()
        if want_json:
            _jprint({"ok": True, "stored": False, "cleared": True})
        else:
            print("session cleared.")
        return 0
    if args.status:
        cookies = load_session()
        if want_json:
            _jprint({"ok": bool(cookies), "stored": bool(cookies),
                     "cookies": len(cookies or []),
                     "file": SESSION_FILE if cookies else None,
                     "verified": False})
            return 0 if cookies else 1
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
        return _fail("no_cookies", "no cookies parsed from that input",
                     hint="export cookies.txt, or copy the Cookie: header value",
                     json_mode=want_json)
    save_session(cookies)
    if want_json:
        _jprint({"ok": True, "stored": True, "cookies": len(cookies),
                 "file": SESSION_FILE})
    else:
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

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    lo, hi = _span_bounds(_parse_range(getattr(args, "range", "")))
    out = sync.make_book(cfg, args.scope, first=lo, last=hi, out=args.out)
    size = os.path.getsize(out)
    if want_json:
        _jprint({"ok": True, "file": out, "bytes": size})
    else:
        print(f"wrote {out}  ({size / 1e6:.1f} MB)")
    return 0


def _cmd_feed(args) -> int:
    from . import sync

    cfg = Config.load(args.config)
    want_json = getattr(args, "json", False)
    checked = _series_or_all(cfg, args.scope, want_json)
    if isinstance(checked, int):
        return checked
    paths = sync.write_feeds(cfg, checked, out_dir=args.out_dir,
                             base_url=args.base_url or "")
    if not paths:
        return _fail("no_series", "no tracked series to write a feed for",
                     hint="webnovel-audio series add <url>", json_mode=want_json)
    if want_json:
        _jprint({"ok": True, "files": list(paths)})
    else:
        for pth in paths:
            print(f"wrote {pth}")
    return 0


def main(argv=None) -> int:
    import sys

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["--help"]                 # bare invocation -> help, never the UI

    args = _build_parser().parse_args(argv)
    return args.func(args)


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="webnovel-audio",
        description="Offline web-novel TTS, CPU-first (Ryzen 8745HS / no CUDA).",
        epilog="pipeline verbs take <target> [range]; an explicit range means "
               "'do exactly these', no range means 'do what's outstanding'.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # The one concrete home for these two defaults; every other level uses
    # SUPPRESS so it cannot clobber what an outer level parsed.
    ap.set_defaults(config=_default_config(), json=False)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Both of these are attached at more than one level of the subcommand tree
    # (`lex` and `lex list` each carry them), and argparse parses a subparser
    # into a *fresh* namespace, then copies every value it holds onto the
    # parent's. A concrete default on the inner parser therefore overwrote a
    # value the outer one had already parsed: `lex -c X list` accepted X and
    # silently used the default instead, and `lex --json list` printed human
    # text. SUPPRESS leaves the attribute unset when the flag is absent, so
    # nothing is copied and the outer value survives; the root supplies the
    # fallback via set_defaults below.
    #: The series identifier, spelled the same way everywhere it appears. Still
    #: positional (and still called `key`) because the Tcl UI passes it that way
    #: at a dozen call sites; centralised here so the help cannot drift between
    #: commands, and so changing the spelling later is one edit rather than 25.
    def _series_pos(p, *, required=True, what="operate on"):
        p.add_argument("--scope", dest="scope", required=required, metavar="SERIES",
                       help=f"the series to {what}: slug, id, or a title substring"
                            + ("" if required else
                               f"; omit or {_SCOPE_ALL} for every enabled series"))

    def _range_pos(p, *, default_means, required=False):
        p.add_argument("--range", default="", required=required, metavar="SPEC",
                       help=f"N | N-M | N- | -M | comma list (1-3,7). "
                            f"Omit for {default_means}")

    def _target_pos(p):
        # Deliberately *not* --scope: this one also accepts a path or URL for a
        # one-off, so it is a wider domain than "which tracked series" and gets
        # its own name rather than pretending to be the same argument.
        p.add_argument("--target", required=True, metavar="TARGET",
                       help="a tracked series (slug, id, or title substring), "
                            "or a path / URL to process as a one-off")

    def _dry(p, *, what, short=True):
        p.add_argument(*(("-n", "--dry-run") if short else ("--dry-run",)),
                       action="store_true", help=f"report what would {what}, "
                                                 "change nothing")

    def _yes(p, *, what):
        p.add_argument("-y", "--yes", action="store_true",
                       help=f"skip the confirmation before {what}")

    def _cfg(p):
        p.add_argument("-c", "--config", default=argparse.SUPPRESS, metavar="PATH",
                       help="config.toml to read (default: ./config.toml, else "
                            "the bundled config.example.toml)")

    def _cfg_json(p):
        _cfg(p)
        p.add_argument("--json", default=argparse.SUPPRESS, action="store_true",
                       help="machine-readable output")

    def _stage_parser(name, help_):
        p = sub.add_parser(name, help=help_)
        _target_pos(p)
        _range_pos(p, default_means="whatever is outstanding")
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
    _target_pos(ck)
    _range_pos(ck, default_means="the first [cast] seed_chapters")
    ck.add_argument("--top", type=int, default=15,
                    help="how many unknown-name candidates to list (default 15)")
    ck.add_argument("--context", type=int, default=6,
                    help="words of context around a flagged heteronym (default 6)")
    _cfg_json(ck)
    ck.set_defaults(func=_cmd_check)

    rd = _stage_parser("render", "synthesize -> mastered .opus")
    rd.add_argument("-o", "--out", help="output path (file/URL target only)")
    rd.add_argument("--dry-run", action="store_true", help="plan only, no audio")
    rd.set_defaults(func=_stage_cmd("rendered"))

    sy = sub.add_parser("sync", help="refresh + render everything outstanding")
    _series_pos(sy, required=False, what="sync")
    sy.add_argument("--limit", type=int, help="max chapters per series this run")
    sy.add_argument("--dry-run", action="store_true", help="list what would render")
    sy.add_argument("--no-refresh", action="store_true", help="skip re-fetching chapter lists")
    sy.add_argument("--estimate", action="store_true",
                    help="print how long it would take and exit (no rendering)")
    sy.add_argument("-y", "--yes", action="store_true",
                    help="skip the size estimate + confirmation (for scripts/timers)")
    _cfg_json(sy)
    sy.set_defaults(func=_cmd_sync)

    # -- series ------------------------------------------------------------
    se = sub.add_parser("series", help="track and manage series")
    se_sub = se.add_subparsers(dest="action")
    a = se_sub.add_parser("add", help="start tracking (metadata only)")
    a.add_argument("--url", required=True, metavar="URL",
                   help="fiction page URL or id")
    a.add_argument("--from", dest="frm", default="start",
                   help="mark chapters up to here as already-read and skip them: "
                        "start (default) | latest | <N> | <chapter-url>")
    _cfg_json(a)
    for name, helptext in (("list", "all tracked series"),):
        _cfg_json(se_sub.add_parser(name, help=helptext))
    sh = se_sub.add_parser("show", help="one series in detail")
    _series_pos(sh, what="describe")
    _cfg_json(sh)
    for name in ("enable", "disable"):
        q = se_sub.add_parser(name, help=f"{name} this series for `sync`")
        _series_pos(q, what=f"{name} for `sync`")
        _cfg_json(q)
    pr_ = se_sub.add_parser(
        "priority", help="render order: higher goes first (default 100)")
    _series_pos(pr_, what="reprioritise")
    pr_.add_argument("--priority", dest="value", required=True, type=int, metavar="N",
                     help="any integer; leave gaps (100, 200, …) so you can "
                          "insert between later. Pausing is separate — a "
                          "paused series keeps its priority")
    _cfg_json(pr_)
    fg = se_sub.add_parser("forget", help="untrack a series")
    _series_pos(fg, what="untrack")
    fg.add_argument("--purge", action="store_true",
                    help="also delete its library files: chapters, rendered "
                         "audio, cast config and the series lexicon")
    # --purge is the most destructive operation in this CLI and was the only
    # one with no guard at all, while `cache prune` -- which deletes segments
    # that can simply be re-rendered -- had the full set. Guard strength should
    # track what cannot be rebuilt.
    fg.add_argument("-n", "--dry-run", action="store_true",
                    help="report what --purge would delete, delete nothing")
    fg.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt")
    _cfg_json(fg)
    rf = se_sub.add_parser("refresh", help="re-fetch chapter lists")
    _series_pos(rf, required=False, what="re-fetch the chapter list of")
    _cfg_json(rf)

    # -- bundles: the offline half. No network, no renders.
    ex = se_sub.add_parser("export",
                           help="write manifest.toml + state.json into the bundle")
    _series_pos(ex, required=False, what="export")
    _cfg_json(ex)
    im = se_sub.add_parser("import", help="adopt a bundle directory into the state DB")
    im.add_argument("--path", required=True, metavar="DIR",
                    help="bundle directory to adopt into the state DB")
    _dry(im, what="be adopted")
    _cfg_json(im)
    sc = se_sub.add_parser("scan",
                           help="import every bundle under a root, and re-locate moved ones")
    sc.add_argument("--root", metavar="DIR",
                    help="directory to scan (default: the configured library dir)")
    _dry(sc, what="be adopted")
    _cfg_json(sc)
    pa = se_sub.add_parser("path", help="print a series' bundle directory")
    _series_pos(pa, what="print the bundle directory of")
    _cfg_json(pa)
    mg = se_sub.add_parser(
        "migrate", help="move a series into the bundle layout (chapters/, covers/, …)")
    _series_pos(mg, required=False, what="migrate")
    _dry(mg, what="move")
    _cfg_json(mg)
    ar = se_sub.add_parser("archive", help="write a bundle to a tar archive")
    _series_pos(ar, what="archive")
    ar.add_argument("-o", "--out", help="output path (default <slug>.tar; "
                                        ".tar.zst/.gz/.bz2/.xz to compress)")
    ar.add_argument("--with-cache", action="store_true",
                    help="include .cache/ — usually 10x larger, and regenerable")
    _cfg_json(ar)
    rc = se_sub.add_parser(
        "reclaim", help="drop shared-cache links now duplicated inside bundles")
    _dry(rc, what="be reclaimed")
    _cfg_json(rc)

    _cfg_json(se)
    se.set_defaults(func=_cmd_series, action=None, frm="start", purge=False,
                    dry_run=False, root=None, path=None, scope=None, out=None,
                    with_cache=False)

    # -- state (plumbing) ---------------------------------------------------
    stt = sub.add_parser("state", help="the per-chapter stage machine, by hand")
    stt_sub = stt.add_subparsers(dest="action")
    ss = stt_sub.add_parser("show", help="per-chapter stage table")
    _series_pos(ss, what="list chapters for")
    _range_pos(ss, default_means="every chapter")
    _cfg_json(ss)
    sx = stt_sub.add_parser("set", help="force a status")
    _series_pos(sx, what="change")
    sx.add_argument("--range", required=True, metavar="SPEC",
                    help="N | N-M | N- | -M | comma list (1-3,7). Required here: "
                         "forcing a status is never implicit")
    sx.add_argument("--status", required=True, metavar="STATE",
                    help="one of: new | fetched | parsed | rendered | skipped")
    _cfg_json(sx)
    sr = stt_sub.add_parser("reset", help="errored chapters -> new, to retry")
    _series_pos(sr, what="reset errored chapters in")
    _range_pos(sr, default_means="every errored chapter")
    _cfg_json(sr)
    _cfg_json(stt)
    stt.set_defaults(func=_cmd_state, action="show", range="")

    cs = sub.add_parser("cast", help="this series' voice assignments")
    cs_sub = cs.add_subparsers(dest="action")
    ce = cs_sub.add_parser("edit", help="open the series config in $EDITOR")
    _series_pos(ce, what="edit the cast of")
    _cfg_json(ce)
    cu = cs_sub.add_parser("update", help="merge in speakers found in a chapter range")
    _series_pos(cu, what="update the cast of")
    _range_pos(cu, default_means="the first [cast] seed_chapters")
    cu.add_argument("--diff", action="store_true", help="show what it would add, write nothing")
    _cfg_json(cu)
    ck2 = cs_sub.add_parser("set", help="assign one speaker a voice (no editor)")
    _series_pos(ck2, what="assign a voice in")
    ck2.add_argument("--speaker", required=True, metavar="NAME",
                     help="the speaker name as `cast show` lists it")
    ck2.add_argument("--voice", default="", metavar="VOICE",
                     help='Kokoro voice id, or "" to list the speaker unassigned')
    _cfg_json(ck2)
    cw = cs_sub.add_parser("show", help="the effective cast, including unassigned")
    _series_pos(cw, what="show the cast of")
    _cfg_json(cw)
    _cfg_json(cs)
    cs.set_defaults(func=_cmd_cast, action="show", range="", diff=False)

    # -- lexicon / config / voices ------------------------------------------
    # Every field is a named flag. `lex add` used to take `slug surface respell`
    # positionally with a `--base` that made `slug` dead but still required, and
    # the CSV it writes lists its columns in a different order than the command
    # took them -- so a caller reconstructing the call from the file format bound
    # each value one slot off, and nothing complained because the arity matched.
    # Names cannot be transposed, and `--scope` is the single destination slot.
    lx = sub.add_parser("lex", help="pronunciation lexicons")
    lx_sub = lx.add_subparsers(dest="action")

    def _scope(p, *, required=True, extra=""):
        p.add_argument("--scope", required=required, metavar="SCOPE",
                       help="which lexicon to act on: a tracked series slug, or "
                            f"{_SCOPE_BASE} for the always-on base lexicon" + extra)

    le = lx_sub.add_parser("edit", help="open a lexicon CSV in $EDITOR")
    _scope(le)
    _cfg(le)

    la = lx_sub.add_parser("add", help="append a row")
    _scope(la)
    la.add_argument("--surface", required=True, type=_nonempty, metavar="WORD",
                    help="word or phrase as it appears in the text")
    la.add_argument("--respell", required=True, metavar="SPELLING",
                    help="sound-it-out spelling, e.g. kay-lith. Empty means "
                         "'reads fine as-is'")
    la.add_argument("--pos", default="", metavar="POS",
                    help="only when tagged this way: NOUN VERB ADJ, a Penn tag "
                         "(VBD), or TAG+lemma. Omit to always apply")
    la.add_argument("--note", default="", metavar="TEXT",
                    help="why this row exists, for the next reader")
    _cfg_json(la)

    lp = lx_sub.add_parser(
        "promote", help="move a rule from a series lexicon into the always-on base")
    _scope(lp, extra=" (a series; promoting from the base is a no-op)")
    lp.add_argument("--surface", required=True, type=_nonempty, metavar="WORD",
                    help="the rule's surface, as it appears in the series file")
    lp.add_argument("--pos", default="", metavar="POS",
                    help="disambiguate when the surface has more than one rule "
                         "(NOUN VERB ADJ, a Penn tag, or TAG+lemma)")
    lp.add_argument("--force", action="store_true",
                    help="add anyway if the base file already has this rule")
    _cfg_json(lp)

    li = lx_sub.add_parser("ignore", help="mark words as 'reads fine' so `check` stops listing them")
    _scope(li)
    li.add_argument("--words", required=True, nargs="+", type=_nonempty, metavar="WORD",
                    help="one or more words to mark as reading correctly")
    _cfg_json(li)

    ll = lx_sub.add_parser("list", help="effective entries (base + series)")
    _scope(ll, required=False, extra=". Omit for the base lexicon alone")
    _cfg_json(ll)

    _cfg_json(lx)
    lx.set_defaults(func=_cmd_lex, action="list", scope=None)

    co = sub.add_parser("config", help="resolved paths / edit config.toml")
    co_sub = co.add_subparsers(dest="action")
    _cfg_json(co_sub.add_parser("show", help="resolved paths"))
    _cfg(co_sub.add_parser("edit", help="open config.toml in $EDITOR"))
    _cfg_json(co)
    co.set_defaults(func=_cmd_config, action="show")

    # `pron` is the command an agent leans on hardest -- it is the only way to
    # ask what the TTS will actually say -- so a wrong answer here is worse than
    # a wrong answer anywhere else. It used to take `--series` (a fourth name for
    # the same concept, unvalidated: a typo'd slug silently fell back to the base
    # lexicon and still printed "with lexicon", so you got a confident preview
    # missing the series' own rules) and it could not emit JSON at all.
    pr = sub.add_parser("pron", help="how the TTS will pronounce text")
    pr.add_argument("text", nargs="*",
                    help="the words to sound out; quoting is optional")
    # --no-lexicon means 'no rules at all', which makes a scope meaningless.
    # Declared here rather than in prose so `schema` can see the relationship.
    src = pr.add_mutually_exclusive_group()
    src.add_argument("--scope", metavar="SCOPE",
                     help=f"also apply this series' lexicon and overlay, or "
                          f"{_SCOPE_BASE} for the base alone (the default)")
    src.add_argument("--no-lexicon", action="store_true",
                     help="raw g2p only, applying no lexicon")
    pr.add_argument("--check", action="store_true",
                    help="audit every lexicon row instead of reading text")
    _cfg_json(pr)
    pr.set_defaults(func=_cmd_pron)

    vc = sub.add_parser("voices", help="list voices / render an audition file")
    vc_sub = vc.add_subparsers(dest="action")
    _cfg_json(vc_sub.add_parser("list", help="the 28 English voice ids"))
    vd = vc_sub.add_parser("demo", help="one chaptered .opus, each voice in turn")
    vd.add_argument("-o", "--out", default="voice-audition.opus", metavar="PATH",
                    help="output .opus (default: voice-audition.opus)")
    vd.add_argument("--text", help="custom sample paragraph")
    vd.add_argument("--only", help="comma-separated subset of voice ids")
    vd.add_argument("--pause", type=int, default=1200, help="ms between voices")
    vd.add_argument("--announcer", default="am_michael", metavar="VOICE",
                    help="voice that reads each voice's name (default: am_michael)")
    _cfg(vd)
    _cfg(vc)
    vc.set_defaults(func=_cmd_voices, action="list", out="voice-audition.opus",
                    text=None, only=None, pause=1200, announcer="am_michael")

    # -- delivery + misc ----------------------------------------------------
    sv = sub.add_parser("serve", help="LAN podcast feeds + audio")
    sv.add_argument("--host", metavar="ADDR",
                    help="interface to bind (default: from config)")
    sv.add_argument("--port", type=int, metavar="N",
                    help="port to listen on (default: from config)")
    _cfg(sv)
    sv.set_defaults(func=_cmd_serve)

    bk = sub.add_parser("book", help="stitch rendered chapters into a .m4b")
    _series_pos(bk, what="stitch")
    _range_pos(bk, default_means="every rendered chapter")
    bk.add_argument("-o", "--out", metavar="PATH",
                    help="output .m4b (default: <slug>.m4b beside the bundle)")
    _cfg_json(bk)
    bk.set_defaults(func=_cmd_book)

    fd = sub.add_parser("feed", help="write static RSS file(s)")
    _series_pos(fd, required=False, what="write a feed for")
    fd.add_argument("--base-url", default="", metavar="URL",
                    help="public URL the feed's enclosure links are built from")
    fd.add_argument("--out-dir", metavar="DIR",
                    help="where to write the .xml (default: the configured feed dir)")
    _cfg_json(fd)
    fd.set_defaults(func=_cmd_feed)

    # -- cache -------------------------------------------------------------
    ca = sub.add_parser("cache", help="segment-cache status and maintenance")
    ca_sub = ca.add_subparsers(dest="action")
    cs = ca_sub.add_parser("status", help="what the cache holds (read-only)")
    _series_pos(cs, required=False, what="report on")
    _cfg_json(cs)
    cc = ca_sub.add_parser(
        "compact", help="re-encode float32 wav to FLAC under the current generation")
    _series_pos(cc, required=False, what="compact the cache of")
    _dry(cc, what="be re-encoded")
    _cfg_json(cc)
    cp = ca_sub.add_parser("prune", help="delete cache entries nothing references")
    _series_pos(cp, required=False, what="prune the cache of")
    cp.add_argument("--stale", action="store_true",
                    help="drop non-current synth generations instead")
    _dry(cp, what="be deleted")
    _yes(cp, what="deleting unreferenced segments")
    cp.add_argument("--force", action="store_true",
                    help="override the reachable-set safety check")
    _cfg_json(cp)
    cl = ca_sub.add_parser("clear", help="delete every cached segment, reachable or not")
    _series_pos(cl, required=False, what="clear the cache of")
    _dry(cl, what="be deleted")
    _yes(cl, what="deleting every cached segment")
    _cfg_json(cl)
    _cfg_json(ca)
    ca.set_defaults(func=_cmd_cache, action=None, scope=None, dry_run=False,
                    stale=False, yes=False, force=False)

    sc = sub.add_parser("schema", help="emit the command surface as JSON (for agents)")
    sc.set_defaults(func=_cmd_schema)

    rt = sub.add_parser("retag", help="refresh Opus tags on rendered chapters (no re-encode)")
    _series_pos(rt, required=False, what="retag")
    _dry(rt, what="be retagged", short=False)
    _cfg_json(rt)
    rt.set_defaults(func=_cmd_retag)

    pg = sub.add_parser("progress", help="what a running sync is doing (read-only)")
    pg.add_argument("--scope", dest="scope", default="", metavar="SERIES",
                    help="the series to report on: slug, id, or a title substring; "
                         f"omit or {_SCOPE_ALL} for every enabled series")
    pg.add_argument("--watch", type=float, nargs="?", const=5.0, default=0,
                    help="redraw every N seconds (default 5)")
    pg.add_argument("--recent", type=int, default=8, help="how many finished chapters to list")
    _cfg_json(pg)
    pg.set_defaults(func=_cmd_progress)

    tg = sub.add_parser("tagger", help="optional spaCy POS tagger for heteronyms")
    tg_sub = tg.add_subparsers(dest="action")
    ti = tg_sub.add_parser("install", help="install the model and switch it on")
    ti.add_argument("--model", default="",
                    help="en_core_web_sm | _md | _lg (default: sm)")
    ti.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    _cfg_json(ti)
    tr = tg_sub.add_parser("remove", help="uninstall the model and switch it off")
    tr.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    _cfg_json(tr)
    ts = tg_sub.add_parser("status", help="what is installed, configured and active")
    _cfg_json(ts)
    tt = tg_sub.add_parser("test", help="show before/after for one sentence")
    tt.add_argument("text", help="a sentence to tag and sound out")
    _cfg_json(tt)
    _cfg_json(tg)
    tg.set_defaults(func=_cmd_tagger, action="status", model="", yes=False, text="")

    md = sub.add_parser("models", help="the Kokoro ONNX model files")
    md_sub = md.add_subparsers(dest="action")
    for name, helptext in (("fetch", "download (~350 MB, once)"), ("path", "where they live")):
        m = md_sub.add_parser(name, help=helptext)
        m.add_argument("--cache-dir", metavar="DIR",
                       help="where the ONNX model files live (default: from config)")
    md.set_defaults(func=_cmd_models, action="fetch", cache_dir=None)

    lg = sub.add_parser("login", help="store a royalroad.com session cookie")
    # Four flags, one of which is really a choice of mode: the handler checked
    # --logout first and unconditionally, so `login --status --logout` cleared the
    # session and never reported anything. Whichever lost was silently discarded,
    # and this is the one command in the surface where the discarded argument was
    # the read-only one. Declared as a group so the combination cannot parse --
    # and so `schema` shows callers that it cannot.
    mode = lg.add_mutually_exclusive_group()
    mode.add_argument("--cookies-file", metavar="PATH",
                      help="Netscape cookies.txt exported from your browser")
    mode.add_argument("--cookie-header", dest="cookie", metavar="STR",
                      help="the raw Cookie: header value (devtools -> copy)")
    mode.add_argument("--status", action="store_true",
                      help="report whether a session is stored (does not verify it)")
    mode.add_argument("--logout", action="store_true",
                      help="delete the stored session")
    _cfg_json(lg)
    lg.set_defaults(func=_cmd_login)

    ui = sub.add_parser("ui", help="launch the Tcl/Tk control UI")
    ui.add_argument("--interpreter", default="",
                    help="Python to host the UI (default: the first one found "
                         "whose Tk can render real fonts). Forces a Tk the "
                         "default search rejects — e.g. a uv-managed "
                         "interpreter, for Tk 9, at the cost of bold, font "
                         "sizes and typeset punctuation")
    ui.set_defaults(func=_cmd_ui)

    return ap
