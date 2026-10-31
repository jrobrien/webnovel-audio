# The Add dialog's slug verification step and the Rename dialog.
#
# The CLI is stubbed (run_json_soft records argv and returns canned JSON), so
# nothing is fetched or renamed. argv: <logfile>
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

set ::CALLS {}
set ::TAKEN 0
proc run_json {args} { return [json::parse {{"series": [], "chapters": []}}] }
proc run_json_soft {args} {
    lappend ::CALLS $args
    if {[lrange $args 0 1] eq {series add} && "--dry-run" in $args} {
        if {$::TAKEN} {
            return [json::parse {{"ok": true, "dry_run": true, "title": "Sky Pride", "chapters": 400,
                "slug": "sky-pride", "tracked": false, "slug_free": false,
                "slug_problem": "'sky-pride' already belongs to 'Sky Pride'"}}]
        }
        return [json::parse {{"ok": true, "dry_run": true, "title": "Drunken Monkey", "chapters": 105,
            "slug": "between-beast-and-buddha-a-drunken", "tracked": false, "slug_free": true,
            "lead": "female", "tags": ["LitRPG", "Female Lead"]}}]
    }
    if {[lrange $args 0 1] eq {series add}} {
        if {"--slug" in $args && [lindex $args [expr {[lsearch $args --slug] + 1}]] eq "taken"} {
            return [json::parse {{"ok": false, "error": {"code": "slug_taken", "message": "'taken' already belongs to 'X'"}}}]
        }
        return [json::parse {{"ok": true, "slug": "drunken-monkey", "title": "Drunken Monkey", "chapters": 105}}]
    }
    if {[lrange $args 0 1] eq {series rename}} {
        if {[lindex $args [expr {[lsearch $args --slug] + 1}]] eq "taken"} {
            return [json::parse {{"ok": false, "error": {"code": "slug_taken", "message": "'taken' already belongs to 'X'"}}}]
        }
        return [json::parse {{"ok": true, "old_slug": "x", "slug": "y", "moved": true}}]
    }
    return [json::parse {{"ok": true}}]
}
proc labels {m} {
    set out {}
    for {set i 0} {$i <= [$m index end]} {incr i} {
        if {[$m type $i] ne "separator"} { lappend out [$m entrycget $i -label] }
    }
    return $out
}
proc last_call {} { return [lindex $::CALLS end] }

proc report {} {
    if {[catch {
        puts $::LOG "sctx [labels .sctx]"

        # -- add: look up, edit the slug, add
        dlg_add ; update
        puts $::LOG "add_stage0 $::ADD_STAGE(.add) button=[.add.b.ok cget -text] slug_mapped=[winfo ismapped .add.s]"
        .add.u insert 0 "https://www.royalroad.com/fiction/1/x"
        .add.b.ok invoke ; update
        puts $::LOG "lookup_call [last_call]"
        puts $::LOG "add_stage1 $::ADD_STAGE(.add) button=[.add.b.ok cget -text] slug=[.add.s get] title=[.add.t cget -text]"
        puts $::LOG "lead_ui [.add.v.c get] | [.add.v.n cget -text] | mapped=[winfo ismapped .add.v]"
        .add.s delete 0 end ; .add.s insert 0 drunken-monkey
        .add.b.ok invoke ; update
        puts $::LOG "add_call [last_call]"
        puts $::LOG "add_closed [expr {![winfo exists .add]}]"

        # -- add: the proposed slug is taken -> warned, can still be edited
        set ::TAKEN 1
        dlg_add ; update
        .add.u insert 0 "https://www.royalroad.com/fiction/2/y"
        .add.b.ok invoke ; update
        puts $::LOG "taken_msg [.add.m cget -text]"
        .add.s delete 0 end ; .add.s insert 0 taken
        .add.b.ok invoke ; update
        puts $::LOG "add_error_inline [.add.m cget -text] still_open=[winfo exists .add]"
        # editing the URL goes back to the lookup step
        .add.u insert end z ; focus -force .add.u ; update ; event generate .add.u <KeyRelease> -keysym z ; update
        puts $::LOG "add_reset $::ADD_STAGE(.add) button=[.add.b.ok cget -text]"
        destroy .add

        # -- rename
        .top.tv insert {} end -id x -values [list "Series X" on 100 "" 0 0 0 ""]
        .top.tv selection set x ; update
        dlg_rename ; update
        puts $::LOG "ren_entry [.ren.e get] move=$::REN_MOVE"
        .ren.e delete 0 end ; .ren.e insert 0 taken
        .ren.b.ok invoke ; update
        puts $::LOG "ren_error [.ren.m cget -text] still_open=[winfo exists .ren]"
        .ren.e delete 0 end ; .ren.e insert 0 y
        .ren.b.ok invoke ; update
        puts $::LOG "ren_call [last_call]"
        puts $::LOG "ren_closed [expr {![winfo exists .ren]}]"
        puts $::LOG DONE
    } e]} { puts $::LOG "ERROR: $e\n$::errorInfo" }
    close $::LOG
    exit 0
}
after 300 report
