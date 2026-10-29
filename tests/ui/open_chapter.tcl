set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl
set FAIL 0
proc check {label ok} {
    if {$ok} { puts $::LOG "ok   $label" } else { puts $::LOG "FAIL $label" ; incr ::FAIL }
}
set ::OPENED {}
proc open_url {url} { lappend ::OPENED $url }
after 900 {
  if {[catch {
    .bl.tv insert {} end -id chtest -values [list 9999 "" "Five" new "" ""] -tags new
    set ::MD(chtest,url) "https://www.royalroad.com/fiction/1/x/chapter/9/five"
    open_chapter
    check "nothing selected opens nothing" [expr {$::OPENED eq ""}]
    .bl.tv selection set chtest
    open_chapter
    check "plain open" [expr {[lindex $::OPENED 0] eq "https://www.royalroad.com/fiction/1/x/chapter/9/five"}]
    open_chapter 10
    check "grab marker" [expr {[lindex $::OPENED 1] eq "https://www.royalroad.com/fiction/1/x/chapter/9/five#wa-grab=10"}]
    set ::SURL(demo) "https://www.royalroad.com/fiction/1/x"
    .top.tv insert {} end -id demo -values [list demo]
    .top.tv selection set demo
    open_source_and_save
    check "series page marker" [expr {[lindex $::OPENED 2] eq "https://www.royalroad.com/fiction/1/x#wa-index"}]
    ;# the right-click handler itself, with the popup stubbed out
    proc tk_popup {args} { lappend ::POPS $args }
    .bl.tv selection set chtest
    chapter_ctx 10 10 5 5
    check "chapter menu opens for one chapter" [expr {[llength $::POPS] == 1}]
    check "browser entries enabled for one" [expr {[.ctx entrycget end -state] eq "normal"}]
    .bl.tv insert {} end -id chtest2 -values [list 9998 "" "Six" new "" ""] -tags new
    .bl.tv selection set [list chtest chtest2]
    chapter_ctx 10 10 5 5
    check "browser entries disabled for two" [expr {[.ctx entrycget end -state] eq "disabled" && [.ctx entrycget [expr {[.ctx index end]-1}] -state] eq "disabled"}]
    check "menu entries exist" [expr {[.ctx entrycget end -label] eq "Open in browser and save the next 10"}]
  } e]} { puts $::LOG "ERROR $e" ; incr ::FAIL }
    puts $::LOG "FAILURES: $::FAIL"
    close $::LOG ; exit [expr {$::FAIL ? 1 : 0}]
}
