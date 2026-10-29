set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl
set FAIL 0
proc check {label ok} {
    if {$ok} { puts $::LOG "ok   $label" } else { puts $::LOG "FAIL $label" ; incr ::FAIL }
}
set ::RAN {}
proc run_cmd {argv {onfinish ""}} { lappend ::RAN [list $argv $onfinish] }
proc sync_estimate {args} {}
after 900 {
  if {[catch {
    array unset ::SBROW ; array unset ::SON
    array set ::SBROW {rr 1 rr2 1 plain 0 paused_rr 1}
    array set ::SON   {rr 1 rr2 1 plain 1 paused_rr 0}

    # the global sync: the browser run, then an ordinary sync for the rest
    dlg_sync @all
    check "option offered" [winfo exists .sync.br]
    check "on by default" [expr {$::SYNC_BROWSER == 1}]
    set ::LIMIT 3
    .sync.b.ok invoke
    lassign [lindex $::RAN 0] a then
    check "global: grab, scope omitted" [expr {$a eq "grab --limit 3"}]
    check "global: then a plain sync for what is not read in a browser" \
        [string match "run_cmd *sync --scope @all --yes --limit 3*" $then]

    # nothing but browser series: no follow-up sync
    set ::RAN {}
    set ::SON(plain) 0
    dlg_sync @all
    set ::LIMIT 0
    .sync.b.ok invoke
    lassign [lindex $::RAN 0] a then
    check "no limit: bare grab, no follow-up" [expr {$a eq "grab" && $then eq ""}]

    # a named series that is paused is synced, not refused
    set ::RAN {}
    dlg_sync paused_rr
    check "paused named series opens the dialog" [winfo exists .sync]
    set ::LIMIT 5
    .sync.b.ok invoke
    check "named: grab with its scope" [expr {[lindex $::RAN 0 0] eq "grab --scope paused_rr --limit 5"}]

    # a plain series, or the option turned off: the ordinary sync
    set ::RAN {}
    dlg_sync plain
    check "no browser option for a plain series" [expr {![winfo ismapped .sync.br]}]
    .sync.b.ok invoke
    check "plain: sync" [string match "sync --scope plain --yes*" [lindex $::RAN 0 0]]
    set ::RAN {}
    dlg_sync rr
    set ::SYNC_BROWSER 0
    .sync.b.ok invoke
    check "option off: sync" [string match "sync --scope rr --yes*" [lindex $::RAN 0 0]]

    # only paused browser series: the global sync offers nothing browser-ish
    array set ::SON {rr 0 rr2 0}
    set ::SYNC_SCOPE @all
    check "global ignores paused series" [expr {![sync_has_browser_series]}]
  } e]} { puts $::LOG "ERROR $e" ; incr ::FAIL }
  puts $::LOG "FAILURES: $::FAIL"
  close $::LOG ; exit [expr {$::FAIL ? 1 : 0}]
}
