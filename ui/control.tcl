#!/usr/bin/env wish
# webnovel-audio — control / status UI.
#
# A thin Tcl/Tk front end over the `webnovel-audio` CLI (`--json`). It adds and
# tracks series, runs `sync` on demand or on a schedule, and streams the output.
# No DB or network access of its own — everything goes through the CLI.
#
#   wish ui/control.tcl              # from the project root
#   WEBNOVEL_AUDIO=/path/to/exe wish ui/control.tcl

package require Tk
source [file join [file dirname [info script]] json.tcl]

set ::CFGDIR [file join [file normalize ~] .config webnovel-audio]
set ::UICONF [file join $::CFGDIR ui.conf]

# filled in at startup from `webnovel-audio config --json`
set ::CFGPATH "" ; set ::LEXDIR "data/lexicons" ; set ::SERIESDIR "data/series"
set ::BASELEX "data/lexicons/_base.csv" ; set ::VOICES {(default)}

# ---------------------------------------------------------------- exe + paths --
proc find_exe {} {
    if {[info exists ::env(WEBNOVEL_AUDIO)] && [file executable $::env(WEBNOVEL_AUDIO)]} {
        return [file normalize $::env(WEBNOVEL_AUDIO)]
    }
    set here [file dirname [file normalize [info script]]]
    foreach c [list \
        [file join $here .. .venv bin webnovel-audio] \
        [lindex [auto_execok webnovel-audio] 0]] {
        if {$c ne "" && [file executable $c]} { return [file normalize $c] }
    }
    return webnovel-audio
}
set ::EXE [find_exe]

# run the CLI and parse one JSON object of output. Returns "" on failure.
proc run_json {args} {
    if {[catch {exec -- $::EXE {*}$args --json 2>@1} out]} {
        log "! [lindex [split $out \n] 0]"
        return ""
    }
    if {[catch {json::parse [string trim $out]} d]} {
        log "! bad JSON from: $args"
        return ""
    }
    return $d
}

# ---------------------------------------------------------------- log pane -----
proc log {msg} {
    .body.log configure -state normal
    .body.log insert end "$msg\n"
    .body.log see end
    .body.log configure -state disabled
}

# ---------------------------------------------------------------- series list --
proc refresh_series {} {
    .tools.status configure -text "loading…"
    set d [run_json series list]
    .series delete [.series children {}]
    if {$d eq ""} { .tools.status configure -text "error" ; return }
    set rows [dict get $d series]
    foreach r $rows {
        set nxt [json::get $r next title]
        .series insert {} end -id [dict get $r slug] -values [list \
            [dict get $r title] \
            "#[dict get $r progress]" \
            [dict get $r pending] \
            [dict get $r errors] \
            [json::get $r provider] \
            $nxt]
    }
    .tools.status configure -text "[llength $rows] series · exe $::EXE"
}

proc selected_slug {} {
    set s [.series selection]
    return [expr {[llength $s] ? [lindex $s 0] : ""}]
}

# ---------------------------------------------------------------- add series ---
proc dlg_add {} {
    set w .add
    catch {destroy $w}
    toplevel $w
    wm title $w "Add series"
    wm transient $w .
    grid [ttk::label $w.l1 -text "Fiction URL:"] -row 0 -column 0 -sticky e -padx 6 -pady 6
    grid [ttk::entry $w.url -width 60] -row 0 -column 1 -padx 6 -pady 6
    grid [ttk::label $w.l2 -text "Start from:"] -row 1 -column 0 -sticky e -padx 6
    grid [ttk::combobox $w.from -width 12 -values {latest start 1 10 50} ] -row 1 -column 1 -sticky w -padx 6
    $w.from set latest
    grid [ttk::frame $w.b] -row 2 -column 0 -columnspan 2 -pady 8
    ttk::button $w.b.ok -text Add -command [list do_add $w]
    ttk::button $w.b.cancel -text Cancel -command [list destroy $w]
    pack $w.b.ok $w.b.cancel -side left -padx 4
    focus $w.url
    bind $w <Return> [list do_add $w]
    bind $w <Escape> [list destroy $w]
}
proc do_add {w} {
    set url [string trim [$w.url get]]
    set from [string trim [$w.from get]]
    if {$url eq ""} return
    destroy $w
    log "\$ series add $url --from $from"
    set d [run_json series add $url --from $from]
    if {$d ne "" && [json::get $d ok] ne "0"} {
        log "added \"[json::get $d title]\" — [json::get $d chapters] chapters, [json::get $d pending] pending"
    }
    refresh_series
}

