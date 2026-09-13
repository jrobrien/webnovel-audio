#!/usr/bin/env wish
# webnovel-audio — control UI.
#
# Layout follows gitk: a vertical panedwindow whose top pane spans the width
# (series), with a horizontal panedwindow below it (chapters | notebook).
#
# It shells the `webnovel-audio` CLI and parses `--json`. No DB or network
# access of its own — every capability here is one you also have from a
# terminal, which is what keeps the two honest.
#
#   webnovel-audio ui          # preferred: finds and exports the CLI path
#   wish ui/control.tcl        # direct

package require Tk
source [file join [file dirname [info script]] json.tcl]

if {![info exists ::env(HOME)] || $::env(HOME) eq ""} {
    puts stderr "webnovel-audio ui: \$HOME is not set"
    exit 1
}
set ::CFGDIR [file join $::env(HOME) .config webnovel-audio]
set ::UICONF [file join $::CFGDIR ui.conf]

;# platform context button — gitk does the same dance for macOS
set ::CTXBUT <Button-3>
if {[tk windowingsystem] eq "aqua"} { set ::CTXBUT <Button-2> }

array set ::geom {main 1280x820 topheight 230 botwidth 760}

set ::CFGPATH "" ; set ::LEXDIR "data/lexicons" ; set ::SERIESDIR "data/series"
set ::BASELEX ""
set ::SERIES "" ; set ::RUNNING 0 ; set ::PIPE "" ; set ::SERVEPID ""
set ::SERVEPIPE "" ; set ::STATUS "ready" ; set ::LIMIT 10
array set ::EDIT {}          ;# tab -> path / mtime / dirty

# ---------------------------------------------------------------- exe ---------
proc find_exe {} {
    if {[info exists ::env(WEBNOVEL_AUDIO)] && [file executable $::env(WEBNOVEL_AUDIO)]} {
        return [file normalize $::env(WEBNOVEL_AUDIO)]
    }
    set here [file dirname [file normalize [info script]]]
    foreach c [list [file join $here .. .venv bin webnovel-audio] \
                    [lindex [auto_execok webnovel-audio] 0]] {
        if {$c ne "" && [file executable $c]} { return [file normalize $c] }
    }
    return webnovel-audio
}
set ::EXE [find_exe]

proc run_json {args} {
    if {[catch {exec -- $::EXE {*}$args --json 2>@1} out]} {
        log "! [lindex [split $out \n] 0]" ; return ""
    }
    if {[catch {json::parse [string trim $out]} d]} {
        log "! bad JSON from: $args" ; return ""
    }
    return $d
}

proc status {msg} { set ::STATUS $msg }

proc log {msg} {
    .br.log.t configure -state normal
    .br.log.t insert end "$msg\n"
    .br.log.t see end
    .br.log.t configure -state disabled
}

# ---------------------------------------------------------------- series ------
proc refresh_series {} {
    set keep [selected_series]
    set d [run_json series list]
    .top.tv delete [.top.tv children {}]
    if {$d eq ""} { status "series list failed" ; return }
    set rows [dict get $d series]
    foreach r $rows {
        set st [dict get $r stages]
        .top.tv insert {} end -id [dict get $r slug] -values [list \
            [dict get $r title] \
            [json::get $r status] \
            [json::get $st rendered] \
            [dict get $r pending] \
            [dict get $r errors] \
            [json::get $r next title]] \
            -tags [expr {[json::get $r enabled] eq "0" ? "off" : "on"}]
    }
    .top.tv tag configure off -foreground gray55
    status "[llength $rows] series"
    if {$keep ne "" && [.top.tv exists $keep]} {
        .top.tv selection set $keep
    } elseif {[llength $rows]} {
        .top.tv selection set [dict get [lindex $rows 0] slug]
    }
}

proc selected_series {} {
    set s [.top.tv selection]
    return [expr {[llength $s] ? [lindex $s 0] : ""}]
}

