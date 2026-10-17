# The Service dialog: which actions each state offers, confirmations on the
# disruptive ones, and the port-conflict warning. The CLI is stubbed
# (run_json_soft records argv and returns canned JSON) and the browser launch and
# message boxes are replaced, so nothing real is started, stopped or opened.
# argv: <logfile>
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

set ::CALLS {}
set ::OPENED {}
set ::ASKED {}
set ::ANSWER yes
proc open_url {url} { lappend ::OPENED $url }
proc tk_messageBox {args} { lappend ::ASKED [dict get $args -message] ; return $::ANSWER }
proc run_json {args} { return [json::parse {{"series": [], "chapters": []}}] }

# the state the stub reports; each action moves it along like the real service does
set ::ST not_installed
proc status_json {} {
    switch $::ST {
        not_installed { return {{"ok": true, "installed": false, "active": false, "enabled": false,
            "state": "not installed", "url": "http://localhost:8080/", "port": 8080}} }
        busy { return {{"ok": true, "installed": true, "active": false, "enabled": true,
            "state": "inactive (dead)", "url": "http://localhost:8080/", "port": 8080,
            "port_busy": true, "port_owner": {"name": "webnovel-audio", "pid": 4242}, "log": "boom"}} }
        stopped { return {{"ok": true, "installed": true, "active": false, "enabled": false,
            "state": "inactive (dead)", "url": "http://localhost:8080/", "port": 8080, "log": "last words"}} }
        running { return {{"ok": true, "installed": true, "active": true, "enabled": true,
            "state": "active (running)", "pid": 777, "since": "Mon 01:00",
            "url": "http://localhost:8080/", "port": 8080, "log": "serving\n@ESC@[40;37;1m██ ▄▄ ▀▀@ESC@[0m\n@ESC@[40;37;1m▀▀ ██ ▄▄@ESC@[0m\nafter"}} }
    }
}
proc run_json_soft {args} {
    lappend ::CALLS $args
    if {[lindex $args 0] ne "service"} { return [json::parse {{"ok": true}}] }
    switch [lindex $args 1] {
        status  { return [json::parse [string map [list @ESC@ [format %c 27]] [status_json]]] }
        install { set ::ST stopped }
        enable  { if {$::ST eq "busy"} { return [json::parse {{"ok": false, "error": {"code": "port_in_use", "message": "port 8080 is already in use"}}}] } ; set ::ST running }
        disable { set ::ST stopped }
        restart { }
        remove  { set ::ST not_installed }
    }
    return [json::parse {{"ok": true}}]
}
proc buttons {} {
    set out {}
    foreach c [lsort -command {apply {{a b} {expr {[string compare $a $b]}}}} [winfo children .svc.b]] {
        lappend out "[$c cget -text]([$c cget -state])"
    }
    return [join [lsort -dictionary $out] " | "]
}
proc press {text} {
    foreach c [winfo children .svc.b] { if {[$c cget -text] eq $text} { $c invoke ; update ; return 1 } }
    error "no button $text in [buttons]"
}
proc report {} {
    if {[catch {
        puts $::LOG "toolbar [.tool.svc cget -text] exists_old=[winfo exists .tool.srv] oldprocs=[llength [info procs server_start]]"
        set ::ST not_installed ; dlg_service ; update
        puts $::LOG "s0 [.svc.s cget -text] || [buttons]"
        press "Install and start"
        puts $::LOG "s1 [.svc.s cget -text] || [buttons]"
        puts $::LOG "installed_calls [lsearch -all -inline -glob $::CALLS {service install*}] [lsearch -all -inline -glob $::CALLS {service enable*}]"
        # restart/disable ask first; "no" does nothing
        set ::CALLS {} ; set ::ANSWER no
        press "Restart…"
        puts $::LOG "restart_no_calls [lsearch -all -inline -glob $::CALLS {service restart*}] asked=[llength $::ASKED]"
        set ::ANSWER yes ; press "Restart…"
        puts $::LOG "restart_yes [lsearch -all -inline -glob $::CALLS {service restart*}]"
        puts $::LOG "ask_text [string map {\n |} [lindex $::ASKED 0]]"
        press "Open player"
        puts $::LOG "opened $::OPENED"
        press "Log…"
        puts $::LOG "log_shown [winfo ismapped .svc.l] text=[.svc.l.t get 1.0 1.end]"
        puts $::LOG "log_plain [string map {\n |} [string trim [.svc.l.t get 1.0 end]]]"
        puts $::LOG "log_qr_tag [llength [.svc.l.t tag ranges qr]] bg=[.svc.l.t tag cget qr -background] fg=[.svc.l.t tag cget qr -foreground] after_tagged=[expr {"qr" in [.svc.l.t tag names 4.0]}] first_tagged=[expr {"qr" in [.svc.l.t tag names 2.1]}]"
        puts $::LOG "log_call [lsearch -all -inline -glob $::CALLS {service status --log*}]"
        puts $::LOG "log_widgets font=[.svc.l.t cget -font] yscroll=[winfo exists .svc.l.y] xscroll=[winfo exists .svc.l.x]"
        # resizing: shrink the window, then grow it back; the pane must follow both ways
        # (the virtual display is small, so grow relative to a shrunken size)
        update idletasks
        set W [winfo width .svc] ; set H [winfo height .svc]
        wm geometry .svc [expr {$W - 250}]x[expr {$H - 200}] ; update
        set w0 [winfo width .svc.l.t] ; set h0 [winfo height .svc.l.t]
        wm geometry .svc ${W}x${H} ; update
        puts $::LOG "log_resized wider=[expr {[winfo width .svc.l.t] > $w0 + 150}] taller=[expr {[winfo height .svc.l.t] > $h0 + 100}]"
        # the scrollbar follows long output: 120 lines, then scroll to the top
        .svc.l.t configure -state normal ; .svc.l.t delete 1.0 end
        for {set i 1} {$i <= 120} {incr i} { .svc.l.t insert end "line $i [string repeat █ 40]\n" }
        .svc.l.t configure -state disabled ; .svc.l.t yview moveto 0 ; update
        puts $::LOG "log_scrolls [expr {[lindex [.svc.l.y get] 1] < 1.0}]"
        press "Hide log"
        puts $::LOG "log_hidden [winfo ismapped .svc.l]"
        press "Disable…"
        puts $::LOG "s2 [.svc.s cget -text] || [buttons]"
        # port held by something else: warned, and Enable is greyed out
        set ::ST busy ; svc_refresh .svc ; update
        puts $::LOG "busy [.svc.s cget -text] || [string map {\n |} [.svc.m cget -text]] || [buttons]"
        # remove asks, then goes back to not-installed
        set ::ST stopped ; svc_refresh .svc ; update
        press "Remove…"
        puts $::LOG "s3 [.svc.s cget -text] || [buttons]"
        # a failing action is reported in the dialog, which stays open
        set ::ST busy ; svc_refresh .svc ; svc_act .svc enable
        puts $::LOG "fail_msg [.svc.m cget -text] open=[winfo exists .svc]"
        puts $::LOG DONE
    } e]} { puts $::LOG "ERROR: $e\n$::errorInfo" }
    close $::LOG
    exit 0
}
after 300 report
