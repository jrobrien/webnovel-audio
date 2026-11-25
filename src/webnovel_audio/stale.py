"""Which artifacts in a series bundle are out of date, why, and what fixes them.

Read-only: the state DB is opened `mode=ro` (`progress.open_ro`) and nothing is
written anywhere -- no `segments.json`, no Markdown, no cache entries. The
script a render would speak is rebuilt with `pipeline.build_script` from the
same `Document` the render path parses (`sync._load_doc`), never through
`pipeline.prepare`, which writes.

The graph, per chapter:

- `text` (`<stem>.md`): missing; the raw page is newer than it; or a
  front-matter field differs from what a re-parse would write. `chapter`,
  `published`, `provider`, `source_id` and `author` are compared with the DB
  (a re-parse also corrects the series author from the page); `title` and
  `fiction` come from the page itself, so they are compared with a fresh parse
  of the cached raw page, not the DB -- comparing them with the DB would report
  differences no re-parse can fix. Fixed by `parse --range`.
- `audio` (`<stem>.opus`, rendered chapters only): the recipe recomputed now
  differs from the stored `.recipe`; `unknown` without one. `.recipe` files
  written since parts existed say which part changed, and a script change
  splits into `text/lexicon` (what is said) and `cast/voice` (voice, style,
  rate, pitch, speaker). Older one-line files report `recipe (no parts
  recorded)`, plus `synth model` when the DB's fingerprint differs and the
  script halves inferred from `segments.json`. Fixed by `render --range`
  (chapters whose recipe matches are skipped).
- `tags` (with `tags=True`): the stream's Vorbis comments, read with ffprobe,
  differ from `retag._full_tags`. Fixed by `retag`.

Per series, `episodes`: a planned episode file missing, stale by
`episodes._fresh`, or an orphan file `episodes.build` would delete.

The segment diff shown for a `segments` change is best-effort: it compares
against `<stem>.segments.json`, which `render --dry-run` also rewrites, so that
baseline can be newer than the audio.

The chapter list's freshness (`toc`) is not reported: the DB does not record
when a series was last refreshed.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass

from . import bundle, episodes, pipeline, providers, sync
from .retag import _full_tags
from .config import Config
from .db import DB, stage_rank
from .textout import _yaml

#: recipe part -> the word the report uses for it
PART_LABELS = {"segs": "segments", "segs_text": "text/lexicon", "segs_voice": "cast/voice",
               "audio": "audio", "dsp": "dsp", "pauses": "pauses",
               "sr": "sample rate", "backend": "backend", "fp": "synth model"}
NO_PARTS = "recipe (no parts recorded)"

#: Vorbis comments a render writes that no DB row can reproduce: the render
#: stamps its *start* time, the DB its end, so comparing them flags every file
TAGS_IGNORED = {"RENDERED_AT", "ENCODER"}

#: Vorbis keys ffmpeg renames when it reads them back (its ogg metadata
#: conversion table): written as the key, reported as the value
_TAG_ALIAS = {"TRACKNUMBER": "TRACK", "DISCNUMBER": "DISC", "ALBUMARTIST": "ALBUM_ARTIST"}

#: front-matter fields compared with the DB, and with a fresh parse
_FM_FROM_DB = ("author", "chapter", "published", "provider", "source_id")
_FM_FROM_PAGE = ("title", "fiction")


def open_db(cfg: Config) -> DB:
    """The state DB, read-only. Raises FileNotFoundError if there is none."""
    from .progress import open_ro
    return DB.wrap(open_ro(cfg))


# --- small helpers -----------------------------------------------------------

def compact_range(numbers) -> str:
    """[12, 40, 41, 42, 43, 44] -> "12,40-44" (what `--range` accepts)."""
    nums = sorted(set(numbers))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def _front_matter(path: str) -> dict:
    """`key -> raw value text` from a chapter .md's front matter, values exactly
    as `textout._front_matter` wrote them (quoted, escaped)."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        if fh.readline().strip() != "---":
            return out
        for line in fh:
            line = line.rstrip("\n")
            if line.strip() == "---":
                break
            if line.startswith("#") or ": " not in line:
                continue
            k, _, v = line.partition(": ")
            out[k.strip()] = v
    return out


