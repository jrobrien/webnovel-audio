# clamx -- clam, with its palette taken from the X resource database.
#
# clam's geometry and element layout, its colours from the desktop. A new theme
# rather than a change to clam: naming `clam` is an explicit request for clam's
# appearance, and repainting that from the desktop palette would override a
# decision the application author already made. `clamx` has to be asked for.
#
#   source clamx.tcl
#   ttk::setTheme clamx        ;# not `ttk::style theme use` -- see below
#
# ---------------------------------------------------------------------------
# THE API IS THE X RESOURCE DATABASE
# ---------------------------------------------------------------------------
#
# These eight resources are the entire input. Nothing else is read, and no
# desktop environment, distribution or palette generator is assumed: set these
# by any means you like -- xrdb, a RESOURCE_MANAGER property, ~/.Xdefaults, or
# `option add` before sourcing this file -- and clamx follows.
#
#   RESOURCE              CLASS               DRIVES
#   background            Background          window and widget ground
#   foreground            Foreground          text, caret, arrows
#   windowColor           Background          entry/treeview/text field ground
#   selectBackground      SelectBackground    selected rows and text
#   selectForeground      SelectForeground    text on a selection
#   disabledForeground    DisabledForeground  insensitive text
#   troughColor           TroughColor         scale/scrollbar troughs, tabs
#   activeBackground      ActiveBackground    hover
#
# They are the same eight Tk 9's own built-in `default` theme reads, so a
# display set up for one is set up for the other, and clamx gives Tk 8.6 the
# appearance Tk 9 gets for free.
#
# HOW MANY YOU SUPPLY MATTERS:
#
#   none of the eight   plain clam, unchanged. A machine with no resources is
#                       entitled to look like an unthemed machine, and clam's
#                       own palette is at least self-consistent.
#   some of the eight   the rest are DERIVED from background and foreground.
#                       A half-supplied palette is worse than none -- four
#                       values arriving dark while four default light gives
#                       white entry fields carrying pale text -- so clamx
#                       never mixes supplied values with clam's literals.
#   all eight           used as given.
#
# Supplying all eight is strongly preferred: derivation keeps a window
# coherent but cannot guess intent, and a derived selection colour in
# particular is a desaturated blend rather than an accent.
#
# No images and no new elements: -parent clam inherits clam's element
# definitions and layouts.
#
# It does NOT inherit clam's `theme settings` -- everything clam configures for
# itself, colour and geometry alike, is per-theme and stops at the parent
# boundary. A bare `ttk::style theme create X -parent clam` gives a ttk::button
# 52x22 against clam's 93x32, two pixels wider than a plain label: no relief,
# no padding, no width floor. So this file restates clam's geometry as well as
# supplying colour. Geometry is copied verbatim from clamTheme.tcl and should
# change only if clam's does.
#
# Installing it in an application: vendor it and `source` it, or drop it in a
# directory with a pkgIndex.tcl and `package require ttk::theme::clamx`. A
# package index does NOT make it load on its own -- something still has to ask
# for it. See docs/consuming.md.

package require Tk 8.6-

# Select it with `ttk::setTheme clamx`, not `ttk::style theme use clamx`. Both
# change the theme, but only setTheme updates ::ttk::currentTheme -- and that
# variable is what a Python host reads back: tkinter's Style().theme_use() with
# no argument returns $ttk::currentTheme, so after a bare `theme use` a Python
# application is told the theme is still `default`. (Python's own
# Style().theme_use("clamx") calls setTheme internally and is fine.)
#
# Version: bumped on every change that alters what this file produces, so a
# vendored copy can be compared against the original:
#     package require ttk::theme::clamx        ;# -> 1.3
#     set ttk::theme::clamx::version           ;# -> 1.3

namespace eval ttk::theme::clamx {
    variable version 1.3
    variable colors

    # --- helpers ----------------------------------------------------------
    proc Res {name class fallback} {
        set v [option get . $name $class]
        return [expr {$v eq "" ? $fallback : $v}]
    }

