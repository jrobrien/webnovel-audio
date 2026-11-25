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
    feed "chapter unchanged (re-render skipped)" {{"event":"chapter","slug":"x","number":8,"title":"Ch 8","result":"ok","stage":"rendered","unchanged":true,"audio_seconds":559.4,"was":"rendered","status":"rendered"}}
    feed "chapter error"  {{"event":"chapter","slug":"x","number":6,"title":"Ch 6","result":"error","stage":"rendered","error":"boom"}}
    feed "chapter dry-run" {{"event":"chapter","slug":"x","number":7,"title":"Ch 7","result":"would-run","stage":"rendered"}}
    feed "locked"         {{"event":"locked","path":"/tmp/sync.lock"}}
    feed "locked (pool)"  {{"event":"locked","pool":"synth","holder":{"pid":42,"command":"grab","what":"render x #3","since":"2026-11-25T14:02:00"},"path":"/tmp/locks/synth.lock"}}
    feed "chapter skipped" {{"event":"chapter","slug":"x","number":9,"title":"Ch 9","result":"skipped","stage":"rendered","reason":"done by another run"}}
    set ::STATUS "rendered #10 — Ch 10"
    feed "waiting"        {{"event":"waiting","pool":"synth","holder":{"pid":42,"command":"grab","what":"render x #3","since":"2026-11-25T14:02:00"},"what":"render x #10"}}
    if {![string match "waiting for synth (held by render x #3, pid 42, since 14:02)" $::STATUS]} { puts $::LOG "FAIL waiting status: $::STATUS" ; incr ::FAIL }
    feed "acquired"       {{"event":"acquired","pool":"synth","waited_s":12.5}}
    if {$::STATUS ne "rendered #10 — Ch 10"} { puts $::LOG "FAIL acquired did not restore the status: $::STATUS" ; incr ::FAIL }
    feed "waiting (no holder record)" {{"event":"waiting","pool":"library","holder":{}}}
    feed "acquired (after)" {{"event":"acquired","pool":"library","waited_s":0.2}}
    feed "done"           {{"event":"done","stage":"rendered","done":2,"errors":1,"skipped":0}}
    feed "unknown event"  {{"event":"something-new","x":1}}
    puts $::LOG "\n--- rendered log ---"
    puts $::LOG [string trim [.br.log.t get 1.0 end]]
    if {![string match "*#8 unchanged*" [.br.log.t get 1.0 end]]} { puts $::LOG "FAIL unchanged line missing from the log" ; incr ::FAIL }
    if {![string match "*#9 skipped — done by another run*" [.br.log.t get 1.0 end]]} { puts $::LOG "FAIL skipped line missing from the log" ; incr ::FAIL }
    if {![string match "*refused: synth is held by render x #3*" [.br.log.t get 1.0 end]]} { puts $::LOG "FAIL locked line does not name the pool" ; incr ::FAIL }
    puts $::LOG "\nFAILURES: $::FAIL"
    close $::LOG ; exit [expr {$::FAIL ? 1 : 0}]
}
