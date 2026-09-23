# What "Open in $EDITOR" would actually run, for a range of $EDITOR values.
#
# A pure function, so this only has to build the UI once and print. argv:
# <logfile>
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

proc report {} {
    if {[catch {
        puts $::LOG "xdg-terminal-exec [expr {[auto_execok xdg-terminal-exec] ne ""}]"
        foreach raw {{omarchy-launch-editor --inline} nvim hx {code -w} gvim {} micro} {
            puts $::LOG "argv '$raw' -> [editor_argv $raw /tmp/x.md]"
        }
        puts $::LOG "DONE"
    } e]} { puts $::LOG "ERROR: $e" }
    close $::LOG
    exit 0
}

;# Run `cmd` once the UI is actually on screen.
;#
;# These probes used a fixed `after` delay, which is a bet that the UI builds
;# in under N milliseconds. Under load -- a render using every core, which is
;# exactly when the suite gets run -- that bet loses occasionally, the probe
;# reports on a half-built window, and the failure looks like the feature is
;# broken rather than like the probe was early. Wait for the widget instead,
;# with the timeout only as a backstop so a genuine build failure still
;# reports rather than hanging.
proc when_ready {cmd {tries 100}} {
    if {[winfo exists .br.cast.t] && [winfo ismapped .br.cast.t] && $tries > 0} {
        update idletasks
        after idle $cmd
        return
    }
    if {$tries <= 0} { after idle $cmd ; return }
    after 50 [list when_ready $cmd [expr {$tries - 1}]]
}

when_ready report
