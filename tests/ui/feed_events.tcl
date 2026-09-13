set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl
set FAIL 0
proc feed {label line} {
    if {[catch {handle_event [json::parse $line] $line} e]} {
        puts $::LOG "FAIL $label: $e" ; incr ::FAIL
    } else { puts $::LOG "ok   $label" }
}
after 900 {
    ;# every event shape run_stage / _cmd_sync can emit
    feed "start"          {{"event":"start","stage":"rendered","series":1,"dry_run":false,"range":null}}
    feed "series"         {{"event":"series","slug":"x","title":"Demo","pending":3}}
    feed "chapter_begin"  {{"event":"chapter_begin","slug":"x","number":4,"title":"Ch 4","stage":"rendered"}}
    feed "chapter ok"     {{"event":"chapter","slug":"x","number":4,"title":"Ch 4","result":"ok","stage":"rendered","path":"/a.opus","audio_seconds":559.4,"elapsed_seconds":112.0}}
    feed "chapter ok (no audio: fetch/parse)" {{"event":"chapter","slug":"x","number":5,"title":"Ch 5","result":"ok","stage":"parsed","elapsed_seconds":0.1}}
    feed "chapter error"  {{"event":"chapter","slug":"x","number":6,"title":"Ch 6","result":"error","stage":"rendered","error":"boom"}}
    feed "chapter dry-run" {{"event":"chapter","slug":"x","number":7,"title":"Ch 7","result":"would-run","stage":"rendered"}}
    feed "locked"         {{"event":"locked","path":"/tmp/sync.lock"}}
    feed "done"           {{"event":"done","stage":"rendered","done":2,"errors":1,"skipped":0}}
    feed "unknown event"  {{"event":"something-new","x":1}}
    puts $::LOG "\n--- rendered log ---"
    puts $::LOG [string trim [.br.log.t get 1.0 end]]
    puts $::LOG "\nFAILURES: $::FAIL"
    close $::LOG ; exit [expr {$::FAIL ? 1 : 0}]
}
