# Adapter onto the Omarchy ttk theme (github.com jro/tk-omarchy-theme), so this
# UI comes up the same colour as the terminal it was launched from.
#
# The theme itself is not this file's business. Sourcing the file it generates
# is completely self-contained: it creates ttk theme "omarchy" (parent clam),
# selects it, runs tk_setPalette for the classic widgets, and walks the
# existing widget tree fixing up Text/Listbox/Entry/Spinbox/Menu — all before
# `source` returns. Re-sourcing is the documented reload path (`omarchy theme
# set` changes the file on disk; nothing re-sources it for you), and it is
# safe to call more than once: `theme create` is guarded, `theme settings` and
# `theme use` are cheap and idempotent style calls.
#
# What this file adds on top:
#   - locate the generated file (or a WEBNOVEL_AUDIO_TK_THEME override, for
#     testing a theme other than the live one)
#   - translate its colour vocabulary into the keys repaint_theme paints our
#     own widgets with (syntax-highlight tags, chapter-stage row colours —
#     nothing the omarchy theme itself knows or needs to know about)
#
# Once that project's installer exists, Tk loads the theme on its own via
# `*TkTheme: omarchy` in the X resource database, with no application code at
# all. Sourcing the file directly, as this does, works the same whether or
# not that installer is present, and is what lets `ui/host.py`'s Python
# interpreter (which never sees the X resource database the way `wish` does)
# pick it up too.
namespace eval omarchy {}

;# Where a generated theme might be, most authoritative first. The real path
;# honours XDG_STATE_HOME per docs/consuming.md in the theme project —
;# Omarchy's own contract, not ours to hardcode around.
proc omarchy::candidates {} {
    set out {}
    if {[info exists ::env(WEBNOVEL_AUDIO_TK_THEME)]} {
        lappend out $::env(WEBNOVEL_AUDIO_TK_THEME)
    }
    set state [expr {[info exists ::env(XDG_STATE_HOME)] && $::env(XDG_STATE_HOME) ne ""
        ? $::env(XDG_STATE_HOME) : [file join $::env(HOME) .local state]}]
    lappend out [file join $state omarchy current theme ttk.tcl]
    return $out
}

;# A quick existence check for theme_resolve, which only picks a name and
;# must not have side effects — the actual `source` (which does) happens once
;# apply_theme has committed to the "omarchy" branch.
proc omarchy::available {} {
    foreach f [candidates] { if {[file readable $f]} { return 1 } }
    return 0
}

;# Source the first candidate that exists. Returns 1 on success. The sourced
;# file leaves ttk theme "omarchy" selected and every classic widget it knows
;# about repainted; the caller (apply_theme) still needs to run our own
;# repaint_theme for the widgets and tags that are ours, not theirs.
proc omarchy::load {} {
    foreach f [candidates] {
        if {![file readable $f]} continue
        if {[catch {uplevel #0 [list source -encoding utf-8 $f]} e]} {
            log "! omarchy theme $f: $e"
            continue
        }
        return 1
    }
    return 0
}

proc omarchy::color {name {fallback ""}} {
    if {[catch {ttk::theme::omarchy::color $name} v]} { return $fallback }
    return [expr {$v eq "" ? $fallback : $v}]
}

;# Thin wrappers over the theme's own exhaustive picker (docs/consuming.md).
;# Not reimplemented here: an earlier version of this file did its own
;# greedy furthest-point selection, which the theme's author measured losing
;# to plain hue naming on three themes (seeded with one colour, never
;# recovers from a bad seed). Exhaustive over an 8-9 candidate pool has no
;# such failure mode, and folds in a WCAG contrast floor against the
;# background that the greedy version never had at all.
proc omarchy::distinct_hues {n {fallback ""}} {
    if {[catch {ttk::theme::omarchy::distinct_hues $n} v] || [llength $v] < $n} {
        return $fallback
    }
    return $v
}

proc omarchy::spread {hexes} {
    if {[catch {ttk::theme::omarchy::spread $hexes} v]} { return -1 }
    return $v
}

;# The live Omarchy palette, translated into the keys repaint_theme expects.
;# Call after a successful `load` — reads whatever ttk::theme::omarchy::color
;# has cached, does not source anything itself.
proc omarchy::palette {} {
    set fg    [color fg  "#e0e0e0"]
    set bg    [color bg  "#1e1e1e"]
    set field [color field $bg]
    set dim   [color muted [color disabledfg $fg]]

    ;# Deliberately NOT red=error, green=rendered: those are terminal palette
    ;# slots, not a categorical scale, and a theme is free to collapse them —
    ;# measured elsewhere at deltaE 0.0, one colour wearing all five hats.
    ;# distinct_hues gives the best four the theme can supply, full stop;
    ;# which stage gets which is an arbitrary but stable (same theme, same
    ;# order every run) assignment. Colour is the redundant channel here —
    ;# the Stage column's text is the one that actually carries the meaning.
    set named [list [color red $fg] [color green $fg] \
                    [color yellow $fg] [color blue $fg]]
    set hues [distinct_hues 4 $named]
    if {$hues eq ""} {
        ;# a cached theme file from before distinct_hues existed: fall back
        ;# to plain naming rather than fail. Still correct, just not immune
        ;# to the collision distinct_hues exists to avoid.
        log "! omarchy theme has no distinct_hues (stale cache?) — using named colours"
        set hues $named
    }
    lassign $hues rendered error fetched parsed
    set s [spread $hues]
    if {$s >= 0 && $s < 25} {
        log "! theme colours for chapter stages are hard to tell apart (spread $s) — relying on the Stage column text"
    }

    return [dict create \
        bg $bg  fg $fg  field $field  dim $dim \
        sel   [color selectbg [color accent $bg]] \
        selfg [color selectfg $fg] \
        rendered $rendered  error $error  skipped $dim \
        fetched  $fetched   parsed $parsed  off $dim \
        com $dim \
        key [color orange [color yellow $fg]] \
        str [color green $fg] \
        head [color accent [color blue $fg]] \
        em [color bright_fg $fg]]
}
