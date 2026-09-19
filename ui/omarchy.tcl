# Adapter onto the Omarchy ttk theme (github.com jro/tk-omarchy-theme), so this
# UI comes up the same colour as the terminal it was launched from.
#
# The theme itself is not this file's business. Sourcing the file it generates
# is completely self-contained: it creates ttk theme "omarchy" (parent clam),
# selects it, runs tk_setPalette for the classic widgets, and walks the
# existing widget tree fixing up Text/Listbox/Entry/Spinbox — all before
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

proc omarchy::_rgb {hex} {
    if {![regexp {^#([0-9a-fA-F]{6})$} $hex -> h]} { return {0 0 0} }
    return [list [scan [string range $h 0 1] %x] \
                 [scan [string range $h 2 3] %x] \
                 [scan [string range $h 4 5] %x]]
}

proc omarchy::_dist {a b} {
    lassign [_rgb $a] r1 g1 b1
    lassign [_rgb $b] r2 g2 b2
    return [expr {sqrt(($r1-$r2)**2 + ($g1-$g2)**2 + ($b1-$b2)**2)}]
}

;# Pick `n` colours from `pool` that stay apart from each other and from
;# `taken`, greedily by furthest point.
;#
;# The chapter-stage colours cannot just be assigned "blue" and "magenta" and
;# trusted: a theme names its hues after intent, not hue angle, and nothing
;# stops two of them landing close together. Measured, not hypothetical: in
;# osaka-jade, `blue` is #509475 — 15 units (of ~440 possible) from that same
;# theme's `green`, which is already spoken for by the `rendered` row. Picking
;# `fetched`'s colour by nominal name would render it the same colour as
;# `rendered` in that theme specifically. Furthest-point selection keeps every
;# status legible in any theme, at the cost of "fetched" not always being the
;# colour literally called blue — legible beats nominal, since nobody reads
;# that column by checking which hue name a stylesheet gave it.
proc omarchy::_spread {pool taken n} {
    set out {}
    for {set i 0} {$i < $n} {incr i} {
        set best "" ; set bestd -1
        foreach c $pool {
            if {$c eq "" || $c in $out} continue
            set d 1e9
            foreach t [concat $taken $out] {
                if {$t eq ""} continue
                set d [expr {min($d, [_dist $c $t])}]
            }
            if {$d > $bestd} { set bestd $d ; set best $c }
        }
        if {$best eq ""} break
        lappend out $best
    }
    return $out
}

;# The live Omarchy palette, translated into the keys repaint_theme expects.
;# Call after a successful `load` — reads whatever ttk::theme::omarchy::color
;# has cached, does not source anything itself.
proc omarchy::palette {} {
    set fg    [color fg  "#e0e0e0"]
    set bg    [color bg  "#1e1e1e"]
    set field [color field $bg]
    set dim   [color muted [color disabledfg $fg]]
    set green [color green $fg]
    set red   [color red $fg]
    ;# rendered/error keep their named meaning; the other two chapter-stage
    ;# colours are otherwise arbitrary, so let them be whichever named hues
    ;# stay furthest from the ones already spoken for
    set rest [_spread [list [color cyan ""] [color blue ""] [color magenta ""] \
                            [color orange ""] [color yellow ""]] \
                      [list $green $red $dim] 2]
    set fetched [lindex $rest 0] ; set parsed [lindex $rest 1]
    if {$fetched eq ""} { set fetched $fg }
    if {$parsed  eq ""} { set parsed  $fg }
    return [dict create \
        bg $bg  fg $fg  field $field  dim $dim \
        sel   [color selectbg [color accent $bg]] \
        selfg [color selectfg $fg] \
        rendered $green  error $red  skipped $dim \
        fetched  $fetched  parsed $parsed  off $dim \
        com $dim \
        key [color orange [color yellow $fg]] \
        str $green \
        head [color accent [color blue $fg]] \
        em [color bright_fg $fg]]
}
