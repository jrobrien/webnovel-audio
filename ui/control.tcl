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
#   webnovel-audio ui                    # finds and exports the CLI path
#   python ui/host.py ui/control.tcl     # same thing, without the CLI
#
# The supported host is Python's tkinter — one interpreter, one path to test.
# `wish ui/control.tcl` still works and is occasionally handy for poking at a
# Tcl-level problem, but it is not what `webnovel-audio ui` runs and is not
# covered by the tests.

package require Tk
source [file join [file dirname [info script]] json.tcl]

if {![info exists ::env(HOME)] || $::env(HOME) eq ""} {
    puts stderr "webnovel-audio ui: \$HOME is not set"
    exit 1
}
;# Where the window geometry, theme and font size are remembered.
;#
;# The env override is for tests: everything below load_conf runs at *source*
;# time, so a probe that sources control.tcl has already read the real
;# ~/.config/webnovel-audio/ui.conf before it gets a chance to redirect the
;# path -- and then asserts against whatever the developer happened to leave
;# in it. Cost a green suite once, when a saved `fontzoom 2` made the font
;# probe boot at a zoom it did not expect.
if {[info exists ::env(WEBNOVEL_AUDIO_UI_CONF)]
    && $::env(WEBNOVEL_AUDIO_UI_CONF) ne ""} {
    set ::UICONF $::env(WEBNOVEL_AUDIO_UI_CONF)
    set ::CFGDIR [file dirname $::UICONF]
} else {
    set ::CFGDIR [file join $::env(HOME) .config webnovel-audio]
    set ::UICONF [file join $::CFGDIR ui.conf]
}

;# platform context button — gitk does the same dance for macOS
set ::CTXBUT <Button-3>
if {[tk windowingsystem] eq "aqua"} { set ::CTXBUT <Button-2> }

array set ::geom {main 1280x820 topheight 230 botwidth 760}

set ::CFGPATH "" ; set ::LEXDIR "data/lexicons" ; set ::SERIESDIR "data/series"
array set ::BPATH {}
set ::BASELEX ""
set ::SERIES "" ; set ::RUNNING 0 ; set ::PIPE "" ; set ::SERVEPID ""
set ::SERVEPIPE "" ; set ::STATUS "ready" ; set ::LIMIT 10
array set ::EDIT {}          ;# tab -> path / mtime / dirty
array set ::MD {}            ;# chapter row id -> text_path / status, from refresh
array set ::HLAFTER {}       ;# tab -> pending re-highlight, coalesced

# ---------------------------------------------------------------- theme -------
;# This UI has no themes. It has an X resource database, which the desktop
;# fills in (Omarchy publishes the live palette to the root window on every
;# `omarchy theme set`) and which Tk reads at startup on its own -- so on
;# Tk 9 the app is already the right colour before any of this runs.
;#
;#   system  whatever the resource database says, untouched
;#   dark    ui/theme/dark.Xresources  overriding it  (Catppuccin Mocha)
;#   light   ui/theme/light.Xresources overriding it  (Catppuccin Latte)
;#
;# light and dark are not themes in any sense this file has to know about:
;# they are the same resource names loaded at a priority that outranks the
;# root window. One code path resolves all three -- read the database, push
;# what ttk cannot read for itself into ttk::style, repaint our own widgets.
;# See ui/xres.tcl.
source [file join [file dirname [file normalize [info script]]] xres.tcl]

set ::THEME system               ;# system|light|dark; persisted in ui.conf
set ::PAL ""                     ;# resolved colours, from xres::palette

;# The system answer has to be captured before anything can override it: Tk
;# has no `option delete`, so once a light/dark fragment is in the database
;# at interactive priority it is there for the life of the process, and
;# switching back to "system" can only be served from a snapshot. Taken here,
;# at source time, which is before the first apply_theme.
;# Before the snapshot, so a machine with nothing on its root window gets a
;# coherent palette rather than clamx's clam greys wrapped around our own
;# Mocha fallbacks. A no-op on any machine that publishes resources.
if {[xres::install_floor]} { set ::FLOORED 1 } else { set ::FLOORED 0 }
set ::xres::SYSTEM [xres::snapshot]

proc pal {key} { return [dict get $::PAL $key] }

proc apply_theme {{name ""}} {
    if {$name ne ""} { set ::THEME $name }
    set vals [xres::apply $::THEME]
    if {$vals eq ""} {
        log "! no $::THEME.Xresources -- staying on the system palette"
        set ::THEME system
        set vals $::xres::SYSTEM
    }
    xres::style_ttk $vals
    set ::PAL [xres::palette $vals]
    repaint_theme
}