    # Blend two #rrggbb colours. The derived shades come from mixing the
    # background toward the FOREGROUND rather than toward white or black: the
    # foreground is the one colour guaranteed to contrast the background, so a
    # border derived from it stays visible in a light theme and a dark one
    # without branching on which we are in.
    proc Mix {a b frac} {
        scan [string range $a 1 end] %2x%2x%2x ar ag ab
        scan [string range $b 1 end] %2x%2x%2x br bg_ bb
        return [format "#%02x%02x%02x" \
            [expr {int($ar + ($br - $ar) * $frac + 0.5)}] \
            [expr {int($ag + ($bg_ - $ag) * $frac + 0.5)}] \
            [expr {int($ab + ($bb - $ab) * $frac + 0.5)}]]
    }

    # The eight structural resources, with the class each is looked up under.
    variable STRUCTURAL {
        background Background
        foreground Foreground
        windowColor Background
        selectBackground SelectBackground
        selectForeground SelectForeground
        disabledForeground DisabledForeground
        troughColor TroughColor
        activeBackground ActiveBackground
    }

    # --- palette ----------------------------------------------------------
    # Re-read the resources and restate every colour. Safe to call again after
    # an `option add` override: Tk reads the database once at startup, so an
    # application that changes theme at runtime must drive this itself.
    #
    # Three cases, because a HALF-supplied palette is worse than none. Each
    # resource falling back independently to clam's own literal is coherent
    # when nothing is set and when everything is, and incoherent in between:
    # four values arriving dark and four defaulting light produces white entry
    # fields with pale text on them.
    #
    #   nothing supplied -> clam's own palette, unchanged. Self-consistent.
    #   some supplied    -> derive the rest from background and foreground.
    #   all supplied     -> use them.
    proc refresh {} {
        variable colors
        variable STRUCTURAL

        array set have {}
        foreach {res class} $STRUCTURAL {
            set v [option get . $res $class]
            if {$v ne ""} { set have($res) $v }
        }
        set supplied [array size have]

        if {$supplied == 0} {
            # Plain clam. Its palette is internally consistent, and a machine
            # with no resources is entitled to look like an unthemed machine.
            set bg #dcdad5; set fg #000000; set field #ffffff
            set sel #4a6984; set selfg #ffffff; set dim #999999
            set trough [Mix $bg $fg 0.12]; set active [Mix $bg $fg 0.08]
        } else {
            # Derived shades use the same ratios themed/tk.Xresources.tpl
            # uses, so an inferred palette and a generated one agree.
            set bg [expr {[info exists have(background)] ? $have(background) : "#dcdad5"}]
            set fg [expr {[info exists have(foreground)] ? $have(foreground) : "#000000"}]
            # Selection is the weak one: mixing background toward foreground
            # cannot invent a hue, so a derived selection is a desaturated
            # blend rather than an accent. That is a reason to supply
            # selectBackground, not a reason for this file to go looking for
            # a colour under some other name.
            set D(windowColor)        [Mix $bg $fg 0.06]
            set D(selectBackground)   [Mix $bg $fg 0.25]
            set D(selectForeground)   $fg
            set D(disabledForeground) [Mix $bg $fg 0.50]
            set D(troughColor)        [Mix $bg $fg 0.12]
            set D(activeBackground)   [Mix $bg $fg 0.16]
            foreach n {windowColor selectBackground selectForeground
                       disabledForeground troughColor activeBackground} {
                set V($n) [expr {[info exists have($n)] ? $have($n) : $D($n)}]
            }
            set field  $V(windowColor);        set sel    $V(selectBackground)
            set selfg  $V(selectForeground);   set dim    $V(disabledForeground)
            set trough $V(troughColor);        set active $V(activeBackground)
        }

        array set colors [list \
            -bg $bg -fg $fg -field $field -sel $sel -selfg $selfg \
            -dim $dim -trough $trough -active $active \
            -border [Mix $bg $fg 0.30] \
            -bevel  [Mix $bg $fg 0.06] \
            -supplied $supplied]

        if {$supplied > 0} { FillClassic }
        Apply
    }