# ---------------------------------------------------------------- set progress -
proc dlg_setprog {} {
    set slug [selected_slug]
    if {$slug eq ""} { log "select a series first" ; return }
    set w .setp
    catch {destroy $w}
    toplevel $w ; wm title $w "Set progress: $slug" ; wm transient $w .
    grid [ttk::label $w.l -text "Heard/read through:"] -row 0 -column 0 -padx 6 -pady 6 -sticky e
    grid [ttk::combobox $w.v -width 12 -values {latest start 1 5 10 25 50 100}] -row 0 -column 1 -padx 6 -pady 6 -sticky w
    $w.v set latest
    grid [ttk::frame $w.b] -row 1 -column 0 -columnspan 2 -pady 6
    ttk::button $w.b.ok -text Set -command "do_setprog $w [list $slug]"
    ttk::button $w.b.cx -text Cancel -command [list destroy $w]
    pack $w.b.ok $w.b.cx -side left -padx 4
    focus $w.v ; bind $w <Escape> [list destroy $w]
}
proc do_setprog {w slug} {
    set v [string trim [$w.v get]] ; destroy $w
    set d [run_json series set $slug $v]
    if {$d ne ""} { log "$slug: progress -> #[json::get $d progress] ([json::get $d pending] pending)" }
    refresh_series
}

# ---------------------------------------------------------------- sync (stream) -
set ::RUNNING 0
set ::PIPE ""

proc sync_start {args} {
    if {$::RUNNING} { log "a sync is already running" ; return }
    set ::RUNNING 1
    ui_busy 1
    log "\$ sync $args"
    set pipe "| [list $::EXE sync {*}$args --json 2>@1]"
    if {[catch {open $pipe r} ::PIPE]} {
        log "! cannot launch: $::PIPE"
        set ::RUNNING 0 ; ui_busy 0 ; return
    }
    fconfigure $::PIPE -blocking 0 -buffering line
    fileevent $::PIPE readable sync_readable
}

proc sync_readable {} {
    if {[gets $::PIPE line] < 0} {
        if {[eof $::PIPE]} { sync_finish }
        return
    }
    set line [string trim $line]
    if {$line eq ""} return
    if {[catch {json::parse $line} ev]} { log $line ; return }
    set slug [json::get $ev slug]
    switch -- [json::get $ev event] {
        start        { log "start: [json::get $ev series] series[expr {[json::get $ev dry_run] eq {1} ? { (dry run)} : {}}]" }
        series {
            log "  [json::get $ev title]: [json::get $ev pending] pending"
            if {$slug ne "" && [.series exists $slug]} {
                .series set $slug pend [json::get $ev pending]
            }
        }
        chapter_begin {
            log "    #[json::get $ev number] [json::get $ev title] …"
            .series set $slug next "rendering #[json::get $ev number]…"
        }
        chapter {
            set r [json::get $ev result] ; set num [json::get $ev number]
            if {$r eq "rendered"} {
                log "    #$num ok  ([json::get $ev audio_seconds]s audio, [json::get $ev elapsed_seconds]s)"
                row_progress $slug $num
            } elseif {$r eq "error"} {
                log "    #$num ERROR: [json::get $ev error]"
                if {$slug ne "" && [.series exists $slug]} {
                    .series set $slug err [expr {[.series set $slug err] + 1}]
                }
            } else {
                log "    #$num [json::get $ev title]  ->  [json::get $ev path]"
            }
        }
        done { log "done: [json::get $ev rendered] rendered, [json::get $ev errors] error(s), [json::get $ev skipped] pending" }
        default { log $line }
    }
}