def _fm_value(v) -> str | None:
    """A value as the front matter would hold it; None when it would be left out."""
    if v in ("", None):
        return None
    return str(v) if isinstance(v, int) else _yaml(v)


def _clip(a: str, b: str, context: int = 1) -> tuple[str, str]:
    """The differing middle of two strings, widened to whole words plus
    `context` words either side: ("the rebar", "the reebar")."""
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    q = 0
    while q < min(len(a), len(b)) - p and a[-1 - q] == b[-1 - q]:
        q += 1

    def widen(s: str, lo: int, hi: int) -> str:
        for _ in range(context + 1):
            lo = s.rfind(" ", 0, max(lo - 1, 0)) + 1 if lo > 0 else 0
        for _ in range(context + 1):
            nxt = s.find(" ", min(hi + 1, len(s)))
            hi = len(s) if nxt < 0 else nxt
        return s[lo:hi].strip()

    return widen(a, p, len(a) - q), widen(b, p, len(b) - q)


def _proj(d: dict, fields) -> tuple:
    return tuple(d.get(f) for f in fields)


def seg_changes(old: list[dict], new: list[dict]) -> list[str]:
    """Which halves of the script differ: "text/lexicon", "cast/voice", both or neither."""
    out = []
    if [_proj(d, pipeline.SEG_TEXT_FIELDS) for d in old] != \
            [_proj(d, pipeline.SEG_TEXT_FIELDS) for d in new]:
        out.append(PART_LABELS["segs_text"])
    sm = _text_matcher(old, new)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" and any(_proj(old[i1 + k], pipeline.SEG_VOICE_FIELDS)
                                  != _proj(new[j1 + k], pipeline.SEG_VOICE_FIELDS)
                                  for k in range(i2 - i1)):
            out.append(PART_LABELS["segs_voice"])
            break
    return out


def _text_matcher(old, new):
    key = pipeline.SEG_TEXT_FIELDS
    return difflib.SequenceMatcher(a=[json.dumps(_proj(d, key)) for d in old],
                                   b=[json.dumps(_proj(d, key)) for d in new],
                                   autojunk=False)


def segment_diff(old: list[dict], new: list[dict], limit: int = 3) -> list[str]:
    """What changed in the script: the first few changed texts, as
    `"the rebar" → "the reebar"`, then voice changes grouped by who speaks,
    as `voice Mira: af_bella → af_nova (23 segments)`.

    Segments are aligned on what is said, so a cast change (same words, new
    voice) lines up segment for segment."""
    texts: list[str] = []
    voices: dict[tuple, int] = {}

    def voice_change(o: dict, n: dict) -> None:
        who = n.get("speaker") or o.get("speaker") or n.get("style") or "narration"
        for f in pipeline.SEG_VOICE_FIELDS:
            if f != "speaker" and o.get(f) != n.get(f):
                voices[(f, who, o.get(f), n.get(f))] = \
                    voices.get((f, who, o.get(f), n.get(f)), 0) + 1

    for tag, i1, i2, j1, j2 in _text_matcher(old, new).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                voice_change(old[i1 + k], new[j1 + k])
            continue
        olds, news = old[i1:i2], new[j1:j2]
        for k in range(max(len(olds), len(news))):
            o = olds[k] if k < len(olds) else None
            n = news[k] if k < len(news) else None
            if o and n and o.get("text") != n.get("text"):
                a, b = _clip(o.get("text", ""), n.get("text", ""))
                texts.append(f'"{a}" → "{b}"')
            elif o and n:
                texts.append(f'"{_short(n.get("text", ""))}": ' + ", ".join(
                    f"{f} {o.get(f)!r} → {n.get(f)!r}" for f in pipeline.SEG_TEXT_FIELDS
                    if o.get(f) != n.get(f)))
                voice_change(o, n)
            elif n:
                texts.append(f'+ "{_short(n.get("text", ""))}"')
            else:
                texts.append(f'- "{_short(o.get("text", ""))}"')
    lines = texts[:limit]
    if len(texts) > limit:
        lines.append(f"… and {len(texts) - limit} more text change(s)")
    for (f, who, a, b), n in sorted(voices.items(), key=lambda kv: -kv[1]):
        lines.append(f"{f} {who}: {a} → {b} ({n} segment{'s' if n != 1 else ''})")
    return lines


