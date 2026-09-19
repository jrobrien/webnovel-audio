# Build the real UI, then report what the theme machinery actually did.
#
# The bug this exists to catch: the vendored theme failed `package require`
# under Tcl 9, apply_theme fell back to clam, and nothing said so — the UI just
# came up in the wrong colours. Asserting on the *live* theme name and on the
# colours actually applied to the non-ttk widgets is what makes that loud.
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl

proc report {} {
    puts $::LOG "tk [info patchlevel]"
    puts $::LOG "families [llength [font families]]"
    puts $::LOG "themes [ttk::style theme names]"
    foreach want {forest-dark forest-light} {
        apply_theme $want
        puts $::LOG "use $want -> [ttk::style theme use]"
        puts $::LOG "  md.t.bg [.br.md.t cget -background]"
        puts $::LOG "  md.t.fg [.br.md.t cget -foreground]"
        puts $::LOG "  tag.rendered [.bl.tv tag configure rendered -foreground]"
    }
    ;# auto must resolve to one of ours, never the clam fallback
    apply_theme auto
    puts $::LOG "auto -> [ttk::style theme use]"
    puts $::LOG "DONE"
    close $::LOG
    exit 0
}

after 900 report
