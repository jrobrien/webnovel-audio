# Plan: cache maintenance subsystem

Status: **proposed**, not implemented. Measurements taken 2026-09-13 against a
9.13 GB / 28,195-segment cache.

## Why

There is no eviction, no TTL, no size cap, and no `cache` command.
`pipeline._cache_path` writes a `.wav` and nothing ever deletes it. The only
removal path is `series forget --purge`, which drops `library/<slug>` and does
**not** touch `.cache/segments` — so a forgotten series strands its entire cache
footprint permanently, unreachable *and* unattributable.

Measured state after five days:

```
segment cache   9.13 GB   28,195 wavs      never pruned
finished .opus  820 MB                     the actual product
```

The cache is 11x larger than the audiobooks it produced, for two compounding
reasons:

1. Segments are written `subtype="FLOAT"` — 32-bit float, 24 kHz mono,
   ~96 KB/s, averaging 339 KB for ~3.6 s of speech.
2. Dead keys are immortal. Every lexicon edit, cast change or `[synth] speed`
   tweak orphans the old keys and nothing reaps them.

Orphan share measured by replaying cache keys from the live `*.segments.json`:

```
live      26,256   8.25 GB
orphaned   1,939   0.88 GB   (10%)
```

That 10% accumulated in five days of light editing. That is the growth rate.

## The shared primitive

Everything here rests on one function, because "orphan" is the only hard
question:

```python
# cache.py
def live_keys(cfg) -> dict[str, set[str]]:
    """sha1 -> {series slugs that reference it}, replayed from *.segments.json"""
```

It reconstructs `sha1(f"{text}|{voice}|{style}|{rate}|{pitch}|{sr}")` from every
`library/*/*.segments.json` — exactly what `pipeline._cache_path` does at write
time. Anything on disk and not in that map is unreachable.

**This is also the subsystem's one real hazard.** The live set is derived from
`segments.json` files, so a missing script makes live entries *look* orphaned.
Delete `library/sky-pride/` and prune, and 5.86 GB of valid cache is destroyed
silently. See Safety.

Measured: cross-series sharing is **28 keys, 0.1%**. Per-series accounting is
therefore honest and `--series` scoping is safe with a simple
"referenced only by this slug" rule.

## What the cache key is missing

`cache_key_material()` covers `text|voice|style|rate|pitch` + sample rate.
Verified genuinely covered — `segment.py` sets `rate=cfg.synth.speed` on every
segment, so `[synth] speed` really is in the key. Four things change the audio
without touching any of them:

| gap | why it bites |
|---|---|
| `kokoro-v1.0.onnx` bytes | a model bump re-synthesizes nothing |
| `voices-v1.0.bin` bytes | voice embeddings change independently of the model |
| `lang` (`"en-us"`) | pinned in `KokoroSynth.__init__`, never reaches the key |
| the g2p toolchain | **the sneaky one** — an espeak-ng upgrade re-phonemizes |

The last is why this shouldn't wait. `espeakng-loader` ships its own
`libespeak-ng.so`, so a routine `uv sync` can change pronunciation across the
whole library with no model bump, no config change and no visible signal. The
`_base.csv` respellings are tuned against espeak-ng 1.52.0 specifically.

### The fingerprint

Add to the `Synthesizer` protocol in `synth/base.py`:

```python
class Synthesizer(Protocol):
    sample_rate: int
    fingerprint: str          # short, stable id for "what produces this audio"
```

`KokoroSynth` computes it at construction from canonical material:

```
kokoro|model=<sha256(onnx)[:16]>|voices=<sha256(bin)[:16]>|lang=en-us
      |espeak=1.52.0|phonemizer=3.4.0|kokoro_onnx=0.6.1|sr=24000
```

Hashed to **12 hex chars**. `NullSynth` returns `null-<sample_rate>-<wps>`.

Cost is negligible: hashing the 325 MB onnx plus the 28 MB bin measured
**0.18 s** warm, maybe ~1.5 s cold — once per process, against a much slower
model load. Memoize on `(path, mtime, size)` if even that annoys.

Deliberately **excludes** `onnxruntime` version: a numerics-level dependency,
not a semantic one. Including it would churn generations on every routine
upgrade for differences below the noise floor.

### Generations as directories, not hash input

