# Vendored ttk theme

[Forest-ttk-theme](https://github.com/rdbende/Forest-ttk-theme) by rdbende,
MIT licensed — see `LICENSE`. Vendored rather than depended on because ttk
themes have no package manager: a theme is a `.tcl` file plus a directory of
PNG sprites that `LoadImages` globs relative to the script's own location.

`control.tcl` sources exactly one of `forest-dark.tcl` / `forest-light.tcl`,
never both. Each file calls `tk_setPalette` from inside its `-settings` block,
which runs at `ttk::style theme create` time rather than on `theme use` — so
sourcing both would leave the classic widgets (`text`, `menu`) painted by
whichever file was sourced last, regardless of the theme actually selected.

## Local patches

### 1. Disabled buttons in `forest-dark.tcl`

Upstream leaves disabled widgets on the global `-disabledfg`, `#595959`.
Against the dark theme's `#313131` background that is invisible: a disabled
button renders as an empty grey rectangle with no label. Two lines from the
[AnjaRy fork](https://github.com/AnjaRy/Forest-ttk-theme) fix it —

```tcl
ttk::style map TButton -foreground [list disabled #bbbbbb]
```

plus a `rect-disabled.png` sprite for the disabled Button element, so the
background dims too. `forest-light.tcl` needs neither: `#595959` on `#ffffff`
reads fine, which is why the fork patches only the dark file.

That fork also adds `apply_theme` / `switch_theme` procs. Those are
deliberately **not** taken: `control.tcl` has its own `apply_theme`, which
has to re-apply the palette on every switch anyway (see the note above about
`tk_setPalette` running only at `theme create` time).

### 2. `package require`

One line is changed from upstream, in **both** `.tcl` files:

```tcl
package require Tk 8.6     ->     package require Tk 8.6-
```

Under Tcl 9, `8.6` means "major 8, minor >= 6", so Tk 9.0.x fails it and the
theme refuses to load. The unbounded `8.6-` means "8.6 or newer" and is
satisfied by both. This matters because the two supported launchers do not
agree on the interpreter: `webnovel-audio ui` execs the system `wish`
(Tk 8.6 here), while `webnovel-audio ui --python` uses Python's bundled
Tcl/Tk, which is 9.0.x. Re-check this line if these files are ever
re-vendored from upstream.
