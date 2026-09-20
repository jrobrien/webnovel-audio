# Build the real UI, then report what the theme machinery actually did.
#
# catppuccin-dark/catppuccin-light are vendored (ui/theme) and must work
# unconditionally, on any machine, Omarchy or not -- that is the whole point
# of vendoring them. Only the *live* omarchy theme is conditional on
# omarchy::live_available, since that depends on this specific machine.
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl

;# Common assertions for any theme that goes through the omarchy engine
;# (live or vendored) -- everything a consumer of ttk::theme::omarchy gets
;# regardless of which colors.toml produced it.
proc probe_engine_theme {want} {
    apply_theme $want
    puts $::LOG "use $want -> ttk=[ttk::style theme use] active=$::ACTIVE_THEME"
    ;# the bug this exists to catch: `ttk::style theme use` does not update
    ;# ::ttk::currentTheme, only `ttk::setTheme` does, and a Python tkinter
    ;# host reads exactly that variable -- so this is the one assertion
    ;# that would have caught a regression a wish-only test cannot see
    puts $::LOG "  ttk::currentTheme $::ttk::currentTheme"
    puts $::LOG "  md.t.bg [.br.md.t cget -background]"
    set rendered [.bl.tv tag configure rendered -foreground]
    set error    [.bl.tv tag configure error -foreground]
    set fetched  [.bl.tv tag configure fetched -foreground]
    set parsed   [.bl.tv tag configure parsed -foreground]
    set skipped  [.bl.tv tag configure skipped -foreground]
    puts $::LOG "  tag.rendered $rendered"
    puts $::LOG "  tag.error $error"
    puts $::LOG "  tag.fetched $fetched"
    puts $::LOG "  tag.parsed $parsed"
    puts $::LOG "  tag.skipped $skipped"
    ;# the four hue-bearing status colours must actually be four colours --
    ;# backed by ttk::theme::omarchy::distinct_hues (exhaustive over the
    ;# theme's palette, contrast-floored); `skipped` is always the muted/dim
    ;# colour, a deliberate de-emphasis rather than a fifth hue to keep apart
    set four [list $rendered $error $fetched $parsed]
    puts $::LOG "  distinct.count [llength [lsort -unique $four]]"
    puts $::LOG "  spread [omarchy::spread $four]"
    ;# a disabled button must be legible -- the omarchy engine's own job here,
    ;# not a local patch (unlike the vendored Forest theme this replaced).
    ;# `style lookup`, not `style map`: lookup resolves state + inheritance
    ;# the way Tk actually renders a widget; map only reports what a style
    ;# configured directly on itself, and this theme sets the disabled
    ;# colour on `.` for TButton to inherit rather than repeating it.
    set dfg [ttk::style lookup TButton -foreground disabled]
    set bg [ttk::style lookup . -background]
    puts $::LOG "  disabled.fg $dfg  bg $bg  distinct [expr {$dfg ne $bg}]"
}

proc report {} {
    puts $::LOG "tk [info patchlevel]"
    puts $::LOG "families [llength [font families]]"
    puts $::LOG "omarchy.live_available [omarchy::live_available]"

    ;# vendored: must work on every machine this suite runs on, not just
    ;# the one they were rendered on
    probe_engine_theme catppuccin-dark
    probe_engine_theme catppuccin-light

    if {[omarchy::live_available]} {
        probe_engine_theme omarchy
    } else {
        puts $::LOG "use omarchy -> SKIPPED (not available on this machine)"
    }

    ;# the true last resort: reached only if even a vendored file fails to
    ;# source. Exercised directly since nothing on a working machine reaches
    ;# it through apply_theme's normal fallback path.
    foreach want {dark light} {
        clam_style $want
        set ::ACTIVE_THEME $want
        repaint_theme
        puts $::LOG "use $want -> ttk=[ttk::style theme use] active=$::ACTIVE_THEME"
        puts $::LOG "  md.t.bg [.br.md.t cget -background]"
        puts $::LOG "  md.t.fg [.br.md.t cget -foreground]"
        set dfg [ttk::style lookup TButton -foreground disabled]
        set bg [ttk::style lookup . -background]
        puts $::LOG "  disabled.fg $dfg  bg $bg  distinct [expr {$dfg ne $bg}]"
    }

    ;# auto must resolve to something real, never an unstyled bare fallback
    apply_theme auto
    puts $::LOG "auto -> ttk=[ttk::style theme use] active=$::ACTIVE_THEME"
    puts $::LOG "DONE"
    close $::LOG
    exit 0
}

after 900 report
