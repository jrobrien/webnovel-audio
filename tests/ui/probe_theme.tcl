# Build the real UI, then report what the theme machinery actually did.
#
# Must pass on a machine with no Omarchy theme installed at all — that is the
# common case for anyone running this test suite off this machine — so the
# omarchy-specific assertions are conditional on omarchy::available, while the
# dark/light fallback and the auto-resolve contract are asserted unconditionally.
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl

proc report {} {
    puts $::LOG "tk [info patchlevel]"
    puts $::LOG "families [llength [font families]]"
    puts $::LOG "omarchy.available [omarchy::available]"

    ;# the fallback must work with no Omarchy theme in the picture at all —
    ;# this is what every machine other than the one this was built on gets
    foreach want {dark light} {
        apply_theme $want
        puts $::LOG "use $want -> ttk=[ttk::style theme use] active=$::ACTIVE_THEME"
        puts $::LOG "  md.t.bg [.br.md.t cget -background]"
        puts $::LOG "  md.t.fg [.br.md.t cget -foreground]"
        puts $::LOG "  tag.rendered [.bl.tv tag configure rendered -foreground]"
        ;# a disabled button must stay legible against clam's own background —
        ;# the whole reason Forest needed a local patch was upstream leaving
        ;# this on the *global* disabled colour, invisible on a dark bg
        set dfg [lindex [ttk::style map TButton -foreground] end]
        set bg [ttk::style lookup . -background]
        puts $::LOG "  $want.disabled.fg $dfg  bg $bg  distinct [expr {$dfg ne $bg}]"
    }

    if {[omarchy::available]} {
        apply_theme omarchy
        puts $::LOG "use omarchy -> ttk=[ttk::style theme use] active=$::ACTIVE_THEME"
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
        ;# the five status colours must actually be five colours -- backed by
        ;# ttk::theme::omarchy::distinct_hues (exhaustive over the theme's
        ;# palette, contrast-floored) for four of them; `skipped` is always
        ;# the muted/dim colour, a deliberate de-emphasis rather than a fifth
        ;# hue to keep apart
        set five [list $rendered $error $fetched $parsed $skipped]
        puts $::LOG "  distinct.count [llength [lsort -unique $five]]"
        puts $::LOG "  spread [omarchy::spread [list $rendered $error $fetched $parsed]]"
    } else {
        puts $::LOG "use omarchy -> SKIPPED (not available on this machine)"
    }

    ;# auto must resolve to something real, never an unstyled bare fallback
    apply_theme auto
    puts $::LOG "auto -> ttk=[ttk::style theme use] active=$::ACTIVE_THEME"
    puts $::LOG "DONE"
    close $::LOG
    exit 0
}

after 900 report
