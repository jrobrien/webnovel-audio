set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl
set FAIL 0
proc check {label ok} {
    if {$ok} { puts $::LOG "ok   $label" } else { puts $::LOG "FAIL $label" ; incr ::FAIL }
}
set ::CALLS {}
set ::BOXES {}
proc tk_messageBox {args} { lappend ::BOXES $args }
after 900 {
    ;# chrome works
    proc run_json_soft {args} {
        lappend ::CALLS $args
        return [json::parse {{"ok":true,"cookies":["cf_clearance","techaro.lol-anubis-auth-x"]}}]
    }
    import_cookies
    check "chrome tried first, once" [expr {$::CALLS eq {{login --from-browser chrome}}}]
    check "count logged" [string match "*imported 2 bot-check cookie(s) from chrome*" [.br.log.t get 1.0 end]]
    check "no dialog on success" [expr {$::BOXES eq ""}]

    ;# chrome has no cookies, chromium does
    set ::CALLS {}
    proc run_json_soft {args} {
        lappend ::CALLS $args
        if {[lindex $args end] eq "chrome"} {
            return [json::parse {{"ok":false,"error":{"message":"chrome has no unexpired bot-check cookies"}}}]
        }
        return [json::parse {{"ok":true,"cookies":["cf_clearance"]}}]
    }
    import_cookies
    check "falls back to chromium" [expr {[llength $::CALLS] == 2}]
    check "chromium logged" [string match "*1 bot-check cookie(s) from chromium*" [.br.log.t get 1.0 end]]

    ;# neither works: say why, in the log and a dialog
    proc run_json_soft {args} {
        return [json::parse {{"ok":false,"error":{"message":"no cookie file"}}}]
    }
    import_cookies
    check "failure dialog shown" [expr {[llength $::BOXES] == 1}]
    check "reason in dialog" [string match "*chrome: no cookie file*chromium: no cookie file*" $::BOXES]
    check "failure logged" [string match "*! cookie import failed*" [.br.log.t get 1.0 end]]
    puts $::LOG "FAILURES: $::FAIL"
    close $::LOG ; exit [expr {$::FAIL ? 1 : 0}]
}
