from __future__ import annotations

import os

# Keep ONNX Runtime / OpenMP from oversubscribing the mobile APU (8 cores).
os.environ.setdefault("OMP_NUM_THREADS", "6")

import argparse  # noqa: E402

from . import __version__  # noqa: E402
from .config import Config  # noqa: E402

_INPUT_HELP = "text file, .html file, or http(s) chapter URL"

_MALE_POOL = [
    "am_michael", "bm_lewis", "am_puck", "bm_george", "am_eric", "am_onyx",
    "bm_daniel", "am_fenrir", "am_liam", "bm_fable",
]
_FEMALE_POOL = [
    "af_heart", "bf_emma", "af_bella", "af_nicole", "bf_alice", "af_sarah",
    "af_sky", "bf_isabella", "af_aoede", "af_kore",
]
# all English Kokoro voice ids (af/am = US, bf/bm = UK)
_EN_VOICES = sorted(set(_MALE_POOL + _FEMALE_POOL) | {
    "am_adam", "am_echo", "am_santa", "af_alloy", "af_jessica", "af_nova", "af_river",
    "bm_daniel", "bf_lily", "bm_george",
})


def _default_config() -> str:
    here = os.path.dirname(__file__)
    for candidate in ("config.toml", os.path.join(here, "..", "..", "config.example.toml")):
        if os.path.exists(candidate):
            return candidate
    return "config.toml"


def _suggest_voices(counts: dict, gender: dict, cfg: Config) -> dict:
    used = set(cfg.cast.voices.values())
    m = (v for v in _MALE_POOL if v not in used)
    f = (v for v in _FEMALE_POOL if v not in used)
    out: dict[str, str] = {}
    for name in sorted(counts, key=lambda n: -counts[n]):
        if not name:
            continue
        if name in cfg.cast.voices:
            out[name] = cfg.cast.voices[name]
            continue
        pool = f if gender.get(name) == "f" else m
        out[name] = next(pool, cfg.cast.default or cfg.voices.dialogue_default)
    return out


def _cmd_render(args) -> int:
    from . import pipeline

    cfg = Config.load(args.config)
    if args.backend:
        cfg.synth.backend = args.backend
    if args.voice:
        cfg.voices.narrator = args.voice
    if args.speed:
        cfg.synth.speed = args.speed

    report = pipeline.render(
        args.input, args.out, cfg,
        backend=cfg.synth.backend, dry_run=args.dry_run, jobs=args.jobs,
    )
    md_path = os.path.splitext(args.out)[0] + ".md"
    print()
    print(f"  script      : {report.script_path}")
    if os.path.exists(md_path):
        print(f"  text        : {md_path}")
    if not args.dry_run:
        print(f"  audio       : {report.out_path}  ({report.size_bytes / 1024:.0f} KiB)")
        print(f"  duration    : {report.audio_seconds / 60:.1f} min")
        print(f"  wall time   : {report.wall_seconds / 60:.1f} min")
        print(f"  realtime x  : {report.realtime_factor:.1f}")
    return 0


