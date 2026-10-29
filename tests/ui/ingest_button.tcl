set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl
set FAIL 0
proc check {label ok} {
    if {$ok} { puts $::LOG "ok   $label" } else { puts $::LOG "FAIL $label" ; incr ::FAIL }
}
after 900 {
    ;# the Ingest button
    set ::LINES {}
    proc refresh_series {} {} ; proc refresh_chapters {} {}
    proc run_json_soft {args} {
        return [json::parse {{"ok":false,"ingested":1,"results":[{"file":"a.html","result":"ingested","detail":"x #4"},{"file":"b.html","result":"rejected","detail":"bot-check"}]}}]
    }
    do_ingest
    set text [.br.log.t get 1.0 end]
    check "ingest lists each page" [expr {[string match "*a.html: ingested*" $text] && [string match "*b.html: rejected*" $text]}]
    check "ingest summary" [string match "*ingest: 1 of 2 page(s) filed*" $text]
    proc run_json_soft {args} { return [json::parse {{"ok":false,"error":{"message":"folder missing"}}}] }
    do_ingest
    check "ingest failure logged" [string match "*! ingest: folder missing*" [.br.log.t get 1.0 end]]
    puts $::LOG "FAILURES: $::FAIL"
    close $::LOG ; exit [expr {$::FAIL ? 1 : 0}]
}
