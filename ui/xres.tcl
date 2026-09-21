# Colour, from the X resource database and nothing else.
#
# There is no ttk theme here, and no theme file to source. Tk already reads
# the root window's RESOURCE_MANAGER at startup -- even under Wayland, via
# XWayland -- and Omarchy publishes the live desktop palette into it on every
# `omarchy theme set` (tk-omarchy-xresources, in github.com jro/tk-omarchy-theme).
# So on Tk 9 a stock `default`-themed application is already the right colour
# before a single line of ours runs. Measured on this machine, uv's Tcl 9.0.4:
# ttk style `.` comes up -background #060B1E -foreground #ffcead
# -selectbackground #252e56, straight from the desktop theme, zero code.
#
# What this file adds is the three things that are not free:
#
#   1. Tk 8.6's ttk ignores the resource database entirely -- tk9.0's
#      ttk/defaults.tcl grew the block that reads it and 8.6's never had it,
#      so 8.6 comes up compiled-in grey (#d9d9d9) no matter what is on the
#      root window. Its *classic* widgets do honour resources. `style_ttk`
#      pushes the resolved values into ttk::style so both versions match.
#   2. light/dark, which are the same resource names loaded from a checked-in
#      fragment at a priority that outranks the root window (see `apply`).
#   3. the categorical colours -- chapter-stage rows, syntax highlighting --
#      which no standard X resource name carries (see `palette`).
namespace eval xres {}

;# Captured at this file's own top level, NOT inside a proc: `info script` is
;# scoped to the innermost `source` frame on the stack, so inside a proc body
;# it reports whatever triggered the call -- and from an event-loop callback
;# (a menu click) there is no source frame at all and it reports "". Bit this
;# exact way once, when the fragment path resolved differently depending on
;# whether the theme was applied at startup or from the Theme menu.
set xres::SELF_DIR [file dirname [file normalize [info script]]]

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

;# Push the resolved values into ttk::style.
;#
;# On Tk 9 with mode=system this is very nearly a no-op -- ttk's `default`
;# theme already read the same resources -- and it is run anyway rather than
;# version-sniffed, because the one case that must work is the one where the
;# two disagree: an override fragment loaded after startup, which ttk does
;# NOT pick up (`option add` does not retint an existing style; measured).
;# Running it unconditionally means light/dark and Tk 8.6 take the identical
;# path, so there is no branch that only one machine ever exercises.
proc xres::style_ttk {vals} {
    set bg    [dict get $vals background]
    set fg    [dict get $vals foreground]
    set field [dict get $vals windowColor]
    set sel   [dict get $vals selectBackground]
    set selfg [dict get $vals selectForeground]
    set dim   [dict get $vals disabledForeground]
    set act   [dict get $vals activeBackground]
    set trough [dict get $vals troughColor]

    ;# ttk::setTheme, not `ttk::style theme use`: only setTheme updates
    ;# ::ttk::currentTheme, and that variable is exactly what a Python
    ;# tkinter host's Style().theme_use() reads. Regress it and the UI looks
    ;# right under wish while a Python host reports the wrong theme.
    ;#
    ;# `default` rather than clam: on Tk 9 it is the only built-in theme that
    ;# reads the resource database at all (clam, alt and classic keep
    ;# hardcoded colours), so leaving it selected is what makes a machine
    ;# with a live desktop palette correct before we touch anything.
    catch {ttk::setTheme default}

    ttk::style configure . -background $bg -foreground $fg \
        -fieldbackground $field -troughcolor $trough -bordercolor $dim \
        -darkcolor $bg -lightcolor $bg \
        -focuscolor [dict get $vals highlightColor] \
        -selectbackground $sel -selectforeground $selfg \
        -insertcolor $fg
    ttk::style map . -foreground [list disabled $dim] \
                     -background [list disabled $bg active $act]

    foreach w {TFrame TLabel TButton TMenubutton TNotebook TScrollbar
               TPanedwindow TSeparator} {
        ttk::style configure $w -background $bg
    }
    ;# The entry family. `-arrowcolor` matters as much as the field colour
    ;# here: Tk's built-in default for a spinbox/combobox arrow is literally
    ;# `black`, which disappears entirely on a dark background -- and it is a
    ;# glyph, so unlike a wrong background it gives no hint that it is there.
    ;# Disabled likewise falls back to a light grey field that flashes bright
    ;# against a dark window.
    foreach w {TEntry TSpinbox TCombobox} {
        ttk::style configure $w -fieldbackground $field -foreground $fg \
            -insertcolor $fg -arrowcolor $fg
        ttk::style map $w -fieldbackground [list disabled $bg readonly $bg] \
            -foreground [list disabled $dim] \
            -arrowcolor [list disabled $dim]
    }
    ttk::style configure Treeview -background $field -fieldbackground $field \
        -foreground $fg
    ttk::style map Treeview -background [list selected $sel] \
                            -foreground [list selected $selfg]
    ;# `-relief raised` on the heading is the chunky-bevel look even once the
    ;# colours match; flatten it rather than guess a derived shade for it.
    ttk::style configure Treeview.Heading -background $bg -foreground $fg \
        -relief flat -borderwidth 1
    ;# Notebook tabs need the UNSELECTED state configured, not just the
    ;# selected one mapped. `ttk::style configure TNotebook` paints the strip
    ;# behind the tabs; the tabs themselves are a separate style with its own
    ;# compiled-in colour, and on Tk 8.6 that colour is #c3c3c3 -- so the
    ;# window came up correct except for a row of grey tabs. Recessed to the
    ;# trough colour, with the selected tab rising to the page background,
    ;# which is the shape the desktop's own palette is describing.
    ttk::style configure TNotebook.Tab -background $trough -foreground $fg
    ttk::style map TNotebook.Tab -background [list selected $bg active $act] \
                                 -foreground [list selected $fg disabled $dim]
    ttk::style map TButton -background [list active $act disabled $bg]
    ttk::style configure TScrollbar -troughcolor $trough -arrowcolor $fg
    ttk::style map TScrollbar -background [list active $sel] \
        -arrowcolor [list disabled $dim]
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