def _cmd_inspect(args) -> int:
    from .pipeline import build_script
    from .segment import find_unknown_names

    cfg = Config.load(args.config)
    blocks, segs, meta, _ = build_script(args.input, cfg)

    kinds: dict[str, int] = {}
    for b in blocks:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    styles: dict[str, int] = {}
    for s in segs:
        k = s.style if s.kind == "speech" else s.kind
        styles[k] = styles.get(k, 0) + 1
    words = sum(len(s.text.split()) for s in segs if s.kind == "speech")

    print(f"config      : {args.config}")
    if meta.get("title"):
        print(f"chapter     : {meta['title']}  ({meta.get('album', '?')})")
    print(f"blocks      : {len(blocks)}  {kinds}")
    print(f"segments    : {len(segs)}  {styles}")
    print(f"words       : {words}   est audio ~{words / 2.7 / 60:.1f} min")

    cast_counts: dict[str, int] = {}
    for s in segs:
        if s.kind == "speech" and s.style == "dialogue":
            cast_counts[s.speaker or "(unattributed)"] = cast_counts.get(s.speaker or "(unattributed)", 0) + 1
    if cast_counts:
        print("\ndialogue by speaker -> voice:")
        for name, n in sorted(cast_counts.items(), key=lambda kv: -kv[1]):
            voice = cfg.cast.voices.get(name, f"{cfg.cast.default or cfg.voices.dialogue_default} (default)")
            print(f"  {n:3d}  {name:<16} {voice}")

    chat_counts: dict[str, int] = {}
    chat_voice_of: dict[str, str] = {}
    for s in segs:
        if s.kind == "speech" and s.style == "chat" and s.speaker:
            chat_counts[s.speaker] = chat_counts.get(s.speaker, 0) + 1
            chat_voice_of.setdefault(s.speaker, s.voice)
    if chat_counts:
        n_lines = sum(chat_counts.values())
        print(f"\nchat: {n_lines} segments from {len(chat_counts)} handles -> pool of "
              f"{len(cfg.chat.voices)} voices")
        for name, n in sorted(chat_counts.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {n:3d}  {name:<24} {chat_voice_of.get(name, '?')}")

    from .pipeline import _load_lexicon

    lex = _load_lexicon(cfg)                       # base + per-series, stacked
    known = lex.surfaces() if lex is not None else set()
    unknown = find_unknown_names(blocks, known)
    print(f"\ncandidate proper nouns not in lexicon ({len(unknown)}):")
    for name, n in unknown:
        print(f"  {n:3d}  {name}")

    print("\nfirst 16 segments:")
    for s in segs[:16]:
        if s.kind == "cue":
            tag = "~earcon~"
        elif s.kind == "pause":
            tag = "pause"
        elif s.style in ("dialogue", "chat") and s.speaker:
            tag = s.speaker
        else:
            tag = s.style
        print(f"  [{tag:>16}] {s.text[:76]}")
    return 0


def _cmd_cast(args) -> int:
    from .dialogue import discover
    from .pipeline import load_document

    cfg = Config.load(args.config)
    blocks, _, _ = load_document(args.input, cfg)
    counts, gender = discover(blocks, cfg)
    voices = _suggest_voices(counts, gender, cfg)

    named = counts.get("", 0)
    print("# paste into config.toml and adjust voices to taste")
    if cfg.cast.protagonist:
        print(f"# protagonist = {cfg.cast.protagonist!r}")
    print("[cast.voices]")
    for name, voice in voices.items():
        g = {"m": "male", "f": "female"}.get(gender.get(name), "?")
        print(f'{name:<16} = "{voice}"   # {counts[name]} lines, {g}')
    if named:
        print(f"# {named} dialogue line(s) went unattributed -> [cast] default voice")
    return 0


def _cmd_lexicon(args) -> int:
    from .lexicon import Lexicon
    from .pipeline import load_document
    from .segment import find_unknown_names

    cfg = Config.load(args.config)
    blocks, _, _ = load_document(args.input, cfg)

    path = args.lexicon or cfg.general.lexicon
    known = Lexicon.load(path).surfaces() if path and os.path.exists(path) else set()
    base = cfg.general.base_lexicon
    if base and os.path.exists(base) and base != path:
        known |= Lexicon.load(base).surfaces()          # already handled globally
    candidates = [name for name, _ in find_unknown_names(blocks, known)]

    if not candidates:
        print("no new candidate proper nouns.")
        return 0
    print(f"{len(candidates)} candidate(s): {', '.join(candidates)}")

    if args.write:
        if not path:
            print("no lexicon path (set [general] lexicon or pass --lexicon)")
            return 1
        added = Lexicon.append_candidates(path, candidates)
        print(f"appended {added} row(s) to {path} — fill in the `respell` column by ear.")
    else:
        print("re-run with --write to append blank rows to the lexicon CSV.")
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
    voices = [v.strip() for v in args.only.split(",")] if args.only else list(_EN_VOICES)
    voices = [v for v in voices if v]

    if not args.demo:
        for pre in ("am", "af", "bm", "bf"):
            row = [v for v in voices if v.startswith(pre + "_")]
            if row:
                print(f"{_ACCENT[pre]:16} {'  '.join(row)}")
        extra = [v for v in voices if v.split('_')[0] not in _ACCENT]
        if extra:
            print(f"{'other':16} {'  '.join(extra)}")
        print(f"\n{len(voices)} voices · audition file:  "
              f"webnovel-audio voices --demo -o voices.opus")
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


def _cmd_fetch(args) -> int:
    from .ingest import fetch_html

    html = fetch_html(args.url)
    out = args.out or "chapter.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"saved {out} ({len(html)} bytes)")
    return 0


def _cmd_fetch_models(args) -> int:
    from .synth.kokoro import DEFAULT_CACHE, fetch_models

    fetch_models(args.cache_dir or DEFAULT_CACHE)
    return 0


# --- Phase 4: Royal Road library -------------------------------------------

def _jprint(obj) -> None:
    import json as _json

    print(_json.dumps(obj, ensure_ascii=False))


def _parse_range(s):
    """'', '5', '5-9' -> (lo, hi) with None meaning open."""
    s = (s or "").strip()
    if not s:
        return None, None
    if "-" in s:
        a, _, b = s.partition("-")
        return (int(a) if a.strip() else None), (int(b) if b.strip() else None)
    return int(s), int(s)


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

    if args.action in ("set", "redo"):
        db = DB(cfg.royalroad.state_db)
        s = db.get_series(args.key)
        if not s:
            (_jprint if want_json else print)(
                {"ok": False, "error": f"no series matching {args.key!r}"}
                if want_json else f"no tracked series matching {args.key!r}")
            db.close()
            return 1
        chs = db.chapters(s["id"])
        if args.action == "set":
            order = sync._resolve_start(chs, args.position)
            db.force_progress(s["id"], order)
            pend = len(db.pending(s["id"]))
            if want_json:
                _jprint({"ok": True, "slug": s["slug"], "progress": order + 1, "pending": pend})
            else:
                print(f"{s['title']}: progress set to #{order + 1} ({pend} chapters pending)")
            db.close()
            return 0

        # redo: mark rendered chapters in a range back to 'new' so `sync` re-renders
        lo, hi = _parse_range(args.range)
        redo = [c for c in chs if c["status"] == "rendered"
                and (lo is None or c["ord"] + 1 >= lo) and (hi is None or c["ord"] + 1 <= hi)]
        for c in redo:
            db.mark(c["id"], "new")
        if redo:
            db.force_progress(s["id"], redo[0]["ord"] - 1)   # never raises progress
        nums = [c["ord"] + 1 for c in redo]
        if want_json:
            _jprint({"ok": True, "slug": s["slug"], "requeued": nums,
                     "pending": len(db.pending(s["id"]))})
        else:
            print(f"{s['title']}: re-queued {len(nums)} chapter(s) "
                  f"{('#' + str(nums[0]) + '–#' + str(nums[-1])) if nums else ''} — run `sync`")
        db.close()
        return 0

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
        print(f"{r['title']}")
        extra = f", {r['errors']} error(s)" if r["errors"] else ""
        print(f"  {r['chapters']} chapters, at #{r['progress']}, {r['pending']} pending{extra}"
              f"   [{r['slug']}]  {r['url']}")
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
    from .royalroad import (RRClient, clear_session, load_session,
                            parse_cookie_header, parse_cookies_txt, save_session)

    if args.logout:
        clear_session()
        print("session cleared.")
        return 0
    if args.check:
        ok = RRClient().is_authenticated() if load_session() else False
        print("authenticated." if ok else "not authenticated (no / stale cookie).")
        return 0 if ok else 1

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
    ok = RRClient(cookies=cookies).is_authenticated()
    print(f"saved {len(cookies)} cookie(s) -> {'authenticated.' if ok else 'but NOT authenticated; check the cookie.'}")
    return 0 if ok else 1


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

    out = sync.make_book(Config.load(args.config), args.key,
                         first=args.frm, last=args.to, out=args.out)
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
        exec_start = f"{venv_bin} sync"
    else:
        exec_start = f"{shutil.which('uv') or 'uv'} run --project {project} webnovel-audio sync"
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
        argv = ["--help"]                      # bare `webnovel-audio` -> help, not the UI

    ap = argparse.ArgumentParser(
        prog="webnovel-audio",
        description="Offline web-novel TTS, CPU-first (Ryzen 8745HS / no CUDA).",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="text/html/URL -> narrated .opus")
    r.add_argument("input", help=_INPUT_HELP)
    r.add_argument("-o", "--out", required=True)
    r.add_argument("-c", "--config", default=_default_config())
    r.add_argument("--backend", choices=["kokoro", "null"])
    r.add_argument("--voice", help="override narrator voice id")
    r.add_argument("--speed", type=float, help="override speaking rate")
    r.add_argument(
        "--jobs", type=int, default=1,
        help="parallel synth workers. Leave at 1 for Kokoro (ONNX Runtime already "
             "uses every core); raise it only for single-threaded backends.",
    )
    r.add_argument("--dry-run", action="store_true", help="build the segment script, skip audio")
    r.set_defaults(func=_cmd_render)

    i = sub.add_parser("inspect", help="blocks, segment styles, cast, thought routing, lexicon queue")
    i.add_argument("input", help=_INPUT_HELP)
    i.add_argument("-c", "--config", default=_default_config())
    i.set_defaults(func=_cmd_inspect)

    ca = sub.add_parser("cast", help="detect speakers and emit a starter [cast.voices] block")
    ca.add_argument("input", help=_INPUT_HELP)
    ca.add_argument("-c", "--config", default=_default_config())
    ca.set_defaults(func=_cmd_cast)

    lx = sub.add_parser("lexicon", help="list / append pronunciation candidates for a chapter")
    lx.add_argument("input", help=_INPUT_HELP)
    lx.add_argument("-c", "--config", default=_default_config())
    lx.add_argument("--lexicon", help="CSV path (defaults to [general] lexicon)")
    lx.add_argument("--write", action="store_true", help="append blank rows for new names")
    lx.set_defaults(func=_cmd_lexicon)

    pr = sub.add_parser("pron", help="show how Kokoro will pronounce text (phonemes + rough gloss)")
    pr.add_argument("text", nargs="*", help="word(s) to check; omit with --check")
    pr.add_argument("-c", "--config", default=_default_config())
    pr.add_argument("--series", help="also apply that series' lexicon + overlay")
    pr.add_argument("--no-lexicon", action="store_true", help="raw g2p only, skip lexicons")
    pr.add_argument("--check", action="store_true",
                    help="audit every lexicon row: surface vs respell phonemes")
    pr.set_defaults(func=_cmd_pron)

    ui = sub.add_parser("ui", help="launch the Tcl/Tk control UI")
    ui.add_argument("--python", action="store_true",
                    help="use Python's bundled Tcl/Tk instead of the `wish` binary")
    ui.set_defaults(func=_cmd_ui)

    vc = sub.add_parser("voices", help="list voices, or render an audition file that cycles them")
    vc.add_argument("--demo", action="store_true", help="render one .opus, each voice speaking a sample")
    vc.add_argument("-o", "--out", default="voice-audition.opus")
    vc.add_argument("--text", help="custom sample paragraph (--demo)")
    vc.add_argument("--only", help="comma-separated subset of voice ids")
    vc.add_argument("--pause", type=int, default=1200, help="ms between voices (--demo; default 1200)")
    vc.add_argument("--announcer", default="am_michael", help="voice that reads each label (--demo)")
    vc.add_argument("-c", "--config", default=_default_config())
    vc.set_defaults(func=_cmd_voices)

    ft = sub.add_parser("fetch", help="download a chapter page to a local .html file")
    ft.add_argument("url")
    ft.add_argument("-o", "--out", help="output path (default chapter.html)")
    ft.set_defaults(func=_cmd_fetch)

    fm = sub.add_parser("fetch-models", help="download Kokoro ONNX model + voices (~350 MB)")
    fm.add_argument("--cache-dir")
    fm.set_defaults(func=_cmd_fetch_models)

    def _cfg_json(p):
        p.add_argument("-c", "--config", default=_default_config())
        p.add_argument("--json", action="store_true", help="machine-readable output")

    se = sub.add_parser("series", help="track Royal Road series in the library DB")
    se_sub = se.add_subparsers(dest="action")
    se_add = se_sub.add_parser("add", help="start tracking a fiction")
    se_add.add_argument("url", help="fiction page URL or id")
    se_add.add_argument("--from", dest="frm", default="latest",
                        help="latest (default) | start | <N> | <chapter-url>")
    _cfg_json(se_add)
    se_set = se_sub.add_parser("set", help="set how far you've listened / read")
    se_set.add_argument("key", help="series slug / id / title substring")
    se_set.add_argument("position", help="latest | start | <N> | <chapter-url>")
    _cfg_json(se_set)
    se_redo = se_sub.add_parser("redo", help="re-queue rendered chapters (after a lexicon/config edit)")
    se_redo.add_argument("key", help="series slug / id / title substring")
    se_redo.add_argument("range", nargs="?", default="", help="N | N-M | N- | -M (default: all rendered)")
    _cfg_json(se_redo)
    se_ref = se_sub.add_parser("refresh", help="re-fetch chapter lists")
    se_ref.add_argument("key", nargs="?", help="one series, or all if omitted")
    _cfg_json(se_ref)
    se_list = se_sub.add_parser("list", help="show tracked series")
    _cfg_json(se_list)
    _cfg_json(se)
    se.set_defaults(func=_cmd_series, action=None, frm="latest")

    sy = sub.add_parser("sync", help="render new chapters of tracked series into the library")
    sy.add_argument("key", nargs="?", help="one series, or all if omitted")
    sy.add_argument("--limit", type=int, help="max chapters per series this run")
    sy.add_argument("--backend", choices=["kokoro", "null"])
    sy.add_argument("--dry-run", action="store_true", help="list what would render")
    sy.add_argument("--no-refresh", action="store_true", help="skip re-fetching chapter lists")
    _cfg_json(sy)
    sy.set_defaults(func=_cmd_sync)

    co = sub.add_parser("config", help="show resolved paths (config, state DB, library, …)")
    _cfg_json(co)
    co.set_defaults(func=_cmd_config)

    lg = sub.add_parser("login", help="store a royalroad.com session cookie")
    lg.add_argument("--cookie", help='Cookie header string: "name=value; name2=value2"')
    lg.add_argument("--cookies-file", help="Netscape cookies.txt exported from a browser")
    lg.add_argument("--check", action="store_true", help="report current auth state")
    lg.add_argument("--logout", action="store_true", help="forget the stored cookie")
    lg.set_defaults(func=_cmd_login)

    sv = sub.add_parser("serve", help="LAN server: podcast feeds + audio for the library")
    sv.add_argument("-c", "--config", default=_default_config())
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.set_defaults(func=_cmd_serve)

    bk = sub.add_parser("book", help="stitch a series' rendered chapters into a .m4b")
    bk.add_argument("key", help="series slug / id / title substring")
    bk.add_argument("--from", dest="frm", type=int, help="first chapter number")
    bk.add_argument("--to", type=int, help="last chapter number")
    bk.add_argument("-o", "--out", help="output .m4b path")
    bk.add_argument("-c", "--config", default=_default_config())
    bk.set_defaults(func=_cmd_book)

    fd = sub.add_parser("feed", help="write static RSS feed file(s) for an external server")
    fd.add_argument("key", nargs="?", help="one series, or all if omitted")
    fd.add_argument("-o", "--out", help="output directory (default library/_feeds)")
    fd.add_argument("--base-url", help="e.g. https://host/webnovels")
    fd.add_argument("-c", "--config", default=_default_config())
    fd.set_defaults(func=_cmd_feed)

    sc = sub.add_parser("schedule", help="emit / install a systemd-user timer for nightly sync")
    sc.add_argument("--calendar", default="*-*-* 03:00", help="systemd OnCalendar (default 3am)")
    sc.add_argument("--install", action="store_true", help="write unit files to ~/.config/systemd/user")
    sc.set_defaults(func=_cmd_schedule)

    args = ap.parse_args(argv)
    return args.func(args)
