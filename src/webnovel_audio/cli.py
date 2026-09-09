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
    from .lexicon import Lexicon
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

    known: set[str] = set()
    if cfg.general.lexicon and os.path.exists(cfg.general.lexicon):
        known = Lexicon.load(cfg.general.lexicon).surfaces()
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

def _cmd_series(args) -> int:
    from . import sync
    from .db import DB

    cfg = Config.load(args.config)
    if args.action == "add":
        sync.add_series(cfg, args.url, start=args.frm)
        return 0
    if args.action == "refresh":
        sync.refresh(cfg, args.key)
        return 0
    if args.action == "set":
        db = DB(cfg.royalroad.state_db)
        s = db.get_series(args.key)
        if not s:
            print(f"no tracked series matching {args.key!r}")
            return 1
        order = sync._resolve_start(db.chapters(s["id"]), args.position)
        db.force_progress(s["id"], order)
        print(f"{s['title']}: progress set to #{order + 1} "
              f"({len(db.pending(s['id']))} chapters pending)")
        db.close()
        return 0

    # default: list
    db = DB(cfg.royalroad.state_db)
    rows = db.list_series()
    if not rows:
        print("no tracked series. add one:  webnovel-audio series add <fiction-url>")
    for s in rows:
        chs = db.chapters(s["id"])
        pend = len(db.pending(s["id"]))
        print(f"{s['title']}")
        print(f"  {len(chs)} chapters, at #{s['progress_order'] + 1}, {pend} pending"
              f"   [{s['slug']}]  {s['url']}")
    db.close()
    return 0


def _cmd_sync(args) -> int:
    from . import sync

    cfg = Config.load(args.config)
    res = sync.run_sync(cfg, args.key, limit=args.limit, dry_run=args.dry_run,
                        backend=args.backend or cfg.synth.backend,
                        refresh_first=not args.no_refresh)
    if args.dry_run:
        print(f"\nsync (dry run): {res.skipped} chapter(s) would render")
    else:
        print(f"\nsync: {res.rendered} rendered, {res.errors} error(s)")
    return 1 if res.errors else 0


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
    from .serve import serve

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

    ft = sub.add_parser("fetch", help="download a chapter page to a local .html file")
    ft.add_argument("url")
    ft.add_argument("-o", "--out", help="output path (default chapter.html)")
    ft.set_defaults(func=_cmd_fetch)

    fm = sub.add_parser("fetch-models", help="download Kokoro ONNX model + voices (~350 MB)")
    fm.add_argument("--cache-dir")
    fm.set_defaults(func=_cmd_fetch_models)

    se = sub.add_parser("series", help="track Royal Road series in the library DB")
    se_sub = se.add_subparsers(dest="action")
    se_add = se_sub.add_parser("add", help="start tracking a fiction")
    se_add.add_argument("url", help="fiction page URL or id")
    se_add.add_argument("--from", dest="frm", default="latest",
                        help="latest (default) | start | <N> | <chapter-url>")
    se_add.add_argument("-c", "--config", default=_default_config())
    se_set = se_sub.add_parser("set", help="set how far you've listened / read")
    se_set.add_argument("key", help="series slug / id / title substring")
    se_set.add_argument("position", help="latest | start | <N> | <chapter-url>")
    se_set.add_argument("-c", "--config", default=_default_config())
    se_ref = se_sub.add_parser("refresh", help="re-fetch chapter lists")
    se_ref.add_argument("key", nargs="?", help="one series, or all if omitted")
    se_ref.add_argument("-c", "--config", default=_default_config())
    se_list = se_sub.add_parser("list", help="show tracked series")
    se_list.add_argument("-c", "--config", default=_default_config())
    se.add_argument("-c", "--config", default=_default_config())
    se.set_defaults(func=_cmd_series, action=None, frm="latest")

    sy = sub.add_parser("sync", help="render new chapters of tracked series into the library")
    sy.add_argument("key", nargs="?", help="one series, or all if omitted")
    sy.add_argument("-c", "--config", default=_default_config())
    sy.add_argument("--limit", type=int, help="max chapters per series this run")
    sy.add_argument("--backend", choices=["kokoro", "null"])
    sy.add_argument("--dry-run", action="store_true", help="list what would render")
    sy.add_argument("--no-refresh", action="store_true", help="skip re-fetching chapter lists")
    sy.set_defaults(func=_cmd_sync)

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