# advance a series row as each chapter lands, without a round-trip to the CLI
proc row_progress {slug num} {
    if {$slug eq "" || ![.series exists $slug]} return
    .series set $slug prog "#$num"
    set p [.series set $slug pend]
    if {[string is integer -strict $p] && $p > 0} { .series set $slug pend [expr {$p - 1}] }
    .series set $slug next ""
}

proc sync_finish {} {
    catch {fileevent $::PIPE readable {}}
    set ok [expr {![catch {close $::PIPE} err]}]
    if {!$ok} { log "— sync exited: [lindex [split $err \n] 0] —" } else { log "— sync finished —" }
    set ::RUNNING 0
    ui_busy 0
    refresh_series
    schedule_next
}

proc sync_stop {} {
    if {!$::RUNNING} return
    log "stopping sync…"
    catch {close $::PIPE}
    set ::RUNNING 0 ; ui_busy 0
}

proc do_sync {which} {
    set a {}
    if {$which eq "selected"} {
        set slug [selected_slug]
        if {$slug eq ""} { log "select a series first" ; return }
        lappend a $slug
    }
    if {[.tools.dry instate selected]} { lappend a --dry-run }
    set lim [string trim [.tools.limit get]]
    if {[string is integer -strict $lim] && $lim > 0} { lappend a --limit $lim }
    sync_start {*}$a
}

proc ui_busy {on} {
    set st [expr {$on ? "disabled" : "!disabled"}]
    foreach b {.tools.syncall .tools.syncsel .tools.refresh .tools.add .tools.setp} {
        $b state $st
    }
    .tools.stop state [expr {$on ? "!disabled" : "disabled"}]
}

# ---------------------------------------------------------------- feed server --
set ::SERVEPIPE "" ; set ::SERVEPID ""

proc server_start {} {
    if {$::SERVEPID ne ""} return
    if {[catch {open "| [list $::EXE serve 2>@1]" r} ::SERVEPIPE]} {
        log "! server: $::SERVEPIPE" ; set ::SERVEPIPE "" ; return
    }
    set ::SERVEPID [pid $::SERVEPIPE]
    fconfigure $::SERVEPIPE -blocking 0 -buffering line
    fileevent $::SERVEPIPE readable server_readable
    .srv.start state disabled
    .srv.stop  state !disabled
    .srv.status configure -text "running (pid $::SERVEPID)" -foreground "#2a9d5c"
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
    .srv.start state !disabled
    .srv.stop  state disabled
    .srv.status configure -text "off" -foreground "#a33"
    log "feed server stopped"
}

proc server_stop {} {
    if {$::SERVEPID eq ""} return
    catch {exec kill -INT {*}$::SERVEPID}
    after 1000 {if {$::SERVEPID ne ""} {catch {exec kill {*}$::SERVEPID} ; server_gone}}
}

# ---------------------------------------------------------------- file edits ---
# WEBNOVEL_AUDIO_EDITOR (a command, may include args) overrides xdg-open — use it
# when xdg-open guesses wrong (a header-only .csv sniffs as text/plain, a filled
# one as text/csv, so the two can open in different apps).
proc open_file {path {stub ""}} {
    file mkdir [file dirname $path]
    if {![file exists $path] && $stub ne ""} {
        set fh [open $path w] ; puts -nonewline $fh $stub ; close $fh
    }
    set ed ""
    if {[info exists ::env(WEBNOVEL_AUDIO_EDITOR)]} { set ed [string trim $::env(WEBNOVEL_AUDIO_EDITOR)] }
    if {$ed ne ""} {
        if {[catch {exec -- {*}$ed $path &} err]} { log "! $ed: $err" } \
        else { log "opened $path  ($ed)" }
        return
    }
    if {[catch {exec xdg-open $path &} err]} { log "! xdg-open: $err" } \
    else { log "opened $path" }
}

proc edit_base_lexicon {} {
    open_file $::BASELEX "surface,respell,ipa,notes\n"
}

