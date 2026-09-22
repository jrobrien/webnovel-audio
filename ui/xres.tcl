# Colour, from the X resource database and nothing else.
#
# Tk already reads the root window's RESOURCE_MANAGER at startup -- even under
# Wayland, via XWayland -- and Omarchy publishes the live desktop palette into
# it on every `omarchy theme set` (tk-omarchy-xresources, in
# github.com jro/tk-omarchy-theme). The desktop's palette is therefore already
# in this process before a single line of ours runs; the work is getting it
# onto the widgets.
#
# Three things are not free:
#
#   1. ttk. On Tk 8.6 it ignores the resource database entirely -- tk9.0's
#      ttk/defaults.tcl grew the block that reads it and 8.6's never had it,
#      so 8.6 comes up compiled-in grey (#d9d9d9) no matter what is on the
#      root window. On BOTH versions it reads the database once, at startup,
#      so an override loaded later is invisible to it. `style_ttk` answers
#      both with the vendored `clamx` theme (ui/vendor) and its `refresh`.
#      Classic widgets need none of this: they honour resources directly, at
#      creation, on every version.
#   2. light/dark, which are the same resource names loaded from a checked-in
#      fragment at a priority that outranks the root window (see `apply`).
#   3. the categorical colours -- chapter-stage rows, syntax highlighting --
#      which no standard X resource name carries (see `palette`). clamx reads
#      the eight structural resources and deliberately stops there: those
#      describe surfaces, and no theme can guess which chapter stage is "red".
namespace eval xres {}

;# Captured at this file's own top level, NOT inside a proc: `info script` is
;# scoped to the innermost `source` frame on the stack, so inside a proc body
;# it reports whatever triggered the call -- and from an event-loop callback
;# (a menu click) there is no source frame at all and it reports "". Bit this
;# exact way once, when the fragment path resolved differently depending on
;# whether the theme was applied at startup or from the Theme menu.
set xres::SELF_DIR [file dirname [file normalize [info script]]]

;# The ttk theme. Sourced by a path relative to THIS script rather than the
;# working directory, which is wherever the user happened to launch from.
;# Vendored on purpose -- see ui/vendor/README.md -- so this UI still comes up
;# themed on a machine with no Omarchy and nothing installed.
source [file join $xres::SELF_DIR vendor clamx.tcl]

;# What ui/vendor/clamx.tcl was vendored at. Upstream bumps this on every
;# change that alters what the file produces, so an unequal version is the
;# signal to re-read its notes -- a silently drifting vendored copy being the
;# failure mode worth designing against.
set xres::CLAMX_VERSION 1.2
if {$ttk::theme::clamx::version ne $::xres::CLAMX_VERSION} {
    puts stderr "note: vendored clamx is $ttk::theme::clamx::version,\
                 this UI was written for $::xres::CLAMX_VERSION"
}

;# Every resource this UI reads, with the fallback to use when the database
;# has nothing -- a bare Tk with no Omarchy, no xrdb, no DISPLAY-side setup
;# at all. The fallbacks are Catppuccin Mocha, matching ui/theme/dark.Xresources.
;#
;# The first block is the standard names Tk and Xt already look up, so they
;# are spelled unqualified and Tk's own widgets find them without us. The
;# `omarchy*` block is the extension contract published in tk-omarchy-theme's
;# docs/consuming.md: light/dark, and the categorical hues that no standard
;# X resource name carries. Prefixed deliberately -- an unqualified `*red`
;# would be handed to every X client with a resource of that name, for
;# nothing gained.
;#
;# `class` matters: `option get` matches EITHER the instance name or the
;# class. The omarchy* extensions have no class of their own, so they are
;# looked up under Foreground, which is what the contract specifies.
;#
;#     resource            class               fallback
set xres::SCHEMA {
    background          Background          #1e1e2e
    foreground          Foreground          #cdd6f4
    activeBackground    Foreground          #3a3b4e
    disabledForeground  DisabledForeground  #767a91
    selectBackground    Foreground          #45475a
    selectForeground    Background          #cdd6f4
    troughColor         Background          #333446
    windowColor         Background          #29293a
    insertBackground    Foreground          #cdd6f4
    highlightColor      HighlightColor      #89b4fa
    omarchyMode         Mode                ""
    omarchyAccent       Foreground          #89b4fa
    omarchyMuted        Foreground          #585b70
    omarchyRed          Foreground          #f38ba8
    omarchyGreen        Foreground          #a6e3a1
    omarchyYellow       Foreground          #f9e2af
    omarchyBlue         Foreground          #89b4fa
    omarchyMagenta      Foreground          #f5c2e7
    omarchyCyan         Foreground          #94e2d5
    omarchyOrange       Foreground          #f6b6ab
}

