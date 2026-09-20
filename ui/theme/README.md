# Vendored fallback themes

Two static, self-contained ttk themes — [Catppuccin](https://catppuccin.com)
Mocha (dark) and Latte (light) — used when no live Omarchy theme is available
(see `ui/omarchy.tcl`). Rendered once, from a sibling project on this
machine, and checked in: nothing here calls out to Omarchy, or to anything
else, at runtime.

```sh
cd ~/Projects/tk-omarchy-theme
./bin/tk-omarchy-render themed/ttk.tcl.tpl catppuccin       > catppuccin-dark.tcl
./bin/tk-omarchy-render themed/ttk.tcl.tpl catppuccin-latte > catppuccin-light.tcl
```

`tk-omarchy-render` reads an Omarchy theme's `colors.toml` (here, Omarchy's
own bundled `catppuccin` / `catppuccin-latte`, which already carry the real
Catppuccin Mocha/Latte hex values — `#1e1e2e` / `#eff1f5` and the rest) and
substitutes them into `themed/ttk.tcl.tpl`, the same template a live Omarchy
theme switch renders. The output is the whole point of vendoring: a plain
`.tcl` file that creates ttk theme `omarchy` (parent `clam`), runs
`tk_setPalette` for the classic widgets, and exposes `color` / `is_dark` /
`repaint` / `distinct_hues` / `spread` / `contrast` — identical API to a live
theme, because it is the identical template. `ui/omarchy.tcl`'s adapter does
not know or care whether the file it sourced came from `~/.local/state/…` or
from here.

**Both vendored files register ttk theme name `omarchy`.** That is
deliberate, not a naming collision to fix: only one of {live Omarchy,
catppuccin-dark, catppuccin-light} is ever sourced at a time, selected by
`apply_theme`, and re-sourcing a different one is the documented reload path
(`theme settings` is re-appliable) — the exact mechanism a live `omarchy
theme set` reload already relies on. Do not rename the theme inside these
files; nothing here depends on the filename, but plenty depends on the
in-file name staying `omarchy`.

**Regenerating:** re-run the two commands above and overwrite. There is
deliberately no build step and no dependency on `tk-omarchy-theme` at
webnovel-audio's runtime — only at vendoring time, on whichever machine
happens to have both repos checked out.