    # ttk::style cannot reach a classic widget's caret, selection, disabled
    # box or trough: those come from the option database. Without them a
    # partially themed desktop leaves a black caret on a dark field and a
    # light grey selection, which no amount of ttk work fixes.
    #
    # Priority 19 is deliberate and is the whole reason this is safe. It sits
    # just below widgetDefault(20), so:
    #
    #   - it beats Tk's compiled-in defaults, which is the point;
    #   - it loses to anything the desktop put in RESOURCE_MANAGER, which
    #     lands at userDefault(60), so a supplied value always wins;
    #   - it loses to the application's own `option add` at any standard
    #     priority, whether that runs before or after this file -- at equal
    #     priority the later call would win, and being one below removes the
    #     ordering question entirely;
    #   - an explicit per-widget option beats the database outright, at any
    #     priority, so an application that colours its own widgets is
    #     untouched.
    #
    # The database is per-interpreter and in memory. Nothing here reaches the
    # X server or any other process.
    proc FillClassic {} {
        variable colors
        set bg $colors(-bg);      set fg     $colors(-fg)
        set field $colors(-field); set sel   $colors(-sel)
        set selfg $colors(-selfg); set dim   $colors(-dim)
        set trough $colors(-trough); set active $colors(-active)

        foreach {pat val} [list \
            *selectBackground           $sel \
            *selectForeground           $selfg \
            *insertBackground           $fg \
            *troughColor                $trough \
            *activeBackground           $active \
            *activeForeground           $fg \
            *highlightBackground        $bg \
            *highlightColor             $sel \
            *disabledForeground         $dim \
            *selectColor                $field \
            *Text.background            $field \
            *Entry.background           $field \
            *Listbox.background         $field \
            *Spinbox.background         $field \
            *Entry.disabledBackground   $bg \
            *Entry.readonlyBackground   $bg \
            *Spinbox.readonlyBackground $bg \
            *Spinbox.buttonBackground   $trough \
        ] { option add $pat $val 19 }
    }