def _short(s: str, n: int = 50) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def stream_tags(path: str) -> dict | None:
    """The audio stream's Vorbis comments, keys upper-cased; None if unreadable."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    r = subprocess.run([exe, "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream_tags", "-of", "json", path],
                       capture_output=True, text=True)
    if r.returncode:
        return None
    try:
        streams = json.loads(r.stdout).get("streams") or [{}]
    except ValueError:
        return None
    return {k.upper(): v for k, v in (streams[0].get("tags") or {}).items()}


def tag_diff(expected: dict, actual: dict) -> list[str]:
    """Tag keys whose value differs, compared case-insensitively by key."""
    want = {_TAG_ALIAS.get(k.upper(), k.upper()): str(v) for k, v in expected.items()}
    keys = (set(want) | set(actual)) - TAGS_IGNORED
    return sorted(k.lower() if k in ("TITLE", "ALBUM", "ARTIST", "COMMENT") else k
                  for k in keys if want.get(k) != actual.get(k))


# --- the per-series check -----------------------------------------------------

class _Shared:
    """What is loaded once per run rather than per series: the spaCy pipeline
    and the synth fingerprint (both depend on a few config fields only)."""

    def __init__(self):
        self.nlp: dict = {}
        self.fp: dict = {}

    def nlp_for(self, scfg: Config):
        model = getattr(scfg.general, "tagger", "") or ""
        if model not in self.nlp:
            self.nlp[model] = pipeline._load_nlp(scfg)
        return self.nlp[model]

    def fp_for(self, scfg: Config, backend: str) -> str:
        from . import cache
        k = (backend, scfg.general.models_dir, scfg.synth.sample_rate)
        if k not in self.fp:
            self.fp[k] = cache.current_fingerprint(scfg, backend)
        return self.fp[k]


def _check_text(c, md_path: str, raw_path: str | None, doc, s, prov) -> list[str]:
    if not os.path.exists(md_path):
        return ["missing"]
    why = []
    if raw_path and os.path.exists(raw_path) and \
            os.path.getmtime(raw_path) > os.path.getmtime(md_path):
        why.append("raw newer")
    fm = _front_matter(md_path)
    want = {"author": s["author"], **sync._md_extra(prov, c)}
    for f in _FM_FROM_DB:
        exp = _fm_value(want.get(f))
        if exp is not None and fm.get(f) is not None and fm[f] != exp:
            why.append(f"front matter: {f}")
        elif exp is not None and fm.get(f) is None and f != "author":
            why.append(f"front matter: {f}")
    if doc is not None:
        page = {"title": doc.chapter_title or "Untitled", "fiction": doc.fiction_title}
        for f in _FM_FROM_PAGE:
            if fm.get(f) != _fm_value(page[f]):
                why.append(f"front matter: {f}")
    return why


def _check_audio(out_path: str, segs, scfg, backend: str, fp: str, why: bool,
                 rendered_fp: str = ""):
    """(reasons, diff lines) for one rendered chapter.

    A `.recipe` from before parts existed cannot say which part changed. Two
    causes can still be named: the DB records the synth fingerprint the audio
    was made with (`rendered_fp`), and `segments.json` holds the last script
    (best-effort: see the module docstring)."""
    if not os.path.exists(out_path):
        return ["missing"], []
    stored = pipeline.read_recipe(out_path)
    if stored is None:
        return ["unknown"], []
    recipe, parts = pipeline.audio_recipe_parts(segs, scfg, backend, fp)
    if stored[0] == recipe:
        return [], []
    old_parts = stored[1]
    diff: list[str] = []
    if old_parts:
        reasons = []
        for k in pipeline.RECIPE_PARTS:
            if old_parts.get(k) == parts[k]:
                continue
            halves = [PART_LABELS[h] for h in ("segs_text", "segs_voice")
                      if k == "segs" and h in old_parts and old_parts[h] != parts[h]]
            reasons += halves or [PART_LABELS[k]]
        reasons = reasons or ["recipe"]
    else:
        reasons = [NO_PARTS]
        if rendered_fp and rendered_fp != fp:
            reasons.append(PART_LABELS["fp"])
    script = {PART_LABELS[k] for k in ("segs", "segs_text", "segs_voice")}
    if script & set(reasons) or NO_PARTS in reasons:
        try:
            with open(os.path.splitext(out_path)[0] + ".segments.json",
                      encoding="utf-8") as fh:
                old = json.load(fh)
            new = [asdict(x) for x in segs]
            if old != new:
                if NO_PARTS in reasons or PART_LABELS["segs"] in reasons:
                    # best-effort: name the half from the stored script
                    halves = [h for h in seg_changes(old, new) if h not in reasons]
                    reasons = [r for r in reasons if r != PART_LABELS["segs"] or not halves]
                    reasons += halves
                if why:
                    diff = segment_diff(old, new)
        except (OSError, ValueError):
            pass
    return reasons, diff


def _check_episodes(cfg, db, s, scfg, bdir) -> dict | None:
    """Stale episode files for one series, mirroring `episodes.build`."""
    try:
        n = episodes.per_episode(scfg)
    except SystemExit as exc:
        return {"per_episode": None, "stale": [], "error": str(exc)}
    eps = episodes.plan(db.chapters(s["id"]), n,
                        partial_after_days=scfg.feed.partial_after_days) if n > 1 else []
    d = episodes._dir(bdir)
    vols = db.volume_map(s["id"])
    stale, wanted = [], set()
    for ep in eps:
        path = os.path.join(d, ep.name)
        wanted |= {os.path.basename(path), os.path.basename(episodes._sidecar(path))}
        vol = vols.get(episodes._row(ep.first, "volume_source_id")) or {}
        if not os.path.exists(path):
            stale.append({"episode": ep.name, "reasons": ["missing"]})
        elif not episodes._fresh(path, ep):
            stale.append({"episode": ep.name, "reasons": ["members changed"]})
        elif not episodes._fresh(path, ep, s, vol.get("title", "")):
            stale.append({"episode": ep.name, "reasons": ["tags changed"]})
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f not in wanted and f.endswith((".opus", ".json")):
                stale.append({"episode": f, "reasons": ["orphan"]})
    return {"per_episode": n, "episodes": len(eps), "stale": stale}


@dataclass
class _Series:
    """Everything one chapter's check needs, loaded once per series."""
    s: object
    prov: object
    ctx: object
    bdir: str
    scfg: Config
    vol_map: dict
    backend: str
    lex: object = None
    nlp: object = None
    fp: str = ""
    tags: bool = False
    why: bool = False