# ---------------------------------------------------------- pronunciation test -
# every slug we could apply a lexicon for: tracked series + data/lexicons/*.csv
proc lexicon_slugs {} {
    set s {}
    foreach id [.series children {}] { lappend s $id }
    foreach f [glob -nocomplain -directory $::LEXDIR *.csv] {
        set stem [file rootname [file tail $f]]
        if {$stem ne "_base"} { lappend s $stem }
    }
    return [lsort -unique $s]
}
proc dlg_pron {} {
    set w .pron ; catch {destroy $w}
    toplevel $w ; wm title $w "Pronunciation check" ; wm transient $w .
    ttk::label  $w.l  -text "Word or phrase:"
    ttk::entry  $w.e  -width 44
    ttk::button $w.go -text "Check" -command [list do_pron $w]
    ttk::label  $w.sl -text "Series lexicon:"
    ttk::combobox $w.sr -width 24 -values [linsert [lexicon_slugs] 0 "(none — base only)"]
    $w.sr set [expr {[selected_slug] ne "" ? [selected_slug] : "(none — base only)"}]
    ttk::button $w.ck -text "Audit lexicon" -command [list do_pron $w --check]
    text $w.out -width 70 -height 14 -wrap none -state disabled \
        -font TkFixedFont -yscrollcommand [list $w.sb set]
    ttk::scrollbar $w.sb -orient vertical -command [list $w.out yview]
    grid $w.l $w.e $w.go -padx 6 -pady {8 4} -sticky w
    grid $w.sl $w.sr $w.ck -row 1 -padx 6 -pady {0 4} -sticky w
    grid $w.out $w.sb -row 2 -column 0 -columnspan 3 -padx 6 -pady 6 -sticky nsew
    grid configure $w.sb -column 3 -sticky ns
    grid rowconfigure $w 2 -weight 1 ; grid columnconfigure $w 1 -weight 1
    focus $w.e ; bind $w <Return> [list do_pron $w] ; bind $w <Escape> [list destroy $w]
}
proc do_pron {w {mode ""}} {
    set slug [$w.sr get]
    if {[string match "(none*" $slug]} { set slug "" }
    set t [string trim [$w.e get]]
    if {$mode eq "--check"} {
        set a [list pron --check]
    } else {
        if {$t eq ""} return
        set a [list pron $t]
    }
    if {$slug ne ""} { lappend a --series $slug }
    if {[catch {exec -- $::EXE {*}$a 2>@1} out]} { set out "error: $out" }
    $w.out configure -state normal
    $w.out delete 1.0 end ; $w.out insert end $out
    $w.out configure -state disabled
}

proc edit_config {} {
    if {![file exists $::CFGPATH]} {
        set ex [file join [file dirname $::CFGPATH] config.example.toml]
        if {[file exists $ex]} {
            file copy $ex $::CFGPATH ; log "created $::CFGPATH from config.example.toml"
        }
    }
    open_file $::CFGPATH
}

proc edit_lexicon {} {
    set slug [selected_slug]
    if {$slug eq ""} { log "select a series first" ; return }
    open_file [file join $::LEXDIR "$slug.csv"] "surface,respell,ipa,notes\n"
}

proc edit_overlay {} {
    set slug [selected_slug]
    if {$slug eq ""} { log "select a series first" ; return }
    open_file [file join $::SERIESDIR "$slug.toml"] \
        "# per-series overrides for $slug (merged over config.toml by sync)\n\n\[cast\]\n"
}