;# Every colour the ttk theme does not reach: classic text widgets, menus,
;# treeview row tags, and the syntax tags.
proc repaint_theme {} {
    set bg [pal field] ; set fg [pal fg] ; set sel [pal sel] ; set selfg [pal selfg]
    foreach w {cast lex md log} {
        set t .br.$w.t
        if {![winfo exists $t]} continue
        $t configure -background $bg -foreground $fg -insertbackground $fg \
            -selectbackground $sel -selectforeground $selfg \
            -highlightthickness 0 -borderwidth 0
        foreach {tag key} {hl_com com hl_key key hl_str str hl_head head} {
            $t tag configure $tag -foreground [pal $key]
        }
        $t tag configure hl_em -foreground [pal em]
        $t tag configure hl_dim -foreground [pal dim]
    }
    foreach m {.ctx .sctx .tool.theme.m} {
        if {![winfo exists $m]} continue
        $m configure -background [pal bg] -foreground [pal fg] \
            -activebackground $sel -activeforeground $selfg \
            -selectcolor [pal fg]
    }
    retag_rows
    foreach w {cast lex md} { if {[winfo exists .br.$w.t]} { highlight $w } }
}

;# Stage colours for the chapter rows, and the greyed-out paused series.
proc retag_rows {} {
    if {[winfo exists .bl.tv]} {
        foreach tag {rendered error skipped fetched parsed} {
            .bl.tv tag configure $tag -foreground [pal $tag]
        }
    }
    if {[winfo exists .top.tv]} { .top.tv tag configure off -foreground [pal off] }
}

proc set_theme {name} {
    apply_theme $name
    log "theme: $::THEME"
}


# ------------------------------------------------------------ font size ------
;# One zoom level for the whole window, applied to Tk's named fonts.
;#
;# Every font in this UI is a named font -- the text panes ask for TkFixedFont
;# and everything else inherits TkDefaultFont/TkHeadingFont through ttk -- so
;# reconfiguring the named fonts moves the entire window and nothing has to be
;# hunted down widget by widget. Tk relays out on its own when a named font
;# changes.
;#
;# A single additive step rather than a scale factor: the bases differ
;# (TkCaptionFont 12, TkSmallCaptionFont 9) and an additive step keeps them in
;# the same order without rounding two of them onto the same size, which is
;# what a multiplier does at small sizes.
set ::FONTZOOM 0                ;# steps from the desktop's own sizes
array set ::FONTBASE {}         ;# font -> its size before we touched anything

;# The bases have to be read before any zoom is applied, or a saved zoom
;# would compound on every launch. Called once, at startup.
proc font_capture {} {
    foreach f [font names] {
        ;# Only Tk's own named fonts. Anything else belongs to whoever
        ;# created it and is not ours to resize.
        if {[string match Tk* $f]} { set ::FONTBASE($f) [font configure $f -size] }
    }
}

;# The size $f would have at zoom $z. Tk reads a POSITIVE -size as points and
;# a NEGATIVE one as pixels, so "bigger" is away from zero in both directions
;# -- get this wrong and the zoom runs backwards on a pixel-sized desktop.
proc font_size_at {f z} {
    set base $::FONTBASE($f)
    return [expr {$base < 0 ? $base - $z : $base + $z}]
}

proc font_apply {} {
    foreach f [array names ::FONTBASE] {
        font configure $f -size [font_size_at $f $::FONTZOOM]
    }
    ;# Treeview rows do NOT follow their font: -rowheight is a style setting
    ;# ttk computes once, from the font as it was when the theme was set up.
    ;# Leave it alone and zooming in clips every row of both trees.
    ttk::style configure Treeview \
        -rowheight [expr {[font metrics TkDefaultFont -linespace] + 4}]
}

;# Step the zoom, refusing to go somewhere illegible rather than clamping
;# silently -- a clamp at the bottom makes ctrl-minus look broken, a refusal
;# with a bell reads as "that is as far as it goes".
proc font_zoom {step} {
    set want [expr {$::FONTZOOM + $step}]
    foreach f [array names ::FONTBASE] {
        if {abs([font_size_at $f $want]) < 6 || abs([font_size_at $f $want]) > 42} {
            bell
            return
        }
    }
    set ::FONTZOOM $want
    font_apply
    log "font size: [expr {$want >= 0 ? "+$want" : $want}]"
}

proc font_reset {} {
    set ::FONTZOOM 0
    font_apply
    log "font size: reset"
}

# ---------------------------------------------------------------- highlight ---
;# Deliberately shallow: enough structure to find your place, not a parser.
;# $EDITOR is one button away and does this properly — so every rule here is
;# line-anchored and single-pass, which is what keeps re-highlighting on every
;# keystroke affordable and keeps a wrong guess cosmetic.
set ::HL_MAX 400000          ;# characters; past this, skip rather than stall

