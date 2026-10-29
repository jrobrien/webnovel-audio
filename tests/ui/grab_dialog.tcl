set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl
set FAIL 0
proc check {label ok} {
    if {$ok} { puts $::LOG "ok   $label" } else { puts $::LOG "FAIL $label" ; incr ::FAIL }
}
set ::RAN {}
proc run_cmd {argv {onfinish ""}} { lappend ::RAN $argv }
after 900 {
  if {[catch {
    dlg_grab demo
    check "dialog exists" [winfo exists .grab]
    set ::GRAB_N 7
    do_grab .grab demo
    check "starts grab with the count" [expr {$::RAN eq {{grab --scope demo --count 7}}}]
    check "dialog closed" [expr {![winfo exists .grab]}]
    dlg_grab demo
    set ::GRAB_N abc
    do_grab .grab demo
    check "a bad count runs nothing" [expr {[llength $::RAN] == 1}]
    handle_event [json::parse {{"event":"grab","message":"ingested: demo #1"}}]
    handle_event [json::parse {{"event":"grab","message":"waiting…","quiet":true}}]
    set text [.br.log.t get 1.0 end]
    check "grab message logged" [string match "*ingested: demo #1*" $text]
    check "quiet heartbeat not logged" [expr {![string match "*waiting…*" $text]}]
  } e]} { puts $::LOG "ERROR $e" ; incr ::FAIL }
  puts $::LOG "FAILURES: $::FAIL"
  close $::LOG ; exit [expr {$::FAIL ? 1 : 0}]
}
