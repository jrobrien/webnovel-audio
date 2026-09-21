# Light and dark overrides

Two X resource fragments — [Catppuccin](https://catppuccin.com) Mocha (`dark`)
and Latte (`light`) — in exactly the resource names Omarchy publishes to the X
root window. They are not themes and there is no theme engine here: the whole
mechanism is `option readfile` at `interactive` priority, which outranks the
root window's database, so "light" and "dark" are literally "pretend the
desktop said this instead". See `ui/xres.tcl`.

The key set is the contract in `tk-omarchy-theme/docs/consuming.md`:

- the standard names Tk and Xt look up on their own (`*background`,
  `*foreground`, `*selectBackground`, `*troughColor`, `*Text.background`, …),
  which Tk 9's ttk `default` theme and every version's classic widgets read
  with no application code at all;
- `*windowColor`, which only Tk 9 reads, for ttk field backgrounds;
- the `*omarchy*` extension — `*omarchyMode` plus the named hues — which
  carries the light/dark answer and the categorical colours that no standard
  X resource name has.

Because the names match, `system`, `light` and `dark` are one code path that
differs only in who filled the database.

**Values.** Catppuccin Mocha and Latte, the same hexes that were previously
vendored as generated ttk theme files. They are checked in rather than
rendered, so this UI still looks right on a machine with no Omarchy at all —
today, or after switching away from it later. `ui/xres.tcl`'s `SCHEMA` carries
the Mocha values a second time, as the fallback for a resource the database
does not answer at all; keep the two in step.

**Editing.** Plain Xresources syntax, no build step. One caveat, inherited from
the same bug upstream: **the file must end in a newline.** Tk silently discards
a final entry that is not newline-terminated, and which entry sorts last is an
accident of ordering.
