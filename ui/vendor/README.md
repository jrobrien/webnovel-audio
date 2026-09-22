# Vendored: `clamx.tcl`

`clamx` is the ttk theme this UI runs on: clam's geometry and element layout,
with the palette read from the X resource database. Upstream is
[tk-omarchy-theme](https://github.com/jro/tk-omarchy-theme) — `lib/clamx.tcl`,
copied verbatim. **Do not edit it here**; fix it upstream and re-copy.

```sh
cp ~/Projects/tk-omarchy-theme/lib/clamx.tcl ui/vendor/clamx.tcl
```

**Vendored version: 1.2.** `ui/xres.tcl` checks
`$ttk::theme::clamx::version` against the version it was written for and logs
if they differ, so a copy that drifts says so instead of going quietly wrong.
Upstream's policy is that the version bumps on every change that alters what
the file produces, so comparing it is enough — there is no need to diff.

## Why vendored rather than required

Upstream deliberately does not install it: nothing registers it as a package,
no `*TkTheme` names it, and it never auto-loads. That is the whole difference
between this file and the generated `ttk.tcl` that project deleted — that one
was rendered from the *active desktop theme*, so a theme installed from a git
clone could supply its contents and have them execute at user privilege in
every Tk process on the machine. `clamx` is static text an application chooses
to source.

Vendoring also keeps the promise the rest of `ui/` makes: this UI has to come
up looking right on a machine with no Omarchy on it at all. Where a resource
is absent every value falls back to clam's own, so the worst case here is
ordinary clam rather than a failure.

## What it does and does not cover

It reads the eight *structural* resources — `background`, `foreground`,
`windowColor`, `selectBackground`, `selectForeground`, `disabledForeground`,
`troughColor`, `activeBackground` — and nothing else. The `*omarchy*` hues
stay this project's business (`xres::palette`), because structural resources
describe surfaces while those describe meanings, and no theme can guess which
chapter stage is "red".

`ttk::theme::clamx::refresh` re-reads the database and restates every
`configure` and `map`. That is the hook the light/dark switch hangs on: Tk
reads resources once at startup, so an override loaded later is invisible to
ttk until something asks for it. It holds no state between calls and is safe
to call repeatedly, in any order.
