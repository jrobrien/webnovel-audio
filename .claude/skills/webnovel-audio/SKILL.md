---
name: webnovel-audio
description: >
  How to drive the webnovel-audio CLI in this repo: track a series, fetch /
  parse / check / render chapters, sync, inspect state and progress, edit the
  cast and lexicon, manage the cache, serve the web player. Use for any task
  that runs `webnovel-audio` or asks how to do something with it ("render
  chapters 20-30", "what's the state of X", "add a series", "re-render after a
  config change"). For a mispronounced word use fix-pronunciation instead.
---

# webnovel-audio CLI

Run from the project root as `uv run webnovel-audio ...`. **Do not guess
syntax from memory or from older docs: the CLI is flag-based, never
positional.** `render sky-pride 1-10` fails; `render --target sky-pride
--range 1-10` works. When unsure:

```
uv run webnovel-audio schema                 # every command, flag, help text, as JSON
uv run webnovel-audio <cmd> [<sub>] --help   # one command
```

`schema` is walked out of argparse, so it cannot be stale. Pipe it through `jq`
(e.g. `jq '.detail[] | select(.path==["lex","add"])'`) rather than reading it all.

## Conventions

- **Pipeline verbs** (`fetch parse check render`): `--target T [--range R]`.
  `T` is a series (slug, id or title substring) or a path/URL for a one-off.
- **Everything else that names a series** (`series state cast lex cache sync book
  feed retag progress pron`): `--scope S`. Where optional, omitted or `@all` =
  every enabled series. `lex` also takes `--scope @base` (the always-on lexicon).
- **`--range`**: `N`, `N-M`, `N-`, `-M`, or comma list `1-3,7,20-25`. Given =
  imperative (do exactly these, whatever their state); omitted = declarative
  (do whatever is outstanding).
- `render` skips a chapter whose audio recipe is unchanged; `--force` overrides.
- `--json` on most commands for scripting; failures become
  `{"ok": false, "error": {code, message, hint}}`. Exit 0 ok, 1 failed, 2
  another sync/render holds the lock.
- Only one `sync`/`render` runs at a time (flock). `progress` is read-only and
  safe mid-render.
- Destructive commands (`series forget --purge`, `cache prune|clear`,
  `profile remove`) need `-y` or a terminal "yes"; `-n` previews. Preview first,
  and ask the user before purging.
- Bundles live in `library/<slug>/` (gitignored); `series path --scope S` prints one.

## Common operations

```sh
webnovel-audio series list                                  # dashboard
webnovel-audio series add --url <url> [--from N]            # track (metadata only)
webnovel-audio series enable|disable --scope S              # include/exclude from sync
webnovel-audio series priority --scope S --priority 200     # render order, higher first
webnovel-audio state show --scope S [--range 1-20]          # per-chapter stages
webnovel-audio state reset --scope S                        # errors -> new, to retry

webnovel-audio fetch  --target S --range 1-10
webnovel-audio parse  --target S --range 1-10 [--explain]
webnovel-audio check  --target S --range 2-10               # report only, writes nothing
webnovel-audio render --target S --range 1-10 [--force] [--dry-run] [-o out.opus]

webnovel-audio sync [--scope S] [--limit N] [--estimate] [-y]   # refresh + render outstanding
webnovel-audio progress [--scope S] [--watch]

webnovel-audio cast show|edit --scope S
webnovel-audio cast update --scope S --range 2-10 [--diff]  # add speakers found
webnovel-audio cast set --scope S --speaker NAME --voice ID
webnovel-audio lex list|edit --scope S
webnovel-audio lex add --scope S --surface W --respell R [--pos P] [--note why]
webnovel-audio pron "text" [--scope S]                      # phonemes + gloss

webnovel-audio cache status [--scope S]
webnovel-audio serve [--port N]                             # web player + podcast feeds
webnovel-audio book --scope S [--range R] -o x.m4b
```

Everything else (`profile`, `feed`, `retag`, `series archive|scan|import|migrate|
reclaim`, `voices`, `tagger`, `models`, `login`, `ui`, `config`) is in `schema`.

## Workflows

- **New series**: `series add` -> `fetch`+`parse` the first ~10 -> `check` ->
  `cast update` / `cast edit` / `lex add` -> `render` -> listen -> unpause/`sync`.
- **Re-render after a config or lexicon change**: just `render --target S --range
  A-B`; the recipe changes, so those chapters re-synthesize (cache makes it cheap).
- **A word sounds wrong**: use the `fix-pronunciation` skill.
- **Long renders** (a whole series): run in the background at low priority, and
  check `progress`; don't start a second one while it runs (exit 2).

## Docs

`README.md` (usage), `SETUP.md` (install/config), `docs/DESIGN.md` (internals).
`tests/test_readme_cli.py` fails if a documented command stops parsing; if you
change a flag, update the docs and this skill.