def _check_chapter(j: _Series, c) -> tuple[dict | None, list[dict]]:
    """(the stale entry or None, problems) for one chapter."""
    problems: list[dict] = []
    num = c["ord"] + 1
    stem = sync._out_stem(j.bdir, c)
    raw_path = c["raw_path"] if c["raw_path"] and os.path.exists(c["raw_path"]) \
        else sync._raw_path(j.bdir, j.prov, c)
    entry: dict = {"number": num, "title": c["title"], "stale": {}}
    doc = None
    if os.path.exists(raw_path):
        try:
            doc = sync._load_doc(j.prov, j.ctx, c, raw_path)
        except (SystemExit, Exception) as exc:   # providers raise SystemExit
            problems.append({"number": num, "error": f"could not parse: {exc}"})
    else:
        problems.append({"number": num, "error": "raw page missing; not checked"})

    text = _check_text(c, stem + ".md", raw_path, doc, j.s, j.prov)
    if text:
        entry["stale"]["text"] = text

    if c["status"] == "rendered" and doc is not None:
        try:
            _, segs, _, _ = pipeline.build_script(doc, j.scfg, lexicon=j.lex, nlp=j.nlp)
            reasons, diff = _check_audio(stem + ".opus", segs, j.scfg, j.backend, j.fp,
                                         j.why, rendered_fp=c["synth_fingerprint"] or "")
        except Exception as exc:  # noqa: BLE001 - one bad chapter shouldn't sink the report
            problems.append({"number": num, "error": f"could not build script: {exc}"})
            reasons, diff = [], []
        if reasons:
            entry["stale"]["audio"] = reasons
            if diff:
                entry["diff"] = diff

    if j.tags and c["status"] == "rendered" and c["audio_path"] \
            and os.path.exists(c["audio_path"]):
        vol = j.vol_map.get(c["volume_source_id"]) if c["volume_source_id"] else None
        actual = stream_tags(c["audio_path"])
        if actual is None:
            problems.append({"number": num, "error": "could not read tags"})
        else:
            keys = tag_diff(_full_tags(j.scfg, j.s, c, vol), actual)
            if keys:
                entry["stale"]["tags"] = keys
    return (entry if entry["stale"] else None), problems