# read/set one "key = \"value\"" line inside a TOML section, preserving the rest
proc toml_get {path section key} {
    if {![file exists $path]} { return "" }
    set fh [open $path r] ; set lines [split [read $fh] "\n"] ; close $fh
    set insec 0
    foreach ln $lines {
        if {[regexp {^\s*\[} $ln]} { set insec [regexp "^\\s*\\\[$section\\\]" $ln] ; continue }
        if {$insec && [regexp "^\\s*$key\\s*=\\s*\"?(\[^\"\]*)\"?" $ln -> v]} { return [string trim $v] }
    }
    return ""
}
proc toml_upsert {path section key value} {
    set lines {}
    if {[file exists $path]} {
        set fh [open $path r] ; set lines [split [string trimright [read $fh] "\n"] "\n"] ; close $fh
    }
    set out {} ; set insec 0 ; set done 0
    foreach ln $lines {
        if {[regexp {^\s*\[} $ln]} {
            if {$insec && !$done} { lappend out "$key = \"$value\"" ; set done 1 }
            set insec [regexp "^\\s*\\\[$section\\\]" $ln]
            lappend out $ln ; continue
        }
        if {$insec && !$done && [regexp "^\\s*$key\\s*=" $ln]} {
            lappend out "$key = \"$value\"" ; set done 1 ; continue
        }
        lappend out $ln
    }
    if {$insec && !$done} { lappend out "$key = \"$value\"" ; set done 1 }
    if {!$done} {
        if {[llength $out] && [lindex $out end] ne ""} { lappend out "" }
        lappend out "\[$section\]" "$key = \"$value\"" ; set done 1
    }
    file mkdir [file dirname $path]
    set fh [open $path w] ; puts $fh [join $out "\n"] ; close $fh
}

proc set_narrator {} {
    set slug [selected_slug]
    if {$slug eq ""} return
    set v [.sel.voice get]
    if {$v eq "" || $v eq "(default)"} return
    set f [file join $::SERIESDIR "$slug.toml"]
    toml_upsert $f cast narrator $v
    log "$slug narrator -> $v   (Re-render to apply)"
}

proc dlg_redo {} {
    set slug [selected_slug]
    if {$slug eq ""} { log "select a series first" ; return }
    set w .redo ; catch {destroy $w}
    toplevel $w ; wm title $w "Re-render: $slug" ; wm transient $w .
    grid [ttk::label $w.l -text "Chapters (blank = all rendered; N or N-M):"] \
        -row 0 -column 0 -columnspan 2 -padx 8 -pady {8 4} -sticky w
    grid [ttk::entry $w.r -width 16] -row 1 -column 0 -padx 8 -sticky w
    grid [ttk::checkbutton $w.s -text "sync now"] -row 1 -column 1 -padx 8
    $w.s state selected
    grid [ttk::frame $w.b] -row 2 -column 0 -columnspan 2 -pady 8
    ttk::button $w.b.ok -text "Re-queue" -command [list do_redo $w [list $slug]]
    ttk::button $w.b.cx -text Cancel -command [list destroy $w]
    pack $w.b.ok $w.b.cx -side left -padx 4
    focus $w.r ; bind $w <Escape> [list destroy $w] ; bind $w <Return> [list do_redo $w [list $slug]]
}
proc do_redo {w slug} {
    set rng [string trim [$w.r get]]
    set sync [$w.s instate selected]
    destroy $w
    set d [run_json series redo $slug $rng]
    if {$d ne ""} { log "$slug: re-queued [json::get $d requeued] ([json::get $d pending] pending)" }
    refresh_series
    if {$sync} { .series selection set $slug ; do_sync selected }
}

proc on_select {} {
    set slug [selected_slug]
    set on [expr {$slug ne "" ? "!disabled" : "disabled"}]
    foreach b {.sel.lex .sel.ovl .sel.redo .sel.voice} { $b state $on }
    if {$slug eq ""} { .sel.title configure -text "—" ; return }
    .sel.title configure -text [.series set $slug title]
    set n [toml_get [file join $::SERIESDIR "$slug.toml"] cast narrator]
    .sel.voice set [expr {$n ne "" ? $n : "(default)"}]
}

# ---------------------------------------------------------------- scheduler ----
set ::AUTO 0 ; set ::MODE interval ; set ::IVAL 180 ; set ::DAILY 03:00
set ::AFTERID "" ; set ::NEXTRUN "off"

proc schedule_next {} {
    catch {after cancel $::AFTERID} ; set ::AFTERID ""
    if {!$::AUTO} { set ::NEXTRUN "off" ; save_conf ; return }
    if {$::MODE eq "interval"} {
        set mins $::IVAL
        if {![string is double -strict $mins] || $mins < 1} { set mins 1 }
        set ms [expr {int($mins * 60000)}]
    } else {
        if {![regexp {^(\d{1,2}):(\d{2})$} $::DAILY -> h m]} { set ::NEXTRUN "bad time" ; return }
        set now [clock seconds]
        set t [clock scan [format %02d:%02d $h $m] -base $now -format %H:%M]
        if {$t <= $now} { incr t 86400 }
        set ms [expr {($t - $now) * 1000}]
    }
    set ::NEXTRUN [clock format [expr {[clock seconds] + $ms / 1000}] -format "%a %H:%M"]
    set ::AFTERID [after $ms auto_fire]
    save_conf
}

proc auto_fire {} {
    if {$::RUNNING} {
        log "auto-sync: a sync is already running, skipping this tick"
        schedule_next
        return
    }
    log "auto-sync firing"
    sync_start
    # schedule_next is called from sync_finish
}

# ---------------------------------------------------------------- persistence --
proc save_conf {} {
    file mkdir $::CFGDIR
    if {[catch {open $::UICONF w} fh]} return
    foreach k {AUTO MODE IVAL DAILY} { puts $fh "$k [set ::$k]" }
    close $fh
}
proc load_conf {} {
    if {[catch {open $::UICONF r} fh]} return
    while {[gets $fh line] >= 0} {
        set line [string trim $line]
        if {$line eq "" || [string index $line 0] eq "#"} continue
        set k [lindex $line 0] ; set v [lrange $line 1 end]
        if {$k in {AUTO MODE IVAL DAILY}} { set ::$k $v }
    }
    close $fh
}

# ---------------------------------------------------------------- build UI -----
wm title . "webnovel-audio"
wm minsize . 720 480

ttk::frame .tools -padding 6
ttk::button .tools.add     -text "Add series…"   -command dlg_add
ttk::button .tools.setp    -text "Set progress…" -command dlg_setprog
ttk::button .tools.refresh -text "Refresh"       -command refresh_series
ttk::button .tools.editcfg -text "Edit config…"  -command edit_config
ttk::button .tools.editbase -text "Base lexicon…" -command edit_base_lexicon
ttk::button .tools.pron    -text "Test word…"     -command dlg_pron
ttk::separator .tools.s1 -orient vertical
ttk::button .tools.syncall -text "Sync all"      -command {do_sync all}
ttk::button .tools.syncsel -text "Sync selected" -command {do_sync selected}
ttk::button .tools.stop    -text "Stop"          -command sync_stop
ttk::label  .tools.ll -text "limit"
ttk::spinbox .tools.limit -from 0 -to 999 -width 4
.tools.limit set 0
ttk::checkbutton .tools.dry -text "dry run"
ttk::label .tools.status -text ""
grid .tools.add .tools.setp .tools.refresh .tools.editcfg .tools.editbase .tools.pron .tools.s1 \
     .tools.syncall .tools.syncsel .tools.stop .tools.ll .tools.limit .tools.dry \
     -row 0 -padx 3 -pady 2 -sticky w
grid .tools.status -row 1 -column 0 -columnspan 12 -sticky w -pady {6 0}
grid .tools.s1 -sticky ns -padx 8
grid .tools.ll -padx {12 3}
grid columnconfigure .tools 20 -weight 1
grid .tools -row 0 -sticky ew

ttk::panedwindow .body -orient vertical
frame .body.sf
ttk::treeview .series -columns {title prog pend err prov next} -show headings \
     -selectmode browse -yscrollcommand {.body.sf.sb set}
foreach {c t w} {title Title 260 prog Progress 80 pend Pending 70 err Err 50 prov Provider 90 next "Next up" 220} {
    .series heading $c -text $t
    .series column $c -width $w -anchor [expr {$c in {prog pend err} ? "center" : "w"}]
}
ttk::scrollbar .body.sf.sb -orient vertical -command {.series yview}
grid .series .body.sf.sb -in .body.sf -sticky nsew
grid rowconfigure .body.sf 0 -weight 1
grid columnconfigure .body.sf 0 -weight 1
bind .series <Double-1> {dlg_setprog}
bind .series <<TreeviewSelect>> on_select

frame .body.lf
text .body.log -height 12 -wrap word -state disabled -yscrollcommand {.body.lf.sb set}
ttk::scrollbar .body.lf.sb -orient vertical -command {.body.log yview}
grid .body.log .body.lf.sb -in .body.lf -sticky nsew
grid rowconfigure .body.lf 0 -weight 1
grid columnconfigure .body.lf 0 -weight 1

.body add .body.sf -weight 3
.body add .body.lf -weight 2
grid .body -row 1 -sticky nsew

ttk::labelframe .sel -text "Selected series" -padding {10 8}
ttk::label  .sel.title -text "—"
ttk::button .sel.lex   -text "Edit lexicon"   -command edit_lexicon
ttk::button .sel.ovl   -text "Edit overrides" -command edit_overlay
ttk::button .sel.redo  -text "Re-render…"     -command dlg_redo
ttk::label  .sel.vl    -text "Narrator:"
ttk::combobox .sel.voice -width 16 -state readonly -values $::VOICES
bind .sel.voice <<ComboboxSelected>> set_narrator
pack .sel.title -side left -padx {0 14}
pack .sel.lex .sel.ovl .sel.redo -side left -padx {0 6}
pack .sel.voice -side right
pack .sel.vl    -side right -padx {12 4}
grid .sel -row 2 -sticky ew -padx 6 -pady {4 4}

ttk::labelframe .sched -text "Auto-sync" -padding {10 8}
ttk::checkbutton .sched.on -text "enabled" -variable ::AUTO -command schedule_next
ttk::radiobutton .sched.mi -text "every" -variable ::MODE -value interval -command schedule_next
ttk::spinbox .sched.iv -from 1 -to 1440 -width 5 -textvariable ::IVAL -command schedule_next
ttk::label .sched.mm -text "min"
ttk::radiobutton .sched.md -text "daily at" -variable ::MODE -value daily -command schedule_next
ttk::entry .sched.dt -width 7 -textvariable ::DAILY
ttk::label .sched.nl -text "next run:"
ttk::label .sched.next -textvariable ::NEXTRUN -foreground "#2a9d5c"
bind .sched.iv <FocusOut> schedule_next
bind .sched.dt <Return>   schedule_next
bind .sched.dt <FocusOut> schedule_next
pack .sched.on -side left -padx {0 16}
pack .sched.mi -side left -padx {0 4}
pack .sched.iv -side left
pack .sched.mm -side left -padx {4 16}
pack .sched.md -side left -padx {0 4}
pack .sched.dt -side left
pack .sched.next -side right -padx {8 0}
pack .sched.nl -side right
grid .sched -row 3 -sticky ew -padx 6 -pady {4 4}

ttk::labelframe .srv -text "Feed server" -padding {10 8}
ttk::button .srv.start -text "Start" -command server_start
ttk::button .srv.stop  -text "Stop"  -command server_stop -state disabled
ttk::label  .srv.status -text "off" -foreground "#a33"
pack .srv.start -side left -padx {0 6}
pack .srv.stop  -side left -padx {0 12}
pack .srv.status -side left
grid .srv -row 4 -sticky ew -padx 6 -pady {0 8}

grid rowconfigure . 1 -weight 1
grid columnconfigure . 0 -weight 1

# ---------------------------------------------------------------- start --------
load_conf
ui_busy 0
log "webnovel-audio control — exe: $::EXE"
if {[set c [run_json config]] ne ""} {
    set ::CFGPATH   [json::get $c config_path]
    set ::LEXDIR    [json::get $c lexicon_dir]
    set ::SERIESDIR [json::get $c series_config_dir]
    if {[json::get $c base_lexicon] ne ""} { set ::BASELEX [json::get $c base_lexicon] }
    set v [json::get $c voices]
    if {[llength $v]} { set ::VOICES [linsert $v 0 "(default)"] }
    .sel.voice configure -values $::VOICES
    if {$::CFGPATH eq ""} { set ::CFGPATH [file join [json::get $c project_dir] config.toml] }
    set ::CFGPATH [file normalize $::CFGPATH]
    log "config   : $::CFGPATH"
    log "state db : [json::get $c state_db]"
    log "library  : [json::get $c library_dir]"
}
on_select
refresh_series
schedule_next
wm protocol . WM_DELETE_WINDOW {server_stop ; save_conf ; exit}
