# Does the Segments tab show a chapter's .segments.json, compact and raw?
#
# Writes a synthetic segments file next to a fake chapter .md, points one
# tree row at it, and reads back what the pane shows. argv: <logfile> <dir>
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

proc report {} {
    if {[catch {
        set dir [lindex $::argv 1]
        set fh [open [file join $dir 001-one.segments.json] w]
        fconfigure $fh -encoding utf-8
        puts $fh {[
  {"text": "Hector blinked.", "voice": "af_nova", "style": "narration", "rate": 1.0,
   "pitch": 0.0, "pause_after_ms": 380, "kind": "speech", "speaker": ""},
  {"text": "Awaiting directive…", "voice": "af_kore", "style": "machine", "rate": 1.0,
   "pitch": 0.0, "pause_after_ms": 480, "kind": "speech", "speaker": ""},
  {"text": "", "voice": "", "style": "narration", "rate": 1.0, "pitch": 0.0,
   "pause_after_ms": 1400, "kind": "pause", "speaker": ""},
  {"text": "Who's there?", "voice": "am_michael", "style": "dialogue", "rate": 1.0,
   "pitch": 0.0, "pause_after_ms": 420, "kind": "speech", "speaker": "Grando"}
]}
        close $fh
        set ::SERIES probe
        .bl.tv delete [.bl.tv children {}]
        .bl.tv insert {} end -id ch1 -values [list 1 "" "One" rendered "" ""]
        .bl.tv insert {} end -id ch2 -values [list 2 "" "Two" new "" ""]
        set ::MD(ch1,path) [file join $dir 001-one.md]
        set ::MD(ch2,path) ""

        .bl.tv selection set ch1 ; update
        set t .br.seg.t
        puts $::LOG "lines [expr {int([$t index end-1c])}]"
        puts $::LOG "row2 [$t get 2.0 2.end]"
        puts $::LOG "row3 [$t get 3.0 3.end]"
        puts $::LOG "row4 [$t get 4.0 4.end]"
        puts $::LOG "machine_tagged [expr {"hl_key" in [$t tag names 2.10]}]"
        puts $::LOG "pause_dim [expr {"hl_dim" in [$t tag names 3.10]}]"
        puts $::LOG "tab [.br tab .br.seg -text]"
        puts $::LOG "ext [.br.seg.b.ext state]"

        set ::SEGRAW 1 ; load_seg
        puts $::LOG "raw_first [$t get 1.0 1.end]"
        puts $::LOG "raw_key_tagged [expr {"hl_key" in [$t tag names 2.4]}]"
        set ::SEGRAW 0

        .bl.tv selection set ch2 ; update
        puts $::LOG "missing [$t get 1.0 1.end]"
        puts $::LOG "missing_ext [.br.seg.b.ext state]"

        .bl.tv selection set ch1 ; update
        puts $::LOG "menu1 [.ctx entrycget 1 -label]"
        puts $::LOG "menu6 [.ctx entrycget 6 -label]"
        puts $::LOG DONE
    } e]} { puts $::LOG "ERROR: $e\n$::errorInfo" }
    close $::LOG
    exit 0
}
after 300 report