#: the series a forked worker checks; set in the parent just before forking
_WORK: tuple | None = None


def _work(i: int):
    j, rows = _WORK
    return _check_chapter(j, rows[i])


def default_jobs() -> int:
    """Worker processes: half the logical CPUs (spaCy tagging is the cost, and
    it is single-threaded per process), at least one."""
    return max(1, (os.cpu_count() or 2) // 2)


def _run(j: _Series, rows, jobs: int, progress=None):
    """`_check_chapter` over `rows`, in order, on `jobs` forked workers.

    Fork, so each worker inherits the lexicon and spaCy pipeline the parent
    loaded once for the series instead of loading its own. Workers only read."""
    slug = sync._dir_slug(j.s)
    if jobs <= 1 or len(rows) < 2 * jobs:
        for i, c in enumerate(rows, 1):
            yield _check_chapter(j, c)
            if progress:
                progress(slug, i, len(rows))
        return
    import multiprocessing as mp
    import warnings

    global _WORK
    _WORK = (j, rows)
    try:
        with warnings.catch_warnings():
            # spaCy/numpy may have started threads; the workers never touch
            # their locks, only their already-loaded tables
            warnings.simplefilter("ignore", DeprecationWarning)
            pool = mp.get_context("fork").Pool(jobs)
        with pool:
            for i, r in enumerate(pool.imap(_work, range(len(rows)), chunksize=2), 1):
                yield r
                if progress:
                    progress(slug, i, len(rows))
    finally:
        _WORK = None


def check_series(cfg: Config, db: DB, s, *, spans=None, tags: bool = False,
                 why: bool = False, shared: _Shared | None = None,
                 backend: str | None = None, progress=None, jobs: int = 1) -> dict:
    """The stale report for one series: every stale chapter with its reasons,
    per-node counts and the command that fixes each."""
    shared = shared or _Shared()
    backend = backend or cfg.synth.backend
    slug = sync._dir_slug(s)
    prov = providers.get(s["provider"])
    bdir = bundle.bundle_dir(cfg, s)
    scfg = sync._series_cfg(cfg, slug, bdir)
    rows = [c for c in db.select(s["id"], spans)
            if stage_rank(c["status"]) >= stage_rank("parsed")
            or (c["status"] == "error" and c["text_path"])]
    rendered = [c for c in rows if c["status"] == "rendered"]
    j = _Series(s=s, prov=prov, ctx=sync._ctx(cfg, db, prov, s, bdir), bdir=bdir,
                scfg=scfg, vol_map=db.volume_map(s["id"]), backend=backend,
                tags=tags, why=why)
    if rendered:
        j.lex = pipeline._load_lexicon(scfg)          # once per series
        j.nlp = shared.nlp_for(scfg)
        j.fp = shared.fp_for(scfg, backend)

    chapters, problems = [], []
    for entry, probs in _run(j, rows, jobs, progress):
        problems += probs
        if entry:
            chapters.append(entry)

    eps = _check_episodes(cfg, db, s, scfg, bdir)
    return {"slug": slug, "title": s["title"],
            "checked": {"text": len(rows), "audio": len(rendered),
                        "tags": len(rendered) if tags else None},
            "chapters": chapters, "episodes": eps, "problems": problems,
            "summary": summarize(slug, chapters, eps)}


def summarize(slug: str, chapters: list[dict], eps: dict | None) -> dict:
    """node -> {count, reasons: {reason: n}, range, fix}."""
    fixes = {"text": f"webnovel-audio parse --target {slug} --range {{r}}",
             "audio": f"webnovel-audio render --target {slug} --range {{r}}",
             "tags": f"webnovel-audio retag --scope {slug}"}
    out: dict = {}
    for node in ("text", "audio", "tags"):
        hit = [c for c in chapters if node in c["stale"]]
        if not hit:
            continue
        reasons: dict = {}
        for c in hit:
            for r in c["stale"][node]:
                reasons[r] = reasons.get(r, 0) + 1
        rng = compact_range(c["number"] for c in hit)
        out[node] = {"count": len(hit), "reasons": reasons, "range": rng,
                     "fix": fixes[node].format(r=rng)}
    if eps and eps.get("stale"):
        reasons = {}
        for e in eps["stale"]:
            for r in e["reasons"]:
                reasons[r] = reasons.get(r, 0) + 1
        out["episodes"] = {"count": len(eps["stale"]), "reasons": reasons,
                           "fix": f"webnovel-audio episodes --scope {slug}"}
    return out


def report(cfg: Config, key: str | None = None, *, spans=None, tags: bool = False,
           why: bool = False, progress=None, db: DB | None = None,
           jobs: int | None = None) -> list[dict]:
    """One `check_series` result per series: `key`, or every enabled series."""
    own = db is None
    db = db or open_db(cfg)
    try:
        if key:
            s = db.get_series(key)
            if not s:
                raise SystemExit(f"no tracked series matching {key!r}")
            targets = [s]
        else:
            targets = [s for s in db.list_series() if s["enabled"]]
        shared = _Shared()
        jobs = default_jobs() if jobs is None else jobs
        return [check_series(cfg, db, s, spans=spans, tags=tags, why=why,
                             shared=shared, progress=progress, jobs=jobs) for s in targets]
    finally:
        if own:
            db.con.close()


def render_text(results: list[dict], *, why: bool = False) -> str:
    """The human report: per series with anything stale, a line per node."""
    lines: list[str] = []
    for r in results:
        summ = r["summary"]
        if not summ and not (why and r["problems"]):
            continue
        lines.append(f"{r['slug']}  ({r['title']})")
        for node, v in summ.items():
            reasons = ", ".join(f"{k} {n}" for k, n in
                                sorted(v["reasons"].items(), key=lambda kv: -kv[1]))
            lines.append(f"  {node}: {v['count']} stale ({reasons}) → {v['fix']}")
            if not why:
                continue
            if node == "episodes":
                for e in r["episodes"]["stale"]:
                    lines.append(f"      {e['episode']}: {', '.join(e['reasons'])}")
                continue
            for c in r["chapters"]:
                if node in c["stale"]:
                    lines.append(f"      #{c['number']} {c['title'] or ''}: "
                                 f"{', '.join(c['stale'][node])}")
                    if node == "audio":
                        lines += [f"          {d}" for d in c.get("diff", [])]
        if r["problems"]:
            lines.append(f"  ({len(r['problems'])} chapter(s) not fully checked"
                         + (")" if not why else "):"))
            if why:
                lines += [f"      #{p['number']}: {p['error']}" for p in r["problems"]]
    stale_n = sum(1 for r in results if r["summary"])
    lines.append(f"{len(results)} series checked; "
                 + (f"{stale_n} with something stale" if stale_n else "nothing stale"))
    return "\n".join(lines)
