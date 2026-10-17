# The series right-click menu: Sync… for one series, Delete series… with a
# confirmation, and the toolbar Sync… being series-wide.
#
# The CLI is stubbed (run_json/run_cmd record their argv and return canned
# JSON), so nothing is fetched, rendered or deleted. argv: <logfile>
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

set ::CALLS {}
proc run_json {args} {
    lappend ::CALLS $args
    if {[lrange $args 0 1] eq {series forget} && "--dry-run" in $args} {
        return [json::parse {{"ok": true, "dry_run": true, "slug": "probe",
            "title": "Probe Series", "would_remove": "/lib/probe", "exists": true,
            "bytes": 2900000000, "human": "2.7 GB", "files": 23794,
            "chapters": 107, "rendered": 105,
            "parts": [{"name": ".cache", "bytes": 1, "human": "1.9 GB", "files": 23359},
                      {"name": "chapters", "bytes": 1, "human": "763.7 MB", "files": 321}]}}]
    }
    if {[lrange $args 0 1] eq {series forget}} {
        return [json::parse {{"ok": true, "slug": "probe", "freed_bytes": 2900000000}}]
    }
    if {[lindex $args 0] eq "sync"} {
        return [json::parse {{"chapters": 3, "human": "4m"}}]
    }
    return [json::parse {{"series": [], "chapters": []}}]
}
proc run_cmd {argv {onfinish ""}} { lappend ::RUNCMDS $argv }
# never launch a real browser from a test
set ::OPENED {}
proc open_url {url} { lappend ::OPENED $url }
set ::RUNCMDS {}

proc labels {m} {
    set out {}
    for {set i 0} {$i <= [$m index end]} {incr i} {
        if {[$m type $i] ne "separator"} { lappend out [$m entrycget $i -label] }
    }
    return $out
}

proc report {} {
    if {[catch {
        puts $::LOG "sctx [labels .sctx]"

        .tool.sync invoke
        update
        puts $::LOG "tb_scope $::SYNC_SCOPE"
        puts $::LOG "tb_all_checkbox [winfo exists .sync.all]"
        puts $::LOG "tb_label [.sync.l cget -text]"
        .sync.b.ok invoke
        puts $::LOG "tb_run [lindex $::RUNCMDS end]"

        .top.tv insert {} end -id probe -values [list "Probe Series" on 100 "" 0 0 0 ""]
        .top.tv selection set probe ; update
        # "Open source page": the selected series' URL, and a clear note without one
        set ::SURL(probe) "https://www.royalroad.com/fiction/1/probe"
        open_source
        puts $::LOG "opened $::OPENED"
        set ::OPENED {}
        unset ::SURL(probe)
        open_source
        puts $::LOG "opened_without_url [llength $::OPENED]"
        dlg_sync probe
        update
        puts $::LOG "one_scope $::SYNC_SCOPE"
        puts $::LOG "one_label [.sync.l cget -text]"
        .sync.b.cx invoke

        dlg_delete
        update
        puts $::LOG "del_exists [winfo exists .del]"
        puts $::LOG "del_text [string map {\n |} [.del.l cget -text]]"
        puts $::LOG "del_q [.del.q cget -text]"
        puts $::LOG "del_focus [focus]"
        .del.b.no invoke
        puts $::LOG "del_after_no [winfo exists .del]"
        puts $::LOG "forgot_after_no [expr {[llength [lsearch -all -glob $::CALLS {series forget* --yes*}]]}]"

        dlg_delete
        update
        .del.b.yes invoke
        update
        puts $::LOG "del_after_yes [winfo exists .del]"
        puts $::LOG "forget_call [lindex [lsearch -inline -all -glob $::CALLS {series forget*--yes*}] end]"
        puts $::LOG DONE
    } e]} { puts $::LOG "ERROR: $e\n$::errorInfo" }
    close $::LOG
    exit 0
}
after 300 report