```
.cache/segments/kokoro/<fingerprint>/<sha1>.flac
                       └── .fingerprint.json   # the full material, expanded
```

Four things fall out of this that folding into the sha1 would not give:

1. **Reaping a stale generation is `rm -rf` on a directory** — no
   `segments.json` replay, so the dangerous path isn't involved in the
   model-bump case at all.
2. `status` can show generations side by side, and `.fingerprint.json` lets it
   report *why* they differ ("espeak 1.52.0 -> 1.53.0", not "hash changed").
3. **Orphan detection stays one-dimensional.** Within the current generation,
   orphan = not in the live set. In every other generation, everything is stale
   by definition. Two modes, cleanly separated by risk.
4. The existing 28,195 flat files are visibly "generation unknown" rather than
   silently colliding.

### Per-chapter provenance

The fingerprint exposes something currently invisible: after a model bump,
re-rendering one chapter gives it new-model audio while its siblings keep the
old. **The book becomes acoustically inconsistent mid-series** and nothing says
so.

Record `chapters.synth_fingerprint` at render time; add `SYNTH_MODEL` to the
Opus tags alongside the existing `RENDERED_AT`. Then `status` and `series show`
can warn:

```
  sky-pride   rendered across 2 synth generations  (a3f1c8d29e04 x53, 7b2e1f x38)
              -> render sky-pride --force   to unify
```

Back-fillable from the current generation, since only one model has ever
existed on this disk.

## Command surface

`cache` as a top-level group, matching `series` / `state` / `models`
(noun-first plumbing groups), each taking `--json` via the existing `_cfg_json`:

```
cache status  [--series X] [--json]        report, never writes
cache prune   [--stale] [-n] [-y]          orphans; --stale drops old generations
cache clear   [--series X] [-y]            delete reachable entries too
cache compact [-n] [-y]                    wav -> flac, migrate into generation dir
cache verify  [--repair] [--json]          truncated / unreadable entries
```

### `cache status`

The command that justifies the rest:

```
segment cache  .cache/segments/kokoro
  generation   a3f1c8d29e04   (current)
    live         26,256    8.25 GB
    orphaned      1,939    0.88 GB  (10%)      -> cache prune
  generation   <unmigrated>                    -> cache compact
    flat wav     28,195    9.13 GB

  by series (exclusive)
    sky-pride          19,095   5.86 GB
    hidden-healer       5,454   1.52 GB
    chasing-sunlight    2,236   0.86 GB
    shared                 28   0.01 GB

  format       28,195 wav / 0 flac             -> cache compact frees ~5.8 GB
```

Naming the follow-up command in the text output is what makes this drivable by
a small model: the report states the remedy rather than requiring the agent to
know it. `--json` returns the same shape.

### `cache prune`

Deletes orphans only. `-n/--dry-run` lists what would go and exits 0 without
touching disk; dry-run should be the *only* way to see the file list, since
printing 1,939 paths on a real run is noise.

`--series` is **deliberately absent**: an orphan has no owner by definition, so
`prune --series X` cannot mean anything coherent. Scoping belongs on `clear`.

`--stale` drops whole non-current generation directories. Safe — no live-set
replay, no manifest guard needed. This is the "I upgraded, reclaim the space"
command.

### `cache clear`

The blunt one; removes live entries too, accepting a cold re-render.

- `cache clear` — whole cache. Requires `-y` or an interactive confirm quoting
  **re-render cost in minutes** (`sync.estimate_render` already computes it),
  not just bytes.
- `cache clear --series sky-pride` — keys referenced *only* by that slug. The
  0.1% shared keys stay, which is the correct and safe direction.

This is also the fix for the existing leak: **`series forget --purge` must call
`clear --series` before `db.forget()`**, while the segment scripts still exist
to be read.

### `cache compact` — the FLAC migration

Measured on a real cached segment (2.1 s, 200 KB wav):

| | size | vs wav | max error | enc/dec |
|---|---|---|---|---|
| FLAC/PCM_16 | 59 KB | **29.4%** | 1.5e-05 (-96 dBFS) | 2 ms / 1 ms |
| FLAC/PCM_24 | 109 KB | 54.4% | 6.0e-08 (-144 dBFS) | 1 ms / 1 ms |

