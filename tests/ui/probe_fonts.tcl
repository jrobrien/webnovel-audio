# Build the real UI, then exercise the font-size zoom.
#
# Everything here goes through the same named fonts the UI actually uses, so
# a regression shows up as a number rather than as a screenshot nobody looks
# at. argv: <logfile>
#
# WEBNOVEL_AUDIO_UI_CONF must point somewhere disposable: control.tcl reads
# it at source time, so redirecting it from here would be too late, and the
# save/load round-trip below would write over the developer's real config.
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

proc sizes {} {
    set out {}
    foreach f [lsort [array names ::FONTBASE]] {
        lappend out $f [font configure $f -size]
    }
    return $out
}

proc snap {label} {
    puts $::LOG "$label zoom $::FONTZOOM"
    puts $::LOG "  sizes [sizes]"
    puts $::LOG "  rowheight [ttk::style lookup Treeview -rowheight]"
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

proc report {} {
    if {[catch {
        snap boot

        ;# The keys, delivered to an EDITABLE text pane. The binding lives on
        ;# the toplevel and on Text with `break`, because the toplevel tag
        ;# runs after the class tag: without the Text binding these would
        ;# zoom and also type into the cast/lexicon editors.
        focus .br.cast.t
        .br.cast.t delete 1.0 end
        .br.cast.t insert end "seed"
        update
        foreach seq {<Control-equal> <Control-plus> <Control-KP_Add>} {
            event generate .br.cast.t $seq
        }
        update
        snap "after 3 larger"
        puts $::LOG "  editor.text '[string trim [.br.cast.t get 1.0 end]]'"

        foreach seq {<Control-minus> <Control-underscore>} {
            event generate .br.cast.t $seq
        }
        update
        snap "after 2 smaller"

        ;# Persistence, through the real save/load rather than by poking the
        ;# variable: a zoom that does not survive a restart is the whole bug
        ;# this feature would have.
        save_conf
        set saved $::FONTZOOM
        set ::FONTZOOM 999
        load_conf
        puts $::LOG "roundtrip saved $saved reloaded $::FONTZOOM"

        ;# A hand-edited ui.conf must not take the UI down at startup, before
        ;# the log pane exists to explain itself.
        set fh [open $::UICONF w] ; puts $fh "fontzoom banana" ; close $fh
        set ::FONTZOOM 3
        load_conf
        puts $::LOG "junk.survived [expr {$::FONTZOOM eq 3}]"

        event generate . <Control-Key-0>
        update
        snap "after reset"

        ;# The limits must refuse rather than clamp silently, and must never
        ;# leave a font at a size nobody can read.
        for {set i 0} {$i < 40} {incr i} { font_zoom -1 }
        snap floor
        for {set i 0} {$i < 200} {incr i} { font_zoom 1 }
        snap ceiling

        font_reset
        snap final
        puts $::LOG "DONE"
    } e]} {
        puts $::LOG "ERROR: $e"
    }
    close $::LOG
    exit 0
}

when_ready report