proc highlight {which} {
    set t .br.$which.t
    if {![winfo exists $t]} return
    foreach tg {hl_com hl_key hl_str hl_head hl_em hl_dim} { $t tag remove $tg 1.0 end }
    if {[$t count -chars 1.0 end] > $::HL_MAX} return
    set last [lindex [split [$t index end-1c] .] 0]
    switch -- $which {
        lex  { hl_csv  $t $last }
        cast { hl_toml $t $last }
        md   { hl_md   $t $last }
    }
}

;# one tagged span on line $i, from character offsets within the line
proc hl_span {t i tag from to} { $t tag add $tag $i.$from $i.$to }

proc hl_csv {t last} {
    for {set i 1} {$i <= $last} {incr i} {
        set ln [$t get $i.0 $i.end]
        if {[string index [string trimleft $ln] 0] eq "#"} {
            hl_span $t $i hl_com 0 end ; continue
        }
        if {$ln eq ""} continue
        ;# the surface column — the field you scan for when hunting a word
        if {[set c [string first "," $ln]] > 0} { hl_span $t $i hl_key 0 $c }
        if {[regexp -indices {^surface,pos,respell,notes} $ln m]} {
            hl_span $t $i hl_head 0 end
        }
    }
}

proc hl_toml {t last} {
    for {set i 1} {$i <= $last} {incr i} {
        set ln [$t get $i.0 $i.end]
        set trimmed [string trimleft $ln]
        if {[string index $trimmed 0] eq "#"} { hl_span $t $i hl_com 0 end ; continue }
        if {[string index $trimmed 0] eq "\["} { hl_span $t $i hl_head 0 end ; continue }
        if {[regexp -indices {^[ \t]*[^=]+=} $ln m]} {
            lassign $m a b ; hl_span $t $i hl_key $a [expr {$b}]
        }
        foreach m [regexp -all -indices -inline {"[^"]*"} $ln] {
            lassign $m a b ; hl_span $t $i hl_str $a [expr {$b + 1}]
        }
    }
}