proc on_series_select {} {
    set slug [selected_series]
    if {$slug eq "" || $slug eq $::SERIES} return
    set ::SERIES $slug
    refresh_chapters
    load_editor cast
    load_editor lex
}

# ---------------------------------------------------------------- chapters ----
proc refresh_chapters {} {
    set keep [.bl.tv selection]
    .bl.tv delete [.bl.tv children {}]
    if {$::SERIES eq ""} return
    set d [run_json state show $::SERIES]
    if {$d eq ""} return
    foreach c [dict get $d chapters] {
        set n [dict get $c number]
        set dur [json::get $c duration_s]
        if {$dur ne "" && $dur > 0} { set dur "[expr {int($dur/60)}]m" } else { set dur "" }
        set st [dict get $c status]
        set note [json::get $c error_stage]
        if {$note ne ""} { append note ": [string range [json::get $c error] 0 48]" }
        .bl.tv insert {} end -id ch$n -values \
            [list $n [dict get $c title] $st $dur $note] -tags $st
    }
    foreach {tag col} {rendered #2a7d4f error #b03030 skipped gray55
                       fetched #4a6fa5 parsed #6a5fa5} {
        .bl.tv tag configure $tag -foreground $col
    }
    foreach id $keep { if {[.bl.tv exists $id]} { .bl.tv selection add $id } }
    status "$::SERIES — [llength [.bl.tv children {}]] chapters"
}

;# selected chapter numbers -> a CLI range like "1-3,7,20-25"
proc selected_range {} {
    set nums {}
    foreach id [.bl.tv selection] { lappend nums [.bl.tv set $id n] }
    if {![llength $nums]} { return "" }
    set nums [lsort -integer -unique $nums]
    set spans {} ; set lo [lindex $nums 0] ; set prev $lo
    foreach n [lrange $nums 1 end] {
        if {$n == $prev + 1} { set prev $n ; continue }
        lappend spans [expr {$lo == $prev ? $lo : "$lo-$prev"}]
        set lo $n ; set prev $n
    }
    lappend spans [expr {$lo == $prev ? $lo : "$lo-$prev"}]
    return [join $spans ","]
}

proc chapter_ctx {X Y x y} {
    set id [.bl.tv identify row $x $y]
    if {$id ne "" && [lsearch -exact [.bl.tv selection] $id] < 0} {
        .bl.tv selection set $id
    }
    set n [llength [.bl.tv selection]]
    .ctx entryconfigure 2 -label "Fetch ($n)"
    .ctx entryconfigure 3 -label "Parse ($n)"
    .ctx entryconfigure 4 -label "Check ($n)"
    .ctx entryconfigure 5 -label "Render ($n)"
    .ctx entryconfigure 7 -label "Update cast from selection ($n)"
    .ctx entryconfigure 0 -state [expr {$n == 1 ? "normal" : "disabled"}]
    tk_popup .ctx $X $Y
}

# ---------------------------------------------------------------- commands ----
;# Run a CLI subcommand, streaming --json events into the log + panes.
proc run_cmd {argv {onfinish ""}} {
    if {$::RUNNING} { log "! busy: a command is already running" ; return }
    set ::RUNNING 1 ; set ::FINISH $onfinish
    ui_busy 1
    .br select .br.log
    log "\$ webnovel-audio $argv"
    set pipe "| [list $::EXE {*}$argv --json 2>@1]"
    if {[catch {open $pipe r} ::PIPE]} {
        log "! cannot launch: $::PIPE"
        set ::RUNNING 0 ; ui_busy 0 ; return
    }
    fconfigure $::PIPE -blocking 0 -buffering line
    fileevent $::PIPE readable cmd_readable
}

proc cmd_readable {} {
    if {[gets $::PIPE line] < 0} {
        if {[eof $::PIPE]} { cmd_finish }
        return
    }
    set line [string trim $line]
    if {$line eq ""} return
    if {[catch {json::parse $line} ev]} { log $line ; return }
    handle_event $ev $line
}

