"""What kind of failure is this? The answer decides what a run does next.

* `ContentError`: this one chapter (page, text) is bad. Mark it `error`, carry on
  with the others; the next run tries it again.
* `DependencyMissing`: the environment cannot do the work (POS tagger, TTS model,
  ffmpeg, espeak, lexicon). It would fail every chapter, so the run stops early with
  one clear message and no chapter is marked. A `SystemExit`, so a command-line
  caller prints the message and exits as it always has.

Anything else a provider raises (a site refusing us, a bot check) stays a plain
`SystemExit`: the run stops.
"""
from __future__ import annotations


class ContentError(Exception):
    """One chapter's content is unusable."""


class DependencyMissing(SystemExit):
    """Something the whole run needs is missing or broken."""
