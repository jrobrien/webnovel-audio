# Minimal JSON -> Tcl parser. Objects become dicts, arrays become lists.
# Enough for webnovel-audio's own `--json` output; not a general validator.
# Usage:  set d [json::parse $text]   ;  set v [json::get $d key subkey ...]

namespace eval json {
    variable s "" ; variable i 0 ; variable n 0
}

proc json::parse {text} {
    variable s $text ; variable i 0 ; variable n [string length $text]
    set v [_value]
    _ws
    if {$i < $n} { error "json: trailing data at $i" }
    return $v
}

# nested get with a default of ""
proc json::get {d args} {
    foreach k $args {
        if {![dict exists $d $k]} { return "" }
        set d [dict get $d $k]
    }
    return $d
}

proc json::_ws {} {
    variable s ; variable i ; variable n
    while {$i < $n && [string first [string index $s $i] " \t\n\r"] >= 0} { incr i }
}

proc json::_value {} {
    variable s ; variable i ; variable n
    _ws
    if {$i >= $n} { error "json: unexpected end" }
    set c [string index $s $i]
    switch -- $c {
        "\{" { return [_object] }
        "\[" { return [_array] }
        "\"" { return [_string] }
        default {
            if {$c eq "-" || [string is digit $c]} { return [_number] }
            if {[string range $s $i $i+3] eq "true"}  { incr i 4 ; return 1 }
            if {[string range $s $i $i+4] eq "false"} { incr i 5 ; return 0 }
            if {[string range $s $i $i+3] eq "null"}  { incr i 4 ; return "" }
            error "json: unexpected char '$c' at $i"
        }
    }
}

proc json::_object {} {
    variable s ; variable i
    incr i  ;# consume opening brace
    set d [dict create]
    _ws
    if {[string index $s $i] eq "\}"} { incr i ; return $d }
    while {1} {
        _ws
        set key [_string]
        _ws
        if {[string index $s $i] ne ":"} { error "json: expected ':' at $i" }
        incr i
        dict set d $key [_value]
        _ws
        set c [string index $s $i] ; incr i
        if {$c eq "\}"} { return $d }
        if {$c ne ","} { error "json: expected ',' or '\}' at [expr {$i-1}]" }
    }
}

proc json::_array {} {
    variable s ; variable i
    incr i  ;# consume opening bracket
    set l {}
    _ws
    if {[string index $s $i] eq "\]"} { incr i ; return $l }
    while {1} {
        lappend l [_value]
        _ws
        set c [string index $s $i] ; incr i
        if {$c eq "\]"} { return $l }
        if {$c ne ","} { error "json: expected ',' or '\]' at [expr {$i-1}]" }
    }
}

proc json::_string {} {
    variable s ; variable i ; variable n
    if {[string index $s $i] ne "\""} { error "json: expected string at $i" }
    incr i
    set out ""
    while {$i < $n} {
        set c [string index $s $i] ; incr i
        if {$c eq "\""} { return $out }
        if {$c ne "\\"} { append out $c ; continue }
        set e [string index $s $i] ; incr i
        switch -- $e {
            "\"" { append out "\"" }
            "\\" { append out "\\" }
            "/"  { append out "/" }
            "b"  { append out "\b" }
            "f"  { append out "\f" }
            "n"  { append out "\n" }
            "r"  { append out "\r" }
            "t"  { append out "\t" }
            "u"  {
                set hex [string range $s $i $i+3] ; incr i 4
                append out [format %c [scan $hex %x]]
            }
            default { error "json: bad escape \\$e at [expr {$i-1}]" }
        }
    }
    error "json: unterminated string"
}

proc json::_number {} {
    variable s ; variable i ; variable n
    set start $i
    if {[string index $s $i] eq "-"} { incr i }
    while {$i < $n && [string first [string index $s $i] "0123456789.eE+-"] >= 0} { incr i }
    return [string range $s $start [expr {$i-1}]]
}

# parse newline-delimited JSON (one object per line) -> list of dicts
proc json::parse_lines {text} {
    set out {}
    foreach line [split $text "\n"] {
        set line [string trim $line]
        if {$line eq ""} continue
        if {[catch {parse $line} d]} continue
        lappend out $d
    }
    return $out
}