;# Split out of cmd_readable so it can be driven directly with synthetic events.
;# It used to be reachable only through a live subprocess, which is how a broken
;# `expr` in the success branch survived: the tests never completed a chapter.
proc handle_event {ev {raw ""}} {
    switch -- [json::get $ev event] {
        start  { log "start: [json::get $ev stage], [json::get $ev series] series" }
        locked { log "! refused: another render/sync holds the lock"
                 status "refused — another run is active" }
        series { log "  [json::get $ev title]: [json::get $ev pending] to do" }
        chapter_begin {
            set n [json::get $ev number]
            status "[json::get $ev stage] #$n — [json::get $ev title]"
            log "    #$n [json::get $ev title] …"
        }
        chapter {
            set n [json::get $ev number]
            set r [json::get $ev result]
            if {$r eq "error"} {
                log "    #$n ERROR: [json::get $ev error]"
            } elseif {$r eq "would-run"} {
                log "    #$n would [json::get $ev stage] — [json::get $ev title]"
            } else {
                set detail ""
                set a [json::get $ev audio_seconds]
                if {$a ne ""} { set detail " (${a}s audio, [json::get $ev elapsed_seconds]s)" }
                set tot [json::get $ev total_segments]
                set hit [json::get $ev cached_segments]
                if {$tot ne "" && $hit ne "" && $hit > 0} {
                    append detail "  \[$hit/$tot segments cached\]"
                }
                log "    #$n ok$detail"
            }
        }
        done {
            log "done: [json::get $ev done] ok, [json::get $ev errors] error(s)"
        }
        default { if {$raw ne ""} { log $raw } }
    }
}