    proc Apply {} {
        variable colors
        set bg     $colors(-bg);      set fg     $colors(-fg)
        set field  $colors(-field);   set sel    $colors(-sel)
        set selfg  $colors(-selfg);   set dim    $colors(-dim)
        set trough $colors(-trough);  set active $colors(-active)
        set border $colors(-border);  set bevel  $colors(-bevel)

        ttk::style theme settings clamx {
            # --- clam's geometry, restated -------------------------------
            # `-parent clam` carries elements and layouts, not settings, so
            # none of this arrives on its own. Values are clam's own, from
            # clamTheme.tcl. Without them buttons render as flat text.
            ttk::style configure . -font TkDefaultFont
            ttk::style configure TButton \
                -relief raised -padding 5 -width -11 -anchor center
            ttk::style configure TMenubutton -relief raised -padding 5 -width -11
            ttk::style configure Toolbutton -relief flat -padding 2 -anchor center
            foreach s {TCheckbutton TRadiobutton} {
                ttk::style configure $s -padding 2 -indicatormargin {1 1 4 1}
            }
            ttk::style configure TEntry -padding 1 -insertwidth 1
            ttk::style configure TCombobox -padding 1 -insertwidth 1
            ttk::style configure TSpinbox -padding {2 0 10 0} -arrowsize 10
            ttk::style configure TNotebook.Tab -padding {6 2 6 2}
            ttk::style configure Heading -relief raised -padding 3 -font TkHeadingFont
            ttk::style configure TLabelframe \
                -relief raised -borderwidth 2 -labelmargins {0 0 0 4}
            ttk::style configure Sash -gripcount 10 -sashthickness 6

            # --- colour ---------------------------------------------------
            # clam's bevels come from a light-to-dark ramp that no X resource
            # supplies. Rather than fake a 3D ramp, keep the surfaces flat and
            # spend the contrast on a border derived from the foreground.
            ttk::style configure . \
                -background $bg -foreground $fg -fieldbackground $field \
                -troughcolor $trough -selectbackground $sel \
                -selectforeground $selfg -insertcolor $fg \
                -bordercolor $border -lightcolor $bevel -darkcolor $bevel

            # Every state map in clam bakes clam's own greys in at theme
            # definition time, and `configure` does not touch them. Restate.
            ttk::style map . \
                -background [list disabled $bg active $active] \
                -foreground [list disabled $dim] \
                -selectbackground [list !focus $trough] \
                -selectforeground [list !focus $fg]

            foreach s {TButton Toolbutton TMenubutton} {
                ttk::style configure $s -background $bg
                ttk::style map $s \
                    -background [list disabled $bg pressed $sel active $active] \
                    -lightcolor [list pressed $sel] \
                    -darkcolor  [list pressed $sel]
            }

            # clam never sets a base -arrowcolor, only a disabled map, so the
            # combobox and spinbox arrows fall back to the element's compiled
            # black -- fine on clam's light grey, invisible on a dark one. Tk 9's
            # `default` theme sets -arrowcolor to the text colour for exactly
            # this reason; do the same.
            ttk::style configure . -arrowcolor $fg
            ttk::style configure TScrollbar -arrowcolor $fg -troughcolor $trough
            ttk::style map TScrollbar \
                -background [list disabled $bg active $active] \
                -arrowcolor [list disabled $dim]

            foreach s {TEntry TSpinbox TCombobox} {
                ttk::style configure $s -fieldbackground $field -foreground $fg \
                    -arrowcolor $fg
                ttk::style map $s \
                    -background      [list readonly $bg active $active pressed $active] \
                    -fieldbackground [list {readonly focus} $sel readonly $field disabled $bg] \
                    -foreground      [list {readonly focus} $selfg disabled $dim] \
                    -bordercolor     [list focus $sel] \
                    -arrowcolor      [list disabled $dim]
            }

            # Check and radio indicators are hardcoded #ffffff/#000000 in clam,
            # as a configure and again in a state map. Both need restating or
            # the tick boxes stay white squares on a dark window.
            foreach s {TCheckbutton TRadiobutton} {
                ttk::style configure $s \
                    -indicatorbackground $field -indicatorforeground $fg
                ttk::style map $s \
                    -indicatorbackground [list \
                        pressed $active \
                        {alternate disabled} $dim \
                        alternate $sel \
                        disabled $bg] \
                    -indicatorforeground [list disabled $dim]
            }

            ttk::style configure Treeview -background $field -fieldbackground $field
            ttk::style map Treeview \
                -background  [list disabled $bg selected $sel] \
                -foreground  [list disabled $dim selected $selfg] \
                -bordercolor [list focus $sel]
            ttk::style configure Heading -background $trough -foreground $fg
            ttk::style map Heading -background [list active $active]

            ttk::style configure TNotebook -background $bg
            ttk::style map TNotebook.Tab \
                -background [list selected $bg {} $trough] \
                -lightcolor [list selected $bevel {} $trough]

            ttk::style configure TLabelframe -bordercolor $border
            ttk::style configure TProgressbar -background $sel
            ttk::style configure TScale -background $bg
            ttk::style configure TSeparator -background $border
        }
    }
}

# Creating a theme twice is an error, and an application can reach this file
# more than once -- two entry points, a reloaded module, a vendored copy beside
# a packaged one. Create it once, then always restate the palette, so a second
# source is a cheap refresh rather than a crash.
if {[lsearch -exact [ttk::style theme names] clamx] < 0} {
    ttk::style theme create clamx -parent clam
}
ttk::theme::clamx::refresh

package provide ttk::theme::clamx 1.3
