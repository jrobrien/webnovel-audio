# Do run events edit the series and chapter lists in place?
#
# Builds a series row and three chapters by hand, feeds synthetic events through
# handle_event, and reads the cells back. No subprocess runs: that is the point.
# argv: <logfile>
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
proc bgerror {m} { puts $::LOG "BGERROR: $m" ; close $::LOG ; exit 1 }
source ui/control.tcl

set ::SUBPROCS 0
proc run_json {args} { incr ::SUBPROCS ; return "" }

proc ev {json} { handle_event [json::parse $json] $json }
proc series_cells {slug} { return [lmap c {rendered pending err} {.top.tv set $slug $c}] }
proc chap {n col} { return [.bl.tv set ch$n $col] }
proc out {k v} { puts $::LOG "$k $v" }

proc report {} {
    if {[catch {
        .top.tv delete [.top.tv children {}]
        .top.tv insert {} end -id demo -values [list Demo on 100 ONGOING 1 2 0 ""]
        .top.tv insert {} end -id other -values [list Other on 100 ONGOING 5 5 0 ""]
        set ::SERIES demo
        .bl.tv delete [.bl.tv children {}]
        foreach {n st} {1 rendered 3 new 5 new} {
            .bl.tv insert {} end -id ch$n -tags $st -values [list $n "" "Ch $n" $st "" ""]
            set ::MD(ch$n,path) "" ; set ::MD(ch$n,status) $st
        }
        .bl.tv configure -displaycolumns {n title status dur}

        update ; set ::SUBPROCS 0          ;# startup/selection refreshes are not under test
        ev {{"event":"series","slug":"demo","title":"Demo","pending":3,"counts":{"rendered":1,"errors":0,"pending":3}}}
        out series_counts [series_cells demo]

        ev {{"event":"chapter_begin","slug":"demo","number":3,"title":"Ch 3","stage":"rendered"}}
        out begin_status [chap 3 status]

        ev {{"event":"chapter","slug":"demo","number":3,"title":"Ch 3","result":"ok","stage":"rendered","was":"new","status":"rendered","audio_seconds":125.4,"text_path":"/x/003.md"}}
        out done_status [chap 3 status]
        out done_dur [chap 3 dur]
        out done_tag [.bl.tv item ch3 -tags]
        out done_md $::MD(ch3,path)
        out done_series [series_cells demo]

        ;# re-parsing a chapter that is already rendered changes nothing
        ev {{"event":"chapter","slug":"demo","number":1,"title":"Ch 1","result":"ok","stage":"parsed","was":"rendered","status":"rendered"}}
        out reparse_series [series_cells demo]
        out reparse_status [chap 1 status]

        ;# a failure: error cell + note, and the Note column appears
        ev {{"event":"chapter","slug":"demo","number":5,"title":"Ch 5","result":"error","stage":"rendered","was":"new","status":"error","error":"boom"}}
        out err_status [chap 5 status]
        out err_note [chap 5 note]
        out err_series [series_cells demo]
        out err_cols [.bl.tv cget -displaycolumns]

        ;# a chapter found by the sync's list refresh is placed by number
        ev {{"event":"chapter","slug":"demo","number":4,"title":"Ch 4","result":"ok","stage":"rendered","was":"new","status":"rendered","audio_seconds":60}}
        out order [.bl.tv children {}]

        ;# another series: its row moves, the chapter list does not
        ev {{"event":"chapter","slug":"other","number":3,"title":"O 3","result":"ok","stage":"rendered","was":"new","status":"rendered"}}
        out other_series [series_cells other]
        out other_chapters [llength [.bl.tv children {}]]

        ;# unknown series, an older CLI without `status`, and a dry run: all no-ops
        ev {{"event":"chapter","slug":"x","number":1,"title":"X","result":"ok","stage":"rendered","was":"new","status":"rendered"}}
        ev {{"event":"chapter","slug":"demo","number":3,"title":"Ch 3","result":"ok","stage":"rendered"}}
        ev {{"event":"chapter","slug":"demo","number":3,"title":"Ch 3","result":"would-run","stage":"rendered"}}
        out noops_series [series_cells demo]

        out subprocs $::SUBPROCS
        out log_has_live_error [expr {[string match "*live update*" [.br.log.t get 1.0 end]]}]
        puts $::LOG DONE
    } e]} { puts $::LOG "ERROR: $e\n$::errorInfo" }
    close $::LOG
    exit 0
}
after 300 report