proc cmd_finish {} {
    catch {fileevent $::PIPE readable {}}
    if {[catch {close $::PIPE} err]} {
        set tail [lindex [split $err \n] end]
        if {[string trim $tail] ne ""} { log "— $tail —" }
    }
    set ::RUNNING 0 ; ui_busy 0
    status "ready"
    refresh_series ; refresh_chapters
    if {$::FINISH ne ""} { uplevel #0 $::FINISH ; set ::FINISH "" }
}

proc stop_cmd {} {
    if {!$::RUNNING} return
    log "stopping…"
    catch {close $::PIPE}
    set ::RUNNING 0 ; ui_busy 0 ; status "stopped"
}

proc ui_busy {on} {
    set st [expr {$on ? "disabled" : "!disabled"}]
    foreach b {.tool.sync .tool.refresh .tool.add} { $b state $st }
    .tool.stop state [expr {$on ? "!disabled" : "disabled"}]
}

;# stage actions over the current chapter selection
proc do_stage {stage} {
    if {$::SERIES eq ""} { log "select a series" ; return }
    set r [selected_range]
    if {$r eq ""} { log "select one or more chapters" ; return }
    ;# `check` and `cast update` return one JSON object, not a stream of events —
    ;# run them synchronously and format the report rather than dumping raw JSON.
    if {$stage eq "check"}  { report_check $r ; return }
    if {$stage eq "cast"}   { report_cast  $r ; return }
    run_cmd [list $stage $::SERIES $r]
}

proc report_check {range} {
    .br select .br.log
    log "\$ webnovel-audio check $::SERIES $range"
    status "checking $range…"
    set d [run_json check $::SERIES $range]
    status "ready"
    if {$d eq ""} return
    set cast [json::get $d cast]
    set new {}
    foreach k [dict keys $cast] {
        if {[json::get [dict get $cast $k] new] ne "0"} { lappend new $k }
    }
    log "  cast: [llength [dict keys $cast]] speaker(s), [llength $new] unmapped"
    foreach k [lrange $new 0 9] {
        set i [dict get $cast $k]
        log "    $k — [json::get $i count] line(s), [json::get $i gender]"
    }
    if {[llength $new]} { log "    -> right-click > Update cast to add them" }
    set het [json::get $d heteronyms]
    if {[llength $het]} {
        log "  heteronyms (judge by ear):"
        foreach h [lrange $het 0 7] {
            log "    [json::get $h word] x[json::get $h count]  …[json::get $h context]…"
        }
    }
    set cand [json::get $d lexicon_candidates]
    if {[llength $cand]} {
        log "  unknown names ([llength $cand]): [join [lrange $cand 0 14] {, }]"
    }
}

proc report_cast {range} {
    .br select .br.log
    log "\$ webnovel-audio cast update $::SERIES $range"
    set d [run_json cast update $::SERIES $range]
    if {$d eq ""} return
    log "  [json::get $d overlay_action]: [json::get $d overlay_path]"
    load_editor cast 1
    .br select .br.cast
}

proc do_state {status} {
    set r [selected_range]
    if {$::SERIES eq "" || $r eq ""} { log "select chapters first" ; return }
    run_json state set $::SERIES $r $status
    refresh_chapters ; refresh_series
}

proc do_reset_errors {} {
    if {$::SERIES eq ""} return
    set r [selected_range]
    if {$r eq ""} { run_json state reset $::SERIES } else { run_json state reset $::SERIES $r }
    refresh_chapters ; refresh_series
}

# ---------------------------------------------------------------- sync --------
proc dlg_sync {} {
    if {$::SERIES eq ""} { log "select a series" ; return }
    set w .sync ; catch {destroy $w}
    toplevel $w ; wm title $w "Sync" ; wm transient $w .
    ttk::label $w.l -text "Sync $::SERIES — chapters to render:"
    ttk::spinbox $w.n -from 0 -to 999 -width 5 -textvariable ::LIMIT
    ttk::label $w.h -text "(0 = everything outstanding)"
    ttk::label $w.est -text "estimating…" -foreground "#2a7d4f"
    ttk::frame $w.b
    ttk::button $w.b.ok -text "Sync" -command [list do_sync $w]
    ttk::button $w.b.cx -text "Cancel" -command [list destroy $w]
    pack $w.b.ok $w.b.cx -side left -padx 4
    grid $w.l -row 0 -column 0 -columnspan 3 -padx 10 -pady {10 4} -sticky w
    grid $w.n -row 1 -column 0 -padx 10 -sticky w
    grid $w.h -row 1 -column 1 -sticky w
    grid $w.est -row 2 -column 0 -columnspan 3 -padx 10 -pady 6 -sticky w
    grid $w.b -row 3 -column 0 -columnspan 3 -pady 8
    bind $w <Escape> [list destroy $w]
    trace add variable ::LIMIT write [list sync_estimate $w]
    sync_estimate $w
}

proc sync_estimate {w args} {
    if {![winfo exists $w]} return
    set a [list sync $::SERIES --estimate]
    if {$::LIMIT > 0} { lappend a --limit $::LIMIT }
    set d [run_json {*}$a]
    if {$d eq ""} { $w.est configure -text "estimate unavailable" ; return }
    $w.est configure -text "[json::get $d chapters] chapter(s), ~[json::get $d human] of CPU"
}

proc do_sync {w} {
    set lim $::LIMIT ; destroy $w
    set a [list sync $::SERIES --yes]
    if {$lim > 0} { lappend a --limit $lim }
    run_cmd $a
}

proc dlg_add {} {
    set w .add ; catch {destroy $w}
    toplevel $w ; wm title $w "Add series" ; wm transient $w .
    ttk::label $w.l -text "Fiction URL:"
    ttk::entry $w.u -width 62
    ttk::label $w.l2 -text "Already read through:"
    ttk::combobox $w.f -width 12 -values {start latest 10 25 50}
    $w.f set start
    ttk::frame $w.b
    ttk::button $w.b.ok -text Add -command [list do_add $w]
    ttk::button $w.b.cx -text Cancel -command [list destroy $w]
    pack $w.b.ok $w.b.cx -side left -padx 4
    grid $w.l $w.u -padx 8 -pady {10 4} -sticky w
    grid $w.l2 $w.f -padx 8 -sticky w
    grid $w.b -columnspan 2 -pady 8
    focus $w.u ; bind $w <Return> [list do_add $w] ; bind $w <Escape> [list destroy $w]
}
proc do_add {w} {
    set url [string trim [$w.u get]] ; set from [$w.f get]
    if {$url eq ""} return
    destroy $w
    set d [run_json series add $url --from $from]
    if {$d ne ""} { log "added [json::get $d title] — [json::get $d chapters] chapters" }
    refresh_series
}

# ---------------------------------------------------------------- editors ----
;# The cast/lexicon files are small and hand-owned, so edit them in-pane. But
;# `cast update` appends to the cast file behind our back — so track mtime and
;# refuse to clobber a file that changed on disk since we loaded it.
proc editor_path {which} {
    if {$::SERIES eq ""} { return "" }
    if {$which eq "cast"} { return [file join $::SERIESDIR $::SERIES.toml] }
    return [file join $::LEXDIR $::SERIES.csv]
}

proc load_editor {which {force 0}} {
    set t .br.$which.t
    set path [editor_path $which]
    if {$path eq ""} { $t delete 1.0 end ; return }
    if {!$force && [info exists ::EDIT($which,dirty)] && $::EDIT($which,dirty)} {
        if {![confirm_discard $which]} return
    }
    set text ""
    if {[file exists $path]} {
        set fh [open $path r] ; set text [read $fh] ; close $fh
        set ::EDIT($which,mtime) [file mtime $path]
    } else {
        set ::EDIT($which,mtime) 0
    }
    set ::EDIT($which,path) $path
    set ::EDIT($which,text) [string trimright $text "\n"]   ;# the dirty baseline
    $t delete 1.0 end
    $t insert 1.0 $text
    $t edit modified 0
    $t edit reset
    set ::EDIT($which,dirty) 0
    editor_title $which
}

;# Offer to keep unsaved work before a load/series-switch throws it away.
proc confirm_discard {which} {
    set a [tk_messageBox -type yesnocancel -icon question -title "Unsaved changes" \
        -message "Save changes to $::EDIT($which,path)?" \
        -detail "No discards them; Cancel keeps the pane as it is."]
    if {$a eq "cancel"} { return 0 }
    if {$a eq "yes"} { save_editor $which }
    return 1
}

proc editor_title {which} {
    set names {cast Cast lex Lexicon}
    set base [dict get $names $which]
    set mark [expr {[info exists ::EDIT($which,dirty)] && $::EDIT($which,dirty) ? " *" : ""}]
    .br tab .br.$which -text "$base$mark"
}

proc editor_modified {which} {
    ;# <<Modified>> is delivered asynchronously, so a "we're loading" flag has
    ;# already cleared by the time it arrives. Compare against the text we
    ;# loaded instead — timing-independent, and undoing back to the original
    ;# correctly reads as clean. These files are a few KB; the compare is free.
    .br.$which.t edit modified 0
    if {![info exists ::EDIT($which,text)]} return
    set cur [string trimright [.br.$which.t get 1.0 end] "\n"]
    set ::EDIT($which,dirty) [expr {$cur ne $::EDIT($which,text)}]
    editor_title $which
}

proc save_editor {which} {
    set path $::EDIT($which,path)
    if {$path eq ""} return
    if {[file exists $path] && [file mtime $path] != $::EDIT($which,mtime)} {
        set a [tk_messageBox -type yesnocancel -icon warning -title "Changed on disk" \
            -message "$path changed on disk since you opened it." \
            -detail "Yes = overwrite with what's in the pane.\nNo = discard your edits and reload." ]
        if {$a eq "cancel"} return
        if {$a eq "no"} { load_editor $which 1 ; return }
    }
    file mkdir [file dirname $path]
    set fh [open $path w]
    puts -nonewline $fh [string trimright [.br.$which.t get 1.0 end] "\n"]
    puts $fh ""
    close $fh
    set ::EDIT($which,mtime) [file mtime $path]
    set ::EDIT($which,text) [string trimright [.br.$which.t get 1.0 end] "\n"]
    set ::EDIT($which,dirty) 0
    editor_title $which
    log "saved $path"
}

;# Hand a rendered chapter to an audio player. $WEBNOVEL_AUDIO_PLAYER wins
;# (e.g. "mpv --no-video"), else xdg-open picks by MIME.
proc play_selected {} {
    if {$::SERIES eq ""} return
    set sel [.bl.tv selection]
    if {![llength $sel]} { log "select a chapter to play" ; return }
    set n [.bl.tv set [lindex $sel 0] n]
    set d [run_json state show $::SERIES $n]
    if {$d eq ""} return
    set c [lindex [dict get $d chapters] 0]
    set path [json::get $c audio_path]
    if {$path eq "" || ![file exists $path]} {
        log "#$n has no rendered audio yet"
        return
    }
    set cmd ""
    if {[info exists ::env(WEBNOVEL_AUDIO_PLAYER)]} {
        set cmd [string trim $::env(WEBNOVEL_AUDIO_PLAYER)]
    }
    if {$cmd eq ""} { set cmd "xdg-open" }
    log "playing #$n — [file tail $path]"
    if {[catch {exec {*}$cmd $path &} e]} { log "! $cmd: $e" }
}

proc external_editor {which} {
    set path $::EDIT($which,path)
    if {$path eq ""} return
    foreach v {WEBNOVEL_AUDIO_EDITOR VISUAL EDITOR} {
        if {[info exists ::env($v)] && [string trim $::env($v)] ne ""} {
            if {[catch {exec {*}[string trim $::env($v)] $path &} e]} { log "! $e" }
            return
        }
    }
    if {[catch {exec xdg-open $path &} e]} { log "! $e" }
}

# ---------------------------------------------------------------- server ------
proc server_toggle {} {
    if {$::SERVEPID ne ""} { server_stop } else { server_start }
}
proc server_start {} {
    if {$::SERVEPID ne ""} return
    if {[catch {open "| [list $::EXE serve 2>@1]" r} ::SERVEPIPE]} {
        log "! server: $::SERVEPIPE" ; set ::SERVEPIPE "" ; return
    }
    set ::SERVEPID [pid $::SERVEPIPE]
    fconfigure $::SERVEPIPE -blocking 0 -buffering line
    fileevent $::SERVEPIPE readable server_readable
    .tool.srv configure -text "Stop server"
    log "feed server started (pid $::SERVEPID)"
}
proc server_readable {} {
    if {[gets $::SERVEPIPE line] < 0} {
        if {[eof $::SERVEPIPE]} { server_gone }
        return
    }
    if {[string trim $line] ne ""} { log "\[serve\] $line" }
}
proc server_gone {} {
    catch {fileevent $::SERVEPIPE readable {}}
    catch {close $::SERVEPIPE}
    set ::SERVEPIPE "" ; set ::SERVEPID ""
    .tool.srv configure -text "Start server"
    log "feed server stopped"
}
proc server_stop {} {
    if {$::SERVEPID eq ""} return
    catch {exec kill -INT {*}$::SERVEPID}
    after 1000 {if {$::SERVEPID ne ""} {catch {exec kill {*}$::SERVEPID} ; server_gone}}
}

# ---------------------------------------------------------------- persistence -
proc save_conf {} {
    file mkdir $::CFGDIR
    if {[catch {open $::UICONF w} fh]} return
    catch {set ::geom(main) [wm geometry .]}
    catch {set ::geom(topheight) [.ctop sashpos 0]}
    catch {set ::geom(botwidth) [.bot sashpos 0]}
    foreach k {main topheight botwidth} { puts $fh "$k $::geom($k)" }
    puts $fh "limit $::LIMIT"
    close $fh
}
proc load_conf {} {
    if {[catch {open $::UICONF r} fh]} return
    while {[gets $fh line] >= 0} {
        set line [string trim $line]
        if {$line eq "" || [string index $line 0] eq "#"} continue
        set k [lindex $line 0] ; set v [lrange $line 1 end]
        if {$k in {main topheight botwidth}} { set ::geom($k) $v }
        if {$k eq "limit"} { set ::LIMIT $v }
    }
    close $fh
}

proc quit {} {
    foreach w {cast lex} {
        if {[info exists ::EDIT($w,dirty)] && $::EDIT($w,dirty)} {
            set a [tk_messageBox -type yesnocancel -icon question \
                -message "Save changes to $::EDIT($w,path)?"]
            if {$a eq "cancel"} return
            if {$a eq "yes"} { save_editor $w }
        }
    }
    server_stop ; save_conf ; exit
}

# ================================================================ build UI ====
load_conf
wm title . "webnovel-audio"
wm minsize . 900 560
wm protocol . WM_DELETE_WINDOW quit

# -- toolbar
ttk::frame .tool -padding {6 5}
ttk::button .tool.add     -text "Add series…" -command dlg_add
ttk::button .tool.refresh -text "Refresh"     -command {refresh_series ; refresh_chapters}
ttk::separator .tool.s1 -orient vertical
ttk::button .tool.sync    -text "Sync…"       -command dlg_sync
ttk::button .tool.stop    -text "Stop"        -command stop_cmd
ttk::separator .tool.s2 -orient vertical
ttk::button .tool.srv     -text "Start server" -command server_toggle
pack .tool.add .tool.refresh -side left -padx 2
pack .tool.s1 -side left -fill y -padx 6
pack .tool.sync .tool.stop -side left -padx 2
pack .tool.s2 -side left -fill y -padx 6
pack .tool.srv -side left -padx 2
grid .tool -row 0 -column 0 -sticky ew

# -- vertical split: series on top, everything else below
ttk::panedwindow .ctop -orient vertical
grid .ctop -row 1 -column 0 -sticky nsew
grid rowconfigure . 1 -weight 1
grid columnconfigure . 0 -weight 1

ttk::frame .top
ttk::treeview .top.tv -columns {title status rendered pending err next} \
    -show headings -selectmode browse -yscrollcommand {.top.sb set}
foreach {c t w a} {title Title 300 w  status Status 90 center  rendered Rendered 80 center
                   pending Pending 80 center  err Err 50 center  next "Next up" 280 w} {
    .top.tv heading $c -text $t
    .top.tv column $c -width $w -anchor $a
}
ttk::scrollbar .top.sb -orient vertical -command {.top.tv yview}
grid .top.tv .top.sb -sticky nsew
grid rowconfigure .top 0 -weight 1
grid columnconfigure .top 0 -weight 1
bind .top.tv <<TreeviewSelect>> on_series_select

# -- horizontal split: chapters | notebook
ttk::panedwindow .bot -orient horizontal
.ctop add .top
.ctop add .bot

ttk::frame .bl
ttk::treeview .bl.tv -columns {n title status dur note} -show headings \
    -selectmode extended -yscrollcommand {.bl.sb set}
foreach {c t w a} {n "#" 55 center  title Title 330 w  status Stage 85 center
                   dur Audio 65 center  note "" 200 w} {
    .bl.tv heading $c -text $t
    .bl.tv column $c -width $w -anchor $a
}
ttk::scrollbar .bl.sb -orient vertical -command {.bl.tv yview}
grid .bl.tv .bl.sb -sticky nsew
grid rowconfigure .bl 0 -weight 1
grid columnconfigure .bl 0 -weight 1
bind .bl.tv $::CTXBUT {chapter_ctx %X %Y %x %y}
bind .bl.tv <Double-1> {play_selected ; break}

menu .ctx -tearoff 0
.ctx add command -label "Play"   -command play_selected
.ctx add separator
.ctx add command -label "Fetch"  -command {do_stage fetch}
.ctx add command -label "Parse"  -command {do_stage parse}
.ctx add command -label "Check"  -command {do_stage check}
.ctx add command -label "Render" -command {do_stage render}
.ctx add separator
.ctx add command -label "Update cast from selection" -command {do_stage cast}
.ctx add separator
.ctx add command -label "Mark skipped" -command {do_state skipped}
.ctx add command -label "Mark new (re-do)" -command {do_state new}
.ctx add command -label "Reset errors" -command do_reset_errors

# -- notebook: Cast | Lexicon | Log
ttk::notebook .br
foreach {w label} {cast Cast lex Lexicon} {
    ttk::frame .br.$w
    text .br.$w.t -wrap none -undo 1 -font TkFixedFont \
        -yscrollcommand ".br.$w.sb set" -xscrollcommand ".br.$w.hb set"
    ttk::scrollbar .br.$w.sb -orient vertical   -command ".br.$w.t yview"
    ttk::scrollbar .br.$w.hb -orient horizontal -command ".br.$w.t xview"
    ttk::frame .br.$w.b
    ttk::button .br.$w.b.save -text Save    -command [list save_editor $w]
    ttk::button .br.$w.b.rev  -text Reload  -command [list load_editor $w 1]
    ttk::button .br.$w.b.ext  -text "Open in \$EDITOR" -command [list external_editor $w]
    pack .br.$w.b.save .br.$w.b.rev .br.$w.b.ext -side left -padx 3
    grid .br.$w.t  .br.$w.sb -sticky nsew
    grid .br.$w.hb -sticky ew
    grid .br.$w.b  -columnspan 2 -sticky w -pady 4
    grid rowconfigure .br.$w 0 -weight 1
    grid columnconfigure .br.$w 0 -weight 1
    .br add .br.$w -text $label
    bind .br.$w.t <<Modified>> [list editor_modified $w]
}
ttk::frame .br.log
text .br.log.t -wrap word -state disabled -font TkFixedFont \
    -yscrollcommand {.br.log.sb set}
ttk::scrollbar .br.log.sb -orient vertical -command {.br.log.t yview}
grid .br.log.t .br.log.sb -sticky nsew
grid rowconfigure .br.log 0 -weight 1
grid columnconfigure .br.log 0 -weight 1
.br add .br.log -text Log

.bot add .bl
.bot add .br

# -- status bar
ttk::label .status -textvariable ::STATUS -relief sunken -anchor w -padding {6 3}
grid .status -row 2 -column 0 -sticky ew

# sashpos before the window is mapped silently does nothing (gitk:2748) — set it
# from a one-shot Map binding that removes itself.
bind .ctop <Map> { bind %W <Map> {} ; after idle [list %W sashpos 0 $::geom(topheight)] }
bind .bot  <Map> { bind %W <Map> {} ; after idle [list %W sashpos 0 $::geom(botwidth)] }
catch {wm geometry . $::geom(main)}

# ---------------------------------------------------------------- start ------
ui_busy 0
log "webnovel-audio control — $::EXE"
if {[set c [run_json config]] ne ""} {
    set ::CFGPATH   [json::get $c config_path]
    set ::LEXDIR    [json::get $c lexicon_dir]
    set ::SERIESDIR [json::get $c series_config_dir]
    set ::BASELEX   [json::get $c base_lexicon]
    log "library: [json::get $c library_dir]"
}
refresh_series
