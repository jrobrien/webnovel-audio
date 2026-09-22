# Build the real UI, then report what the colour machinery actually did.
#
# There is no theme to probe any more -- only the X resource database. The
# two checked-in override fragments (ui/theme) must work unconditionally, on
# any machine, Omarchy or not: that is the whole point of shipping them.
# "system" is whatever this particular machine's root window says, so it is
# reported rather than asserted against fixed values.
set LOG [open [lindex $argv 0] w]
fconfigure $LOG -buffering none
source ui/control.tcl

proc probe_mode {want} {
    apply_theme $want
    puts $::LOG "use $want -> ttk=[ttk::style theme use] mode=$::THEME"
    ;# The bug this exists to catch: `ttk::style theme use` does not update
    ;# ::ttk::currentTheme, only `ttk::setTheme` does, and a Python tkinter
    ;# host reads exactly that variable -- so this is the one assertion a
    ;# wish-only test cannot see.
    puts $::LOG "  ttk::currentTheme $::ttk::currentTheme"
    ;# ttk must agree with the resource database. On Tk 9 with mode=system it
    ;# already does before we touch it; on Tk 8.6, and after any override,
    ;# only xres::style_ttk makes it so -- and that is the whole reason the
    ;# proc runs unconditionally rather than being version-sniffed.
    puts $::LOG "  ttk.bg [ttk::style lookup . -background]"
    puts $::LOG "  ttk.fg [ttk::style lookup . -foreground]"
    puts $::LOG "  md.t.bg [.br.md.t cget -background]"
    puts $::LOG "  md.t.fg [.br.md.t cget -foreground]"
    set rendered [.bl.tv tag configure rendered -foreground]
    set error    [.bl.tv tag configure error -foreground]
    set fetched  [.bl.tv tag configure fetched -foreground]
    set parsed   [.bl.tv tag configure parsed -foreground]
    puts $::LOG "  tag.rendered $rendered"
    puts $::LOG "  tag.error $error"
    puts $::LOG "  tag.fetched $fetched"
    puts $::LOG "  tag.parsed $parsed"
    puts $::LOG "  tag.skipped [.bl.tv tag configure skipped -foreground]"
    ;# The four hue-bearing status colours must actually be four colours.
    ;# Previously backed by the theme package's distinct_hues (exhaustive,
    ;# contrast-floored); with the package gone this is a plain distinctness
    ;# count, and the named hues it draws on are an extension the vendored
    ;# fragments publish and Omarchy's has been asked to.
    puts $::LOG "  distinct.count [llength [lsort -unique \
        [list $rendered $error $fetched $parsed]]]"
    ;# Tabs: the unselected state is a separate style from the strip behind
    ;# it, with its own compiled-in colour, so it is the one that goes grey
    ;# while everything around it is correct. Reported from the real UI.
    puts $::LOG "  tab.bg [ttk::style lookup TNotebook.Tab -background]"
    puts $::LOG "  tab.bg.selected [ttk::style lookup TNotebook.Tab -background selected]"

    ;# The general form of that bug: any style option still answering with
    ;# one of Tk's compiled-in greys (or a bare `black` arrow glyph, which is
    ;# invisible on a dark background and leaves no hint that it is there)
    ;# has escaped xres::style_ttk. Zero is the only acceptable count -- on
    ;# Tk 9 with mode=system ttk reads the database itself and this is free,
    ;# but on 8.6, and under any override, only style_ttk makes it hold.
    set greys {#d9d9d9 #c3c3c3 #b3b3b3 #ececec #a3a3a3 #4a6984 black white
               gray grey}
    set found {}
    foreach st {. TFrame TLabel TButton TMenubutton TNotebook TNotebook.Tab
                TScrollbar Vertical.TScrollbar Horizontal.TScrollbar
                TPanedwindow Sash TSeparator TEntry TSpinbox TCombobox
                Treeview Treeview.Heading} {
        foreach opt {-background -foreground -fieldbackground -troughcolor
                     -bordercolor -arrowcolor -lightcolor -darkcolor
                     -selectbackground -insertcolor} {
            foreach state {{} selected active disabled readonly} {
                if {[catch {ttk::style lookup $st $opt $state} v]} continue
                if {$v ne "" && [lsearch -nocase $greys $v] >= 0} {
                    lappend found "$st$opt([join $state ,])=$v"
                }
            }
        }
    }
    puts $::LOG "  greys [llength $found]"
    if {[llength $found]} { puts $::LOG "  greys.found $found" }

    set dfg [ttk::style lookup TButton -foreground disabled]
    set bg [ttk::style lookup . -background]
    puts $::LOG "  disabled.fg $dfg"
    puts $::LOG "  disabled.distinct [expr {$dfg ne $bg}]"
}