`soundfile` FLAC has **no float subtype** (PCM_8/16/24 only), so this is a
quantization, not a pure container swap. That is the one real decision.

**Recommendation: PCM_16.** These segments are downstream of nothing but
loudnorm and a ~48 kbps Opus encode; a -96 dBFS floor sits far below what Opus
itself introduces, and even with +20 dB of loudnorm gain lands at -76 dBFS.
Kokoro output is bounded well under unity (0.741 peak on the sample), so there
is no clipping risk. That is **3.4x** — 8.25 GB of live cache becomes ~2.4 GB.
PCM_24 at 1.8x is the squeamish option.

**The key does not change** — only the extension and the parent directory.

1. read `<sha1>.wav` (flat, legacy)
2. write `<current-fp>/<sha1>.flac` at PCM_16
3. verify the round trip, then unlink the wav

Only one model has ever been on this disk (both files downloaded 2026-09-08,
single copy in `~/.cache/webnovel-audio`), so attributing every existing segment
to the current fingerprint is **correct, not an assumption**.

Lookup during and after migration: try `<fp>/<sha1>.flac`, then
`<fp>/<sha1>.wav`, then flat `<sha1>.wav`. Writes always produce
`<fp>/<sha1>.flac`. Resumable, interruptible, idempotent.

### `cache verify`

Lower value but cheap: `sf.info()` every entry, report unreadable or
zero-length ones. Catches a segment written during an interrupted render, which
currently would be reused forever as silence or a truncated clip, invisibly.
`--repair` unlinks the bad ones so the next render re-synthesizes them.

## Safety

Four guards, in order of trust:

1. **`sync_lock(cfg)` around every destructive action.** Pruning concurrently
   with a render could unlink a key the render just wrote. The lock already
   exists and is non-blocking, so `prune` during a sync fails fast with the
   existing structured error rather than corrupting anything.
2. **Refuse to prune on an implausible live set.** If `live_keys` returns more
   than ~20% fewer keys than the last recorded count, abort with
   `cache_live_set_suspect` and require `--force`. This catches the
   missing-`segments.json` footgun. Needs one number persisted — a
   `.cache/segments/.manifest.json` written on each successful prune is enough,
   and makes `status` faster too. **Applies only to current-generation prune**;
   `--stale` doesn't need it, since staleness is a directory fact, not an
   inference.
3. **Per-series scoping only ever deletes exclusively-owned keys.** Never
   delete a key another series still references.
4. **`clear` always confirms** unless `-y`, quoting re-render cost in minutes.

Structured errors reuse the existing `_fail` helper: `cache_locked`,
`cache_live_set_suspect`, `no_such_generation`, and the existing `_no_series`.

## Net effect

```
today          9.13 GB
after prune    8.25 GB
after compact  ~2.4 GB     (live set at FLAC/PCM_16)
```

From 11x the size of the finished audiobooks to ~3x, with no re-synthesis and
no audible change — and the result is now *attributable*, so a future espeak or
model bump degrades into reclaimable garbage instead of silent staleness.

## Sequencing

`compact` is the biggest win and the lowest risk — no deletion semantics, no
live-set reasoning, fully resumable. `prune` carries all the danger and
recovers the least.

1. **`cache status`** — read-only, immediately useful, and the test harness for
   `live_keys` + fingerprint. Land it and eyeball its numbers before anything
   deletes.
2. **Fingerprint + `cache compact`** — protocol change, generation dirs, FLAC,
   one pass over 28k files. ~5.8 GB, no deletion semantics.
3. **`chapters.synth_fingerprint`** + `SYNTH_MODEL` Opus tag + the
   mixed-generation warning.
4. **`cache prune --stale`** — safe mode first.
5. **`cache prune`** (orphans) + manifest guard.
6. **`cache clear`**, then wire into `series forget --purge`.
7. **`cache verify`** if still worth it.

Steps 1-3 are the coherent first chunk: they free the space, close the
correctness hole, and nothing in them can lose data.

## One migration, not two

The main scheduling argument. `cache compact` already rewrites every file for
the FLAC change; making it also place output into `<fingerprint>/` costs
nothing extra. Doing the fingerprint later would mean a second full pass over
28k files, or living with an unattributable flat generation forever.