proc hl_md {t last} {
    set fm 0                              ;# inside the --- front matter block
    for {set i 1} {$i <= $last} {incr i} {
        set ln [$t get $i.0 $i.end]
        if {$i == 1 && $ln eq "---"} { set fm 1 ; hl_span $t $i hl_dim 0 end ; continue }
        if {$fm} {
            hl_span $t $i hl_dim 0 end
            if {$ln eq "---"} { set fm 0 }
            continue
        }
        if {[regexp {^#{1,6}\s} $ln]} { hl_span $t $i hl_head 0 end ; continue }
        if {[regexp {^\s*([-*_])(\s*\1){2,}\s*$} $ln]} { hl_span $t $i hl_dim 0 end ; continue }
        foreach m [regexp -all -indices -inline {\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_} $ln] {
            lassign $m a b ; hl_span $t $i hl_em $a [expr {$b + 1}]
        }
    }
}

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
    ;# Startup work (theme loading) runs before the log pane exists. Falling
    ;# back to stderr rather than erroring is what makes a failure there
    ;# visible — a silently swallowed one hid a broken theme load for a whole
    ;# interpreter version.
    if {![winfo exists .br.log.t]} { puts stderr $msg ; return }
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
        set on [expr {[json::get $r enabled] eq "0" ? 0 : 1}]
        .top.tv insert {} end -id [dict get $r slug] -values [list \
            [dict get $r title] \
            [expr {$on ? "on" : "paused"}] \
            [json::get $r priority] \
            [json::get $r status] \
            [json::get $st rendered] \
            [dict get $r pending] \
            [dict get $r errors] \
            [json::get $r next title]] \
            -tags [expr {$on ? "on" : "off"}]
    }
    retag_rows              ;# themed, not hardcoded gray55
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

;# Scroll the chapter list to the first chapter still to be done.
;#
;# A long-running series is hundreds of rendered rows followed by the few
;# that matter, so the list opens on ancient history and every visit starts
;# with the same scroll to the bottom. This puts the interesting end on
;# screen instead.
;#
;# "Still to be done" excludes `skipped` as well as `rendered`: a skipped
;# chapter is deliberately not going to be rendered, and stopping at one
;# would pin the view to a decision already made. Everything else -- new,
;# fetched, parsed, error -- is unfinished work.
;#
;# Only called from on_series_select, NOT from refresh_chapters. Refresh runs
;# on its own after a render finishes and from the Refresh button, and
;# yanking the view out from under someone who has scrolled somewhere
;# deliberately is worse than the problem being fixed.
proc show_first_unfinished {{tries 60}} {
    ;# Wait for the tree to be on screen before scrolling it.
    ;#
    ;# At startup this runs from `after idle`, which fires before the
    ;# toplevel is mapped: the treeview is then 1 pixel tall and reports
    ;# `yview {0.0 1.0}` -- it believes the whole list is already visible, so
    ;# `moveto` has nothing to scroll and silently does nothing. The window
    ;# maps a moment later at its real height, still showing chapter 1, which
    ;# is exactly the case this feature exists for and the one it was missing.
    ;# Measured: mapped=0 height=1 at idle, mapped=1 height=523 by 1500ms.
    ;#
    ;# Bounded, so a tree that never maps (a probe that exits early, a
    ;# withdrawn window) stops rather than rescheduling itself forever.
    if {![winfo ismapped .bl.tv] || [winfo height .bl.tv] <= 1} {
        if {$tries > 0} { after 30 [list show_first_unfinished [incr tries -1]] }
        return
    }
    set kids [.bl.tv children {}]
    set total [llength $kids]
    if {$total == 0} return
    set target -1
    for {set i 0} {$i < $total} {incr i} {
        if {[.bl.tv set [lindex $kids $i] status] ni {rendered skipped}} {
            set target $i
            break
        }
    }
    ;# Nothing outstanding: leave the view alone rather than guess.
    if {$target < 0} return
    ;# A couple of rendered rows above it, so it reads as "here is the edge"
    ;# rather than as a list that happens to start there. moveto clamps at
    ;# the end of the range on its own, so a target in the last screenful
    ;# still lands visible.
    set lead [expr {$target > 2 ? $target - 2 : 0}]
    .bl.tv yview moveto [expr {double($lead) / $total}]
}

proc on_series_select {} {
    set slug [selected_series]
    if {$slug eq "" || $slug eq $::SERIES} return
    set ::SERIES $slug
    refresh_chapters
    ;# after idle: the treeview has just been repopulated and does not know
    ;# its own scroll range until it has laid out, so moveto before then
    ;# scrolls against a stale total and lands in the wrong place.
    after idle show_first_unfinished
    load_editor cast
    load_editor lex
}

# ---------------------------------------------------------------- chapters ----
proc refresh_chapters {} {
    set keep [.bl.tv selection]
    .bl.tv delete [.bl.tv children {}]
    array unset ::MD
    if {$::SERIES eq ""} { load_md ; return }
    set d [run_json state show --scope $::SERIES]
    if {$d eq ""} return
    set vols 0
    foreach c [dict get $d chapters] {
        set n [dict get $c number]
        set dur [json::get $c duration_s]
        if {$dur ne "" && $dur > 0} { set dur "[expr {int($dur/60)}]m" } else { set dur "" }
        set st [dict get $c status]
        set note [json::get $c error_stage]
        if {$note ne ""} { append note ": [string range [json::get $c error] 0 48]" }
        set v [json::get $c volume_index]
        if {$v ne "" && $v ne "0"} {
            set v "V$v"
            if {[json::get $c volume_chapter] ne ""} { incr vols }
        } else { set v "" }
        .bl.tv insert {} end -id ch$n -values \
            [list $n $v [dict get $c title] $st $dur $note] -tags $st
        ;# stashed for the Markdown pane, so selecting a row costs no subprocess
        set ::MD(ch$n,path)   [json::get $c text_path]
        set ::MD(ch$n,status) $st
    }
    retag_rows
    foreach id $keep { if {[.bl.tv exists $id]} { .bl.tv selection add $id } }
    ;# Vol only earns its place when the series actually has volumes
    set cols {n}
    if {$vols} { lappend cols vol }
    lappend cols title status dur
    foreach id [.bl.tv children {}] {
        if {[.bl.tv set $id note] ne ""} { lappend cols note ; break }
    }
    .bl.tv configure -displaycolumns $cols
    if {[series_paused $::SERIES]} {
        status "$::SERIES — [llength [.bl.tv children {}]] chapters · PAUSED, excluded from sync"
    } else {
        status "$::SERIES — [llength [.bl.tv children {}]] chapters"
    }
    load_md
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

proc series_paused {slug} {
    return [expr {[.top.tv exists $slug] && [.top.tv set $slug sync] eq "paused"}]
}

proc toggle_pause {} {
    set slug [selected_series]
    if {$slug eq ""} return
    set verb [expr {[series_paused $slug] ? "enable" : "disable"}]
    run_json series $verb --scope $slug
    log "$slug: sync [expr {$verb eq {enable} ? {resumed} : {paused}}]"
    refresh_series
    on_series_status
}

# Render order. Higher goes first; pausing is a separate axis and leaves this
# alone, so resuming puts the series back where it was.
proc dlg_priority {} {
    set slug [selected_series]
    if {$slug eq ""} return
    set cur [.top.tv set $slug prio]
    set w .prio ; catch {destroy $w}
    toplevel $w ; wm title $w "Priority" ; wm transient $w .
    ttk::label $w.l -text "Render priority for $slug:"
    ttk::spinbox $w.n -from -9999 -to 9999 -increment 100 -width 8
    $w.n set $cur
    ttk::label $w.h -text "higher renders first · 100 is the default\npausing is separate and keeps this value" \
        -justify left -foreground [pal dim]
    ttk::frame $w.b
    ttk::button $w.b.ok -text "Set" -command [list do_priority $w $slug]
    ttk::button $w.b.cx -text "Cancel" -command [list destroy $w]
    pack $w.b.ok $w.b.cx -side left -padx 4
    grid $w.l -row 0 -column 0 -columnspan 2 -padx 10 -pady {10 4} -sticky w
    grid $w.n -row 1 -column 0 -padx 10 -sticky w
    grid $w.h -row 2 -column 0 -columnspan 2 -padx 10 -pady 6 -sticky w
    grid $w.b -row 3 -column 0 -columnspan 2 -pady 8
    focus $w.n
    bind $w <Return> [list do_priority $w $slug]
    bind $w <Escape> [list destroy $w]
}

proc do_priority {w slug} {
    set v [string trim [$w.n get]]
    destroy $w
    if {![string is integer -strict $v]} { log "! priority must be an integer" ; return }
    run_json series priority --scope $slug --priority $v
    log "$slug: priority $v"
    refresh_series
}

proc series_ctx {X Y x y} {
    set id [.top.tv identify row $x $y]
    if {$id ne ""} { .top.tv selection set $id }
    set slug [selected_series]
    if {$slug eq ""} return
    .sctx entryconfigure 0 -label \
        [expr {[series_paused $slug] ? "Resume sync" : "Pause sync (exclude from sync)"}]
    tk_popup .sctx $X $Y
}

;# keep the status bar honest about why a series might not be syncing
proc on_series_status {} {
    set slug [selected_series]
    if {$slug eq ""} return
    if {[series_paused $slug]} {
        status "$slug — paused, excluded from sync"
    }
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
    run_cmd [list $stage --target $::SERIES --range $r]
}

proc report_check {range} {
    .br select .br.log
    log "\$ webnovel-audio check $::SERIES $range"
    status "checking $range…"
    set d [run_json check --target $::SERIES --range $range]
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
    set d [run_json cast update --scope $::SERIES --range $range]
    if {$d eq ""} return
    log "  [json::get $d overlay_action]: [json::get $d overlay_path]"
    load_editor cast 1
    .br select .br.cast
}

proc do_state {status} {
    set r [selected_range]
    if {$::SERIES eq "" || $r eq ""} { log "select chapters first" ; return }
    run_json state set --scope $::SERIES --range $r --status $status
    refresh_chapters ; refresh_series
}

proc do_reset_errors {} {
    if {$::SERIES eq ""} return
    set r [selected_range]
    if {$r eq ""} { run_json state reset --scope $::SERIES } else { run_json state reset --scope $::SERIES --range $r }
    refresh_chapters ; refresh_series
}

# ---------------------------------------------------------------- sync --------
proc dlg_sync {} {
    if {$::SERIES eq ""} { log "select a series" ; return }
    if {[series_paused $::SERIES]} {
        log "! $::SERIES is paused — right-click the series to resume it"
        status "$::SERIES is paused"
        return
    }
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
    set a [list sync --scope $::SERIES --estimate]
    if {$::LIMIT > 0} { lappend a --limit $::LIMIT }
    set d [run_json {*}$a]
    if {$d eq ""} { $w.est configure -text "estimate unavailable" ; return }
    $w.est configure -text "[json::get $d chapters] chapter(s), ~[json::get $d human] of CPU"
}

proc do_sync {w} {
    set lim $::LIMIT ; destroy $w
    set a [list sync --scope $::SERIES --yes]
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
    set d [run_json series add --url $url --from $from]
    if {$d ne ""} { log "added [json::get $d title] — [json::get $d chapters] chapters" }
    refresh_series
}

# ---------------------------------------------------------------- editors ----
;# The cast/lexicon files are small and hand-owned, so edit them in-pane. But
;# `cast update` appends to the cast file behind our back — so track mtime and
;# refuse to clobber a file that changed on disk since we loaded it.
;# Both files live in the series bundle now, whose location the CLI owns —
;# a relocated series is not under the library root. Cached per series so
;# selecting one costs a single `series show`.
proc editor_path {which} {
    if {$::SERIES eq ""} { return "" }
    if {![info exists ::BPATH($::SERIES,$which)]} { cache_bundle_paths $::SERIES }
    if {[info exists ::BPATH($::SERIES,$which)]} { return $::BPATH($::SERIES,$which) }
    ;# fall back to the pre-bundle layout so an un-migrated tree still edits
    if {$which eq "cast"} { return [file join $::SERIESDIR $::SERIES.toml] }
    return [file join $::LEXDIR $::SERIES.csv]
}

proc cache_bundle_paths {slug} {
    set d [run_json series show --scope $slug]
    if {$d eq "" || ![dict exists $d paths]} return
    set p [dict get $d paths]
    set ::BPATH($slug,cast) [dict get $p config]
    set ::BPATH($slug,lex)  [dict get $p lexicon]
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
    highlight $which
}

# ---------------------------------------------------------------- markdown ---
;# The parsed chapter text, read-only. Its path comes from the `state show`
;# already done by refresh_chapters (text_path is in that payload), so moving
;# the selection costs a file read and no subprocess.
proc md_set {body {path ""}} {
    set t .br.md.t
    $t configure -state normal
    $t delete 1.0 end
    $t insert 1.0 $body
    $t configure -state disabled
    set ::EDIT(md,path) $path
    .br.md.b.ext state [expr {$path eq "" ? "disabled" : "!disabled"}]
    .br tab .br.md -text [expr {$path eq "" ? "Markdown" : "Markdown — [file tail $path]"}]
    highlight md
}

proc load_md {} {
    if {![winfo exists .br.md.t]} return
    set sel [.bl.tv selection]
    if {$::SERIES eq "" || ![llength $sel]} {
        md_set "Select a chapter to read its Markdown here." ; return
    }
    if {[llength $sel] > 1} {
        md_set "Markdown view only supported for single chapter selected.\
                \n\n[llength $sel] chapters are selected." ; return
    }
    set id [lindex $sel 0]
    set n [.bl.tv set $id n]
    set path [expr {[info exists ::MD($id,path)] ? $::MD($id,path) : ""}]
    set st   [expr {[info exists ::MD($id,status)] ? $::MD($id,status) : ""}]
    if {$path eq ""} {
        ;# No .md recorded. Which stage is missing decides what to suggest.
        switch -- $st {
            new     { set why "Chapter #$n has not been fetched yet.\
                               \n\nRight-click it and run Fetch, then Parse, to\
                               produce the Markdown." }
            fetched { set why "Chapter #$n is fetched but not parsed.\
                               \n\nRight-click it and run Parse to produce the\
                               Markdown." }
            skipped { set why "Chapter #$n is marked skipped, so it was never\
                               parsed.\n\nMark it new and run Fetch/Parse to\
                               produce the Markdown." }
            default { set why "No Markdown file is recorded for chapter #$n\
                               (stage: $st).\n\nRight-click it and run Parse to\
                               regenerate it." }
        }
        md_set $why ; return
    }
    if {![file exists $path]} {
        md_set "The Markdown for chapter #$n is recorded at\n\n  $path\n\nbut\
                that file is missing. Right-click the chapter and run Parse to\
                write it again."
        return
    }
    if {[catch {open $path r} fh]} { md_set "! cannot open $path\n\n$fh" ; return }
    fconfigure $fh -encoding utf-8
    set body [read $fh] ; close $fh
    md_set $body $path
    ;# the filename goes on the tab, not the status bar — the status bar
    ;# belongs to the running command, and a filename left there goes stale
    ;# the moment the selection changes to something with no Markdown.
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
    hl_soon $which
}

;# Re-highlight after the typing stops rather than on every keystroke: one
;# coalesced pass, so holding a key down cannot queue a pass per character.
proc hl_soon {which} {
    if {[info exists ::HLAFTER($which)]} { after cancel $::HLAFTER($which) }
    set ::HLAFTER($which) [after 150 [list highlight $which]]
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
    set d [run_json state show --scope $::SERIES --range $n]
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

;# Editors that need a terminal to be any use. A GUI editor launched from
;# here is fine as-is; one of these, exec'd from a process with no
;# controlling terminal, either dies immediately or appears to do nothing at
;# all -- which is what "Open in $EDITOR" looked like before.
set ::TUI_EDITORS {nvim vim vi nano micro hx helix emacs kak ed}

;# Turn the value of $EDITOR into an argv that opens a WINDOW.
;#
;# Two cases, and the first is the one that actually bites on this desktop.
;# Omarchy sets EDITOR to `omarchy-launch-editor --inline`, and `--inline`
;# means "run in the terminal you already have" -- exec'ing the editor
;# directly instead of handing it to omarchy-launch-tui. That is right for a
;# shell and wrong for us, and dropping the flag is all it takes: without it
;# the same command opens a new terminal window by itself, and already does
;# the right thing for a GUI editor too.
;#
;# The second case is a bare `EDITOR=nvim`, where nothing is going to open a
;# window unless we do. xdg-terminal-exec is the portable spelling of "the
;# user's terminal" -- it is what omarchy-launch-tui itself calls -- and the
;# app-id keeps the window identifiable to a window rule.
proc editor_argv {raw path} {
    set argv {}
    foreach word $raw {
        if {$word eq "--inline"} continue
        lappend argv $word
    }
    if {[llength $argv] == 0} { return [list xdg-open $path] }
    if {[file tail [lindex $argv 0]] in $::TUI_EDITORS
        && [auto_execok xdg-terminal-exec] ne ""} {
        return [list xdg-terminal-exec --app-id=webnovel-audio-editor \
                     -e {*}$argv $path]
    }
    return [concat $argv [list $path]]
}

proc external_editor {which} {
    set path $::EDIT($which,path)
    if {$path eq ""} return
    foreach v {WEBNOVEL_AUDIO_EDITOR VISUAL EDITOR} {
        if {![info exists ::env($v)]} continue
        set raw [string trim $::env($v)]
        if {$raw eq ""} continue
        set argv [editor_argv $raw $path]
        log "editor: [lrange $argv 0 end-1] …"
        if {[catch {exec {*}$argv &} e]} { log "! $e" }
        return
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
    puts $fh "theme $::THEME"
    puts $fh "fontzoom $::FONTZOOM"
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
        ;# Guarded: ui.conf is a plain text file a user may well edit, and a
        ;# junk zoom would otherwise make every font call error out at
        ;# startup, before the log pane exists to say why.
        if {$k eq "fontzoom" && [string is integer -strict $v]} {
            set ::FONTZOOM $v
        }
        ;# Accept the names this UI used before it had only three: a
        ;# saved ui.conf outlives the code that wrote it, and a stale value
        ;# reaching apply_theme would look for a $want.Xresources that is
        ;# not there and log a failure on every launch.
        if {$k eq "theme"} {
            set ::THEME [switch -- $v {
                auto - omarchy {format system}
                catppuccin-dark {format dark}
                catppuccin-light {format light}
                default {expr {$v in {system light dark} ? $v : "system"}}
            }]
        }
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
;# Before load_conf, which may carry a saved zoom: the bases must be the
;# desktop's own sizes, or a saved zoom would compound on every launch.
font_capture
load_conf
;# Both before any widget is built, so widgets are created at the right
;# colours and the right size rather than being corrected afterwards.
apply_theme
font_apply
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
;# Labelled "View" rather than "Theme": it carries both appearance settings
;# now, and the widget path stays .tool.theme so repaint_theme keeps finding
;# the menu.
ttk::menubutton .tool.theme -text "View" -direction below -menu .tool.theme.m
menu .tool.theme.m -tearoff 0
foreach {lbl val} {"System" system  "Light" light  "Dark" dark} {
    .tool.theme.m add radiobutton -label $lbl -value $val -variable ::THEME \
        -command [list set_theme $val]
}
.tool.theme.m add separator
;# Tk reads the root window's resource database once, at startup, and
;# `omarchy theme set` pushes no live-update signal to a running app -- so a
;# desktop theme switch made since launch is invisible until something goes
;# and looks. This re-reads the fragment behind whichever mode is current
;# (for "system", the live one the desktop wrote), so one label covers all
;# three.
.tool.theme.m add command -label "Reload colours" \
    -command {apply_theme ; log "colours reloaded ($::THEME)"}
.tool.theme.m add separator
;# The accelerator labels are the point of putting these in the menu at all
;# -- the keys are how anyone will actually use it.
.tool.theme.m add command -label "Larger text"  -accelerator "Ctrl +" \
    -command {font_zoom 1}
.tool.theme.m add command -label "Smaller text" -accelerator "Ctrl -" \
    -command {font_zoom -1}
.tool.theme.m add command -label "Reset text size" -accelerator "Ctrl 0" \
    -command font_reset
pack .tool.theme -side right -padx 2
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
ttk::treeview .top.tv -columns {title sync prio status rendered pending err next} \
    -show headings -selectmode browse -yscrollcommand {.top.sb set}
foreach {c t w a s} {title Title 280 w 1   sync Sync 70 center 0
                     prio Prio 55 center 0
                     status Status 105 center 0   rendered Rendered 80 center 0
                     pending Pending 80 center 0   err Err 50 center 0
                     next "Next up" 260 w 1} {
    .top.tv heading $c -text $t
    .top.tv column $c -width $w -anchor $a -stretch $s
}
ttk::scrollbar .top.sb -orient vertical -command {.top.tv yview}
grid .top.tv .top.sb -sticky nsew
grid rowconfigure .top 0 -weight 1
grid columnconfigure .top 0 -weight 1
bind .top.tv <<TreeviewSelect>> on_series_select
bind .top.tv $::CTXBUT {series_ctx %X %Y %x %y}

menu .sctx -tearoff 0
.sctx add command -label "Pause sync" -command toggle_pause
.sctx add separator
.sctx add command -label "Refresh chapter list" -command {
    if {[selected_series] ne ""} { run_cmd [list series refresh --scope [selected_series]] }
}
.sctx add command -label "Set priority…" -command dlg_priority
.sctx add command -label "Edit cast"    -command {.br select .br.cast}
.sctx add command -label "Edit lexicon" -command {.br select .br.lex}

# -- horizontal split: chapters | notebook
ttk::panedwindow .bot -orient horizontal
.ctop add .top
.ctop add .bot

ttk::frame .bl
ttk::treeview .bl.tv -columns {n vol title status dur note} -show headings \
    -selectmode extended -yscrollcommand {.bl.sb set}
foreach {c t w a s} {n "#" 55 center 0   vol Vol 50 center 0   title Title 360 w 1
                     status Stage 85 center 0   dur Audio 65 center 0
                     note Note 260 w 0} {
    .bl.tv heading $c -text $t
    .bl.tv column $c -width $w -anchor $a -stretch $s
}
;# Note only carries the error stage/message, so it's empty for a healthy
;# series — hide it entirely rather than leaving a blank column soaking up the
;# width. -displaycolumns is re-set by refresh_chapters.
.bl.tv configure -displaycolumns {n title status dur}
ttk::scrollbar .bl.sb -orient vertical -command {.bl.tv yview}
grid .bl.tv .bl.sb -sticky nsew
grid rowconfigure .bl 0 -weight 1
grid columnconfigure .bl 0 -weight 1
bind .bl.tv $::CTXBUT {chapter_ctx %X %Y %x %y}
bind .bl.tv <Double-1> {play_selected ; break}
bind .bl.tv <<TreeviewSelect>> load_md

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

# -- notebook: Cast | Lexicon | Markdown | Log
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

;# Markdown: read-only, so no Save and no dirty tracking — and wrapped, since
;# it is prose rather than a table.
ttk::frame .br.md
text .br.md.t -wrap word -state disabled -font TkFixedFont -padx 6 -pady 4 \
    -yscrollcommand {.br.md.sb set}
ttk::scrollbar .br.md.sb -orient vertical -command {.br.md.t yview}
ttk::frame .br.md.b
ttk::button .br.md.b.rev -text Reload -command load_md
ttk::button .br.md.b.ext -text "Open in \$EDITOR" -command {external_editor md}
pack .br.md.b.rev .br.md.b.ext -side left -padx 3
grid .br.md.t .br.md.sb -sticky nsew
grid .br.md.b -columnspan 2 -sticky w -pady 4
grid rowconfigure .br.md 0 -weight 1
grid columnconfigure .br.md 0 -weight 1
.br add .br.md -text Markdown

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
;# Font size. Bound on the toplevel, which is in every descendant's bindtags,
;# so the keys work wherever focus happens to be -- including inside the
;# editable text panes. Bound on Text as well, with `break`: the toplevel tag
;# runs AFTER the class tag, so a toplevel binding alone cannot stop Text's
;# own <Control-Key> handling, and ctrl-minus would zoom AND type into the
;# cast/lexicon editors.
;#
;# Both spellings of each key: on a US layout ctrl-plus arrives as
;# <Control-plus> only when shift is held, and as <Control-equal> when it is
;# not -- "ctrl +" is what people call it either way. The keypad pair is
;# there because a numpad sends different keysyms entirely.
foreach {seq cmd} {<Control-equal>      {font_zoom 1}
                   <Control-plus>       {font_zoom 1}
                   <Control-KP_Add>     {font_zoom 1}
                   <Control-minus>      {font_zoom -1}
                   <Control-underscore> {font_zoom -1}
                   <Control-KP_Subtract> {font_zoom -1}
                   <Control-Key-0>      font_reset
                   <Control-KP_0>       font_reset} {
    bind . $seq $cmd
    bind Text $seq "$cmd ; break"
}

bind .ctop <Map> { bind %W <Map> {} ; after idle [list %W sashpos 0 $::geom(topheight)] }
bind .bot  <Map> { bind %W <Map> {} ; after idle [list %W sashpos 0 $::geom(botwidth)] }
catch {wm geometry . $::geom(main)}

# ---------------------------------------------------------------- start ------
;# Now that every widget exists, paint the ones ttk does not reach. The theme
;# itself was selected before the build so classic widgets were created with
;# the right defaults; this is what makes a later switch stick.
repaint_theme
ui_busy 0
load_md
log "webnovel-audio control — $::EXE"
log "theme: $::THEME ([ttk::style theme use])"
if {[set c [run_json config]] ne ""} {
    set ::CFGPATH   [json::get $c config_path]
    set ::LEXDIR    [json::get $c lexicon_dir]
    set ::SERIESDIR [json::get $c series_config_dir]
    set ::BASELEX   [json::get $c base_lexicon]
    log "library: [json::get $c library_dir]"
}
refresh_series