proc xres::get {name} {
    foreach {res class fallback} $::xres::SCHEMA {
        if {$res ne $name} continue
        set v [option get . $res $class]
        return [expr {$v eq "" ? $fallback : $v}]
    }
    return ""
}

;# The checked-in override fragments. Catppuccin Latte/Mocha in exactly the
;# resource names Omarchy publishes, so "light"/"dark"/"system" are one code
;# path that differs only in who filled the database.
proc xres::fragment {mode} {
    return [file join $::xres::SELF_DIR theme $mode.Xresources]
}

;# Fill the option database for `mode`, which is system|light|dark.
;#
;# "system" adds nothing: Tk loaded the root window's database at startup and
;# that IS the system answer. light/dark load a fragment at `interactive`
;# priority (80), which outranks the root window's entries -- and in Tk's
;# option database priority beats specificity, so a generic `*background`
;# override correctly wins over a more specific `*Text.background` that the
;# desktop put there. Verified both ways on Tcl 9.0.4.
;#
;# Not undoable: once a fragment is added at interactive priority, switching
;# back to "system" cannot remove it, because Tk has no `option delete`. So
;# system is loaded by *reading* the database, and light/dark by writing to
;# it -- which means going back to system needs the values read before any
;# fragment was ever added. SYSTEM is that snapshot, taken at startup.
set xres::SYSTEM {}

;# A floor under the whole palette, for a machine that publishes no X
;# resources at all -- no Omarchy, no xrdb, nothing on the root window.
;#
;# Needed because two different fallback sets now meet: clamx falls back to
;# clam's own light greys, while SCHEMA falls back to Mocha. On a themed
;# machine neither is ever reached and they cannot disagree. On a bare one
;# both are, and the window comes up light-grey ttk wrapped around dark text
;# panes. Loading the dark fragment gives both the same answer instead.
;#
;# At `startupFile` priority (40), which is BELOW the root window's own
;# entries and far below the light/dark overrides at `interactive` (80). So
;# this is a floor and not a preference: anything the desktop actually says
;# outranks it, and it only decides what nobody else has an opinion about.
;#
;# Applied only when the database is entirely bare, never to fill gaps in a
;# partial one. A third-party tool that set `*background` dark and nothing
;# else would otherwise get our light Mocha foreground on it -- or, just as
;# easily, a light background with the same foreground, which is unreadable.
;# All-or-nothing is the case a fallback can actually get right.
proc xres::install_floor {} {
    if {[option get . background Background] ne ""} { return 0 }
    set f [fragment dark]
    if {![file readable $f]} { return 0 }
    if {[catch {option readfile $f startupFile}]} { return 0 }
    return 1
}

proc xres::snapshot {} {
    set out {}
    foreach {res class fallback} $::xres::SCHEMA { dict set out $res [get $res] }
    return $out
}


;# The fragment the desktop itself publishes. Tk read the root window once,
;# at startup, and nothing re-reads it for the life of the process -- so this
;# is what makes "Reload colours" mean something after an `omarchy theme set`
;# in another window. Same file tk-omarchy-xresources merges into the root
;# window, read directly; the path is Omarchy's own contract, honouring
;# XDG_STATE_HOME rather than hardcoding around it.
;#
;# The env override exists to test against a palette other than this
;# machine's live one.
proc xres::live_fragment {} {
    if {[info exists ::env(WEBNOVEL_AUDIO_XRESOURCES)]} {
        return $::env(WEBNOVEL_AUDIO_XRESOURCES)
    }
    set state [expr {[info exists ::env(XDG_STATE_HOME)] && $::env(XDG_STATE_HOME) ne ""
        ? $::env(XDG_STATE_HOME) : [file join $::env(HOME) .local state]}]
    return [file join $state omarchy current theme tk.Xresources]
}

proc xres::apply {mode} {
    ;# Equal priority, so a later `option readfile` wins over an earlier one:
    ;# system re-reading the live fragment and then light overriding it lands
    ;# the right way round, and so does switching back.
    set f [expr {$mode eq "system" ? [live_fragment] : [fragment $mode]}]
    if {![file readable $f]} {
        ;# No live fragment is normal -- it means no Omarchy on this machine.
        ;# The startup snapshot is then the whole of "system": whatever Tk
        ;# already read from the root window, or the SCHEMA fallbacks.
        if {$mode eq "system"} { return $::xres::SYSTEM }
        return ""
    }
    if {[catch {option readfile $f interactive} e]} {
        log "! $f: $e"
        return [expr {$mode eq "system" ? $::xres::SYSTEM : ""}]
    }
    return [snapshot]
}

