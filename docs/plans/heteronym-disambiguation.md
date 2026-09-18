# Plan: heteronym disambiguation

Status: **implemented** (2026-09-16), **unified into the lexicon** (2026-09-17). `tagger.py`, `data/lexicons/_base.csv`, the
`_finish` ordering and the `tagger` CLI are in. `en_core_web_sm` installed and
enabled; 216 tests pass. Thirteen now-redundant lexicon rows pruned (below).
Benchmark scripts live in `tools/`.

Prompted by `read` being audibly wrong in narration — simple past rendered as
`/riːd/`. Confirmed: espeak gets past-tense `read` right **36% of the time**.

## Summary

`heteronyms.py` flags 74 words for human review and never corrects one. The
open question was whether espeak already handles them, and whether a POS tagger
would do better. Measured on Google's
[WikipediaHomographData](https://github.com/google-research-datasets/WikipediaHomographData)
(Apache-2.0; 162 homographs, 16,102 hand-labelled sentences, ~100 per word):

| | accuracy |
|---|---|
| espeak-ng, as the render path calls it | **74.4%** |
| always guessing the commoner reading | **84.8%** |
| spaCy `en_core_web_sm` + POS rules | **96.2%** |
| spaCy `en_core_web_md` + POS rules | **97.0%** |

**espeak is 10 points worse than a constant lookup table.** Its context
heuristics are net negative on this data. For **85 of 155 words it never
produces more than one reading at all**, so there is no espeak-side behaviour
to tune — the only levers are a static pin or a tagger.

Worse, the deficit is concentrated exactly where a tagger helps: on the 92
words whose readings split on part of speech, espeak scores 69.4% against an
83.2% baseline.

## The `read` case

```
read_present -> read_present   59     present-tense recall: 100% (59/59)
read_past    -> read_present   35     PAST-tense    recall:  36% (20/55)
read_past    -> read_past      20
```

espeak handles `has read` and `often read by`, but a bare simple past with no
auxiliary — "he read it straight through", "the sign read 'EPO Cheats Out'" —
falls through to `/riːd/`. That construction dominates in fiction, so the
real-world rate is likely below 36%. spaCy's `VBD`/`VBN` tag lifts it to
**85.6%**.

`wound` is the same shape (both readings are verbs) and needs the lemma, not
just the tag: `VBD` lemmatizing to `wind` -> `/waʊnd/`.

## Where the 74 stand

- **23 words where espeak is fixed and wrong** — no context heuristic exists:
  `advocate articulate bass combine compact compress contrast decrease
  delegate discount duplicate entrance excuse import incline insult invalid
  lead minute moderate resume row subject`
- **The `-ate` cluster is a systematic hole.** espeak emits the `/eɪt/` verb
  form unconditionally, so every adjective/noun use (`a delegate`, `an
  advocate`) is wrong. Nine of the fourteen worst words are this one pattern.
- **~22 words espeak already gets essentially right** and that need nothing:
  `console protest polish convict subject moderate entrance row desert house
  contrast contract perfect separate conflict project record contest compound
  rebel defect discount wind`

## Model choice

Tagging only (`exclude=["ner", "parser", "senter"]`), pinned to one core while
a render held the other 15:

| | disk | load | throughput | per 2,800-word chapter |
|---|---|---|---|---|
| `en_core_web_sm` | 15.2 MB | ~0.3 s | 7,700-8,000 w/s | ~0.36 s |
| `en_core_web_md` | 56.5 MB | ~0.8-1.2 s | 7,400-9,200 w/s | ~0.30-0.38 s |

Throughput is within run-to-run noise (±10%) — `md` is **not** meaningfully
slower to run, only slower to load and 3.7x larger on disk. It buys +0.8pp.

Either way the cost is **under half a second against a 3-5 minute render** —
about 0.2% of chapter wall time. Runtime is not the deciding factor; the
~50 MB model download versus "no model download; every decision is visible"
is. `sm` at 96.2% is the sane default; `md` is not worth 3.7x the disk for
0.8pp. Note `en_core_web_lg` was not tested — `md` already shows the curve
flattening.

## Resolver detail

Mapping a tag to a reading needs one non-obvious fallback. An `ADJ` tag on a
word whose senses are only verb|noun (attributive nouns get tagged `ADJ`)
initially abstained 3.5% of the time. Falling back to the nominal reading
instead of abstaining is worth **+3.2pp** (93.0% -> 96.2%) and drops
abstentions to 0.1%.

Per word, spaCy beats espeak on 85, loses on 4 (`moped retard deviate
deliberate`), and ties within 2pp on 37.

## One table, not two (2026-09-17)

The heteronym rules and the pronunciation lexicon were separate files with
separate formats, applied one after the other with the lexicon barred from
spans the tagger had touched. That was two precedence rules to hold in your
head, and it produced a whole class of bug: a word pinned for one part of
speech and left to espeak for the others.

They are now one CSV and one resolver (`lexicon.py`):

    surface,pos,respell,notes

An empty `pos` means "whatever the tag" — the default reading a part-of-speech
rule then refines. Most specific wins: phrase, then fine tag + lemma, then fine
tag, then coarse POS, then no POS; and at equal specificity a per-series file
beats the base. `Lexicon.match_spans` and the two-stage ordering in
`segment._finish` are gone — `_finish` is one call.

What this changed, beyond the merge:

- **Tagging is required.** `pipeline._load_nlp` exits with the install command
  rather than letting espeak decide. The `Tagger | None` branching goes away.
- **Matching is case-insensitive**, with a leading capital carried onto the
  respelling. `Chi`/`chi` and `Qi`/`qi` collapse to one row each. The all-caps
  branch was deliberately dropped: espeak reads an all-caps token as letter
  names, so a shouted "LIVE" pinned to "liv" would come out "ell eye vee".
- **The `ipa` column is gone.** It was reserved for a backend that never
  arrived, and IPA is not a useful thing to hand-write here.
- **`general.tagger_rules` is gone**; the rules are in the normal base/series
  chain.
- Old four-column files still load unchanged — `DictReader` keys on each
  file's own header, so a missing `pos` column reads as empty, which is
  exactly "applies always". The shipped files were migrated anyway.

## Rule coverage is audited, not assumed (2026-09-17)

Pinning only one side of a word is how a bug ships. `live` had an ADJ rule and
no VERB rule, so when the blanket `live` -> `liv` row was pruned from
sky-pride's lexicon the tagger correctly declined and espeak decided — and
espeak flips to /laɪv/ under subject-auxiliary inversion ("Will he live?")
while reading "he will live" correctly. 34 segments in 31 chapters were wrong.

`tools/audit_heteronym_rules.py` detects this class without reference data:
for these words the part of speech *determines* the reading, so any uncovered
(surface, POS) class that comes out with more than one pronunciation across the
library is espeak guessing, and any class voiced identically to a *different*
POS that is pinned is consistently wrong. It asks the tagger itself whether a
rule already fires, rather than comparing coarse POS against the rule's column
— otherwise every fine-tag rule (`read,VBD`) looks uncovered.

Two things it must get right, both learned the hard way:

- **Keep stress.** Half these words differ only in which syllable carries it
  (IM-port / im-PORT). A stress-blind comparison reports nothing for the whole
  stress-shift family, and wrongly flags `import` as colliding.
- **Verify alignment.** The target word is located by phonemizing the prefix
  and counting espeak's own tokens, which slips on hyphenated neighbours
  ("re-read"). A reading that does not begin with the word's own first phoneme
  is a slip, not a pronunciation. Punctuation must be trimmed too — espeak
  spells `*` out as "asterisk".

First run found 8 classes; six were fixed with verb-side rules (`excuse`
`insult` `estimate` `moderate` `subject` `contrast`), two are documented in
the lexicon as deliberately left alone.

## Caveats

- **Wikipedia, not fiction.** Its sense distribution is skewed hard (`row` is
  100% one sense here, `desert` 100%). That inflates the majority baseline, so
  espeak's real-world deficit is probably *larger* in prose, not smaller.
- 8.8% of sentences dropped on phoneme alignment; per-word `n` is 57-114.
- Sense matching is edit distance over IPA after normalizing espeak's
  conventions against the dataset's. Spot-checked by hand on ~10 words.

## What shipped

`tagger.load()` returns `Tagger | None`, mirroring `Lexicon | None`; nothing
imports spaCy at module scope. 29 rules in `data/lexicons/_base.csv`, each with the
edit distance to the reference IPA recorded in its notes column. Four words
(`articulate` `aggregate` `elaborate` `associate`) are listed as rejected: every
respelling that reaches the right vowel is mangled by espeak (best d=4-7), so
they are left alone rather than made worse.

Ordering in `segment._finish` is tagger-then-lexicon, with the tagger barred
from any span `Lexicon.match_spans` reports. Running the lexicon first would
feed the tagger already-respelled text ("lyve music"); running it last without
the span guard would let a tag silently beat a hand-written row.

CLI: `tagger install|remove|status|test`. One model installed at a time —
switching replaces it, which is cheap at 15/57 MB and removes all the state a
version matrix would need.

### Lexicon rows pruned as a result

- **`_base.csv`: 11 of 19 rows.** Every `live X` phrase row — `live music`,
  `live wire`, `live stream`, … — measured **zero occurrences across all 226
  chapters**. Speculative seeds, and `live/ADJ` covers the class anyway.
- **`sky-pride/lexicon.csv`: 2 rows.** The blanket `live` -> `liv` pin was
  right 137 times and wrong 4 (3 real adjectives plus one spaCy mis-tag):
  "live adders", "live weapons", "a live branch" were all voiced /lɪv/.

**Not pruned:** sky-pride's `tear` rows. All 14 bare `tear` occurrences are the
rip sense, so `tear` -> `tare` is correct 14/14, and the teardrop-side phrase
rows carry real weight. The tagger deliberately never touches `tear`. That is
the intended division: grammar to the tagger, meaning to the lexicon.

## Interjections — a separate problem, measured 2026-09-16

Scanned by `tools/scan_interjections.py` over all 226 chapters / 530,610 words.

Interjections are **not** a heteronym problem and POS tagging does nothing for
them. A heteronym is ambiguous *within* espeak's vocabulary; a vowel-less
interjection falls *outside* it, hits no g2p rule, and espeak reads the letters
aloud.

**59 occurrences across 16 spellings, in 47 of 226 chapters.**

```
  39 mmm       -> ˌɛmˌɛmˈɛm                    "EM EM EM"
   6 shh       -> ˌɛsˌeɪtʃˈeɪtʃ                "ESS AITCH AITCH"
   1 tch       -> tˌiːsˌiːˈeɪtʃ                "TEE CEE AITCH"
   1 hrmph     -> ˌeɪtʃˌɑːɹɹˈɛmpˌiːˈeɪtʃ       "AITCH ARR EM PEE AITCH"
   1 hssggkrk  -> ˌeɪtʃˌɛsˈɛsdʒˌiːdʒˌiːkˈeɪ…   (a creature noise)
   … mmph, mmhmm, mmmhmm, hmf, hrmf, sh, llm, pln, gvnrrch, ghhkrk, xxxxxxxl
```

`mmm` alone is 39 of the 59. Concentrated in `sky-pride` (43 affected
chapters); `chasing-sunlight` has none.

The dividing line is clean: **any vowel and espeak is fine** — `oh` `ah` `huh`
`um` `uh` `ugh` `yay` `bah` all phonemize correctly, and `hmm` -> `həm` is
right because espeak has a rule for that one specific spelling. Vowel-less and
it spells out. So the rule of thumb is simply *does the token contain a vowel*.

### It is fixable with the lexicon as it stands

No new machinery — these are whole-word respellings, exactly what
`data/lexicons/_base.csv` is for. Verified through the render path's espeak:

| surface | respell | before | after |
|---|---|---|---|
| `mmm` | `hmmm` | ˌɛmˌɛmˈɛm | hˈəm |
| `shh`, `sh` | `shush` | ˌɛsˌeɪtʃˈeɪtʃ | ʃˈʌʃ |
| `tch`, `tsk` | `tut` | tˌiːsˌiːˈeɪtʃ | tˈʌt |
| `hmf`, `hrmf`, `hrmph` | `humf` | ˌeɪtʃˌɛmˈɛf | hˈʌmf |
| `mmph` | `mumf` | ˌɛmˌɛmpˌiːˈeɪtʃ | mˈʌmf |
| `mmhmm` | `uh-hmmm` | ˌɛmˌɛmˈeɪtʃˌɛmˈɛm | ˈʌhˈəm |
| `mmmhmm` | `hmmm-hmmm` | ˌɛmˌɛmˈɛmˌeɪtʃˌɛmˈɛm | hˈəmhˈəm |
| `uhh` | `uh` | ˈuː ("oo") | ˈʌ |

`shh` -> `shush` is a compromise: it adds a vowel espeak won't otherwise
produce, rather than the pure /ʃː/ the text means. Judge by ear before
committing it.

Not written to `_base.csv` — these are the project's first *non-name* base
entries, and `mmm` is case-sensitive-whole-word, so `Mmm` needs its own row
the way `Chi`/`chi` and `Qi`/`qi` already do.

### A softer second class, not auto-flagged

The scanner only detects the letter-spelling failure, which is unambiguous.
Separately espeak produces a *wrong but pronounceable* reading for a few:
`tsk` -> tˈəsk ("tuhsk"), `tsch` -> tˈiːʃ ("teesh"), `uhh` -> ˈuː ("oo"),
`eeeehhhhhhhh` -> ˈiːiː (most of it dropped). These need an ear, not a rule.
`eh` -> ˈeɪ ("ay") is *not* in this class — that is a legitimate reading.

### Scale check

59 occurrences in 530,610 words is 0.011% — about one every four chapters. The
argument for fixing it is not frequency but salience: every instance is inside
dialogue, and a voice spelling "EM EM EM" mid-line is far more jarring than a
mispronounced noun.

## Reproducing

```sh
.venv/bin/python tools/bench_heteronyms_espeak.py        # fetches the corpus
uv venv --python 3.12 /tmp/spacyenv                      # NOT the project venv
VIRTUAL_ENV=/tmp/spacyenv uv pip install spacy
VIRTUAL_ENV=/tmp/spacyenv uv pip install \
  https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
/tmp/spacyenv/bin/python tools/bench_heteronyms_spacy.py
```

spaCy must **not** go into `.venv` — that venv's `phonemizer`/`espeakng-loader`
versions are part of the synth fingerprint, and a stray resolve would
re-phonemize the library.

The espeak bench also doubles as a **regression check for g2p upgrades**:
`synth/kokoro.py` can already tell you the phonemizer version *changed*, but
not whether the change was harmless. Re-run and diff per-word accuracy from
`results.json` to answer that.
