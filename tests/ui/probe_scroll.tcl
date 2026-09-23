# Does selecting a series put the first unfinished chapter on screen?
#
# Drives show_first_unfinished against synthetic chapter lists rather than a
# real series, so the shapes that matter -- a long rendered prefix, nothing
# outstanding, skipped rows, an empty list -- are all reachable without a
# library. argv: <logfile>
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

;# Fill the chapter tree with `statuses`, in order.
proc fill {statuses} {
    .bl.tv delete [.bl.tv children {}]
    set n 0
    foreach st $statuses {
        incr n
        .bl.tv insert {} end -id ch$n -values [list $n "" "Chapter $n" $st "" ""]
    }
    ;# Start every case from the top. Deleting and reinserting does NOT
    ;# reset the view, and sourcing control.tcl runs the real startup --
    ;# which now scrolls the real series' chapter list before these cases
    ;# run, and left its offset behind on the first one.
    .bl.tv yview moveto 0
    update idletasks
}

;# Which rows are actually on screen, as a list of 1-based chapter numbers.
proc visible_rows {} {
    set out {}
    foreach id [.bl.tv children {}] {
        set bbox [.bl.tv bbox $id]
        if {$bbox ne ""} { lappend out [.bl.tv set $id n] }
    }
    return $out
}

proc report {} {
    if {[catch {
        ;# 300 rendered, then the unfinished tail -- the real shape of a long
        ;# series, and the one where the default view is useless.
        set many {}
        for {set i 0} {$i < 300} {incr i} { lappend many rendered }
        fill [concat $many {new new new}]
        puts $::LOG "long.total [llength [.bl.tv children {}]]"
        puts $::LOG "long.before [lindex [visible_rows] 0]"
        show_first_unfinished
        update idletasks
        set vis [visible_rows]
        puts $::LOG "long.visible.first [lindex $vis 0]"
        puts $::LOG "long.visible.last [lindex $vis end]"
        puts $::LOG "long.target_visible [expr {301 in $vis}]"

        ;# The real shape of this machine's longest series: a large rendered
        ;# body and a large unfinished tail. Here the target should land NEAR
        ;# THE TOP, which is the point of the feature -- the previous case
        ;# only proves it is on screen, because with two rows after it the
        ;# scroll clamps at the end of the range and cannot do better.
        set body {}
        for {set i 0} {$i < 302} {incr i} { lappend body rendered }
        set tail {}
        for {set i 0} {$i < 102} {incr i} { lappend tail new }
        fill [concat $body $tail]
        show_first_unfinished
        update idletasks
        set vis [visible_rows]
        puts $::LOG "real.visible.first [lindex $vis 0]"
        puts $::LOG "real.target_offset [expr {303 - [lindex $vis 0]}]"
        puts $::LOG "real.target_visible [expr {303 in $vis}]"

        ;# skipped is a decision already made, not outstanding work
        fill {rendered skipped skipped rendered new}
        show_first_unfinished
        update idletasks
        puts $::LOG "skipped.target_visible [expr {5 in [visible_rows]}]"

        ;# nothing outstanding: must not scroll anywhere
        fill {rendered rendered rendered}
        set before [lindex [.bl.tv yview] 0]
        show_first_unfinished
        update idletasks
        puts $::LOG "done.unmoved [expr {[lindex [.bl.tv yview] 0] == $before}]"

        ;# empty list must not error
        fill {}
        show_first_unfinished
        puts $::LOG "empty.ok 1"

        ;# the first row already being unfinished must not scroll past it
        fill {new rendered rendered}
        show_first_unfinished
        update idletasks
        puts $::LOG "firstrow.visible [expr {1 in [visible_rows]}]"

        ;# The startup shape, and the one the feature was missing.
        ;#
        ;# At startup this is called from `after idle`, which fires before
        ;# the toplevel is mapped: the tree is 1px tall and reports
        ;# `yview {0.0 1.0}`, so it thinks everything is visible and moveto
        ;# does nothing at all. Withdrawing the toplevel reproduces that
        ;# exactly -- and the proc must defer, then land once it is back.
        fill [concat $body $tail]
        .bl.tv yview moveto 0
        wm withdraw .
        update idletasks
        show_first_unfinished
        update
        puts $::LOG "unmapped.height [winfo height .bl.tv]"
        puts $::LOG "unmapped.deferred [expr {[lindex [.bl.tv yview] 0] == 0.0}]"
        wm deiconify .
        for {set i 0} {$i < 150} {incr i} {
            update
            if {[lindex [.bl.tv yview] 0] > 0.0} break
            after 30
        }
        update idletasks
        puts $::LOG "remapped.landed [expr {303 in [visible_rows]}]"

        puts $::LOG "DONE"
    } e]} { puts $::LOG "ERROR: $e" }
    close $::LOG
    exit 0
}

;# Run `cmd` once the UI is actually on screen.
proc when_ready {cmd {tries 100}} {
    if {[winfo exists .bl.tv] && [winfo ismapped .bl.tv] && $tries > 0} {
        update idletasks
        after idle $cmd
        return
    }
    if {$tries <= 0} { after idle $cmd ; return }
    after 50 [list when_ready $cmd [expr {$tries - 1}]]
}

when_ready report