;# 1 = dark. Prefers the explicit `*omarchyMode` resource, which is the
;# literal `mode` from the theme's colors.toml, and falls back to the
;# luminance of the background -- which is what a desktop that publishes no
;# such resource, or none at all, resolves to.
proc xres::is_dark {vals} {
    set m [string tolower [dict get $vals omarchyMode]]
    if {$m eq "dark"} { return 1 }
    if {$m eq "light"} { return 0 }
    if {[catch {winfo rgb . [dict get $vals background]} rgb]} { return 1 }
    lassign $rgb r g b
    return [expr {(0.299*$r + 0.587*$g + 0.114*$b) / 65535.0 < 0.5}]
}

;# Select clamx and point it at whatever the database now says.
;#
;# clamx (ui/vendor, from tk-omarchy-theme) is clam's geometry with its
;# palette read from the same eight structural resources this file reads. It
;# replaces a hand-written styling pass that had to restate clam's greys one
;# state map at a time -- and kept missing some, because clam bakes its own
;# colours into `map` at theme-definition time where `configure` cannot reach
;# them. The check and radio indicators are hardcoded twice over; the arrows
;# fall back to a compiled-in black that is invisible on a dark window.
;#
;# Run on every apply, not just when the mode changes, and on both Tk
;# versions:
;#
;#   - Tk 8.6's ttk reads no resources at all, so nothing else would colour it.
;#   - On BOTH versions ttk reads the database once, at startup. An override
;#     loaded later is invisible to it until something asks -- which is what
;#     `refresh` is for, and is the hook the whole light/dark switch hangs on.
;#
;# `ttk::setTheme`, not `ttk::style theme use`: only setTheme updates
;# ::ttk::currentTheme, and that variable is exactly what a Python tkinter
;# host's Style().theme_use() reads back. `theme use` leaves it stale, so the
;# UI looks right under wish while the host reports the wrong theme.
proc xres::style_ttk {vals} {
    ;# $vals is deliberately unused: clamx reads the option database itself,
    ;# and giving it a second path to the same values is how the two drift.
    ;# The argument stays because every caller already has the dict and a
    ;# future non-ttk fixup here will want it.
    catch {ttk::setTheme clamx}
    ttk::theme::clamx::refresh
}

;# The resolved values, translated into the keys repaint_theme paints our own
;# widgets and tags with.
;#
;# The four chapter-stage colours are the one thing the resource database is
;# genuinely short of: the eight names Tk's ttk reads are all structural
;# (background, trough, disabled...), none of them categorical. The named
;# hues below are the `omarchy*` extension contract; where they are absent
;# the SCHEMA fallbacks fill in, which is a real palette rather than four
;# copies of the foreground.
;#
;# No contrast-floored picker sits behind this any more: the theme package's
;# distinct_hues went away with the package, so these are the named hues as
;# published. A theme is free to collapse them -- osaka-jade's blue is
;# literally its accent, and this machine's `ethereal` has omarchyBlue ==
;# omarchyAccent -- so two stage colours CAN come out identical. That is
;# survivable only because colour is the redundant channel here: the Stage
;# column's text is what carries the meaning.
;#
;# Deliberately NOT red=error, green=rendered as a contract: a theme is free
;# to collapse those slots (measured elsewhere at deltaE 0.0, one colour
;# wearing five hats). Colour is the redundant channel here -- the Stage
;# column's text is what actually carries the meaning.
proc xres::palette {vals} {
    set fg  [dict get $vals foreground]
    set dim [dict get $vals omarchyMuted]
    return [dict create \
        bg    [dict get $vals background] \
        fg    $fg \
        field [dict get $vals windowColor] \
        dim   $dim \
        sel   [dict get $vals selectBackground] \
        selfg [dict get $vals selectForeground] \
        rendered [dict get $vals omarchyGreen]  error [dict get $vals omarchyRed] \
        fetched  [dict get $vals omarchyBlue]   parsed [dict get $vals omarchyMagenta] \
        skipped $dim  off $dim  com $dim \
        key  [dict get $vals omarchyOrange] \
        str  [dict get $vals omarchyGreen] \
        head [dict get $vals omarchyAccent] \
        em   $fg]
}