proc report {} {
    puts $::LOG "tk [info patchlevel]"
    puts $::LOG "families [llength [font families]]"
    puts $::LOG "live_fragment [file readable [xres::live_fragment]]"
    puts $::LOG "clamx.version $ttk::theme::clamx::version wanted $::xres::CLAMX_VERSION"

    ;# Geometry parity with clam, measured on a real widget.
    ;#
    ;# clamx is `-parent clam`, which carries clam's layouts and elements but
    ;# NOT its style settings -- so a child theme silently loses -relief,
    ;# -padding, -width, -font and the rest, and every button renders as flat
    ;# text two pixels wider than a label. clamx restates them; this is the
    ;# assertion that a re-vendored copy still does.
    ;#
    ;# `winfo reqwidth` on a real button, NOT `ttk::style lookup`: lookup does
    ;# not traverse theme parents, so it cannot tell an unset option from an
    ;# inherited one, and it reported the same emptiness for both while clam
    ;# itself rendered correctly. The widget is the only honest witness.
    ;#
    ;# Deliberately separate from the zero-grey sweep: nothing about this
    ;# class of breakage is visible in colour terms, so the sweep passed
    ;# straight through it.
    ttk::button .geomprobe -text "Refresh"
    foreach t {clam clamx} {
        ttk::setTheme $t
        update idletasks
        puts $::LOG "geometry.$t [winfo reqwidth .geomprobe]x[winfo reqheight .geomprobe]"
    }
    puts $::LOG "geometry.relief '[ttk::style lookup TButton -relief]'"
    destroy .geomprobe

    ;# What Tk read from the root window on its own, before anything of ours
    ;# ran. On a desktop publishing a palette these are the desktop's; on a
    ;# bare machine they are the SCHEMA fallbacks.
    puts $::LOG "boot.background [dict get $::xres::SYSTEM background]"
    puts $::LOG "boot.omarchyMode '[dict get $::xres::SYSTEM omarchyMode]'"
    puts $::LOG "boot.is_dark [xres::is_dark $::xres::SYSTEM]"
    puts $::LOG "floored $::FLOORED"

    ;# The floor's whole safety rests on its PRIORITY, which is invisible
    ;# until it is wrong: at startupFile (40) the root window's own entries
    ;# must still outrank it, while a light/dark override at interactive (80)
    ;# outranks both. Proven here rather than asserted in a comment, because
    ;# getting it backwards would silently pin every machine to Mocha.
    ;# Skipped where the root window publishes nothing -- there is then no
    ;# desktop value for the floor to lose to, which is the point of it.
    set desktop_bg [option get . background Background]
    if {$desktop_bg ne "" && !$::FLOORED} {
        option readfile [xres::fragment dark] startupFile
        puts $::LOG "floor.loses_to_desktop [expr {
            [option get . background Background] eq $desktop_bg}]"
        option readfile [xres::fragment light] interactive
        puts $::LOG "override.beats_floor [expr {
            [option get . background Background] ne $desktop_bg}]"
        ;# put the desktop's own answer back before the modes are probed
        if {[file readable [xres::live_fragment]]} {
            option readfile [xres::live_fragment] interactive
        }
    }

    ;# The overrides: checked in, so exact values are asserted. They must
    ;# also survive being applied in either order -- an override cannot be
    ;# removed from Tk's option database, only outranked by a later one.
    probe_mode dark
    probe_mode light
    probe_mode dark
    probe_mode system

    puts $::LOG "DONE"
    close $::LOG
    exit 0
}

after 900 report
