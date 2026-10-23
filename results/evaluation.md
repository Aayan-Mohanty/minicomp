Planted-bug evaluation: up to 400 programs per bug and setting, seeds 0-399.

| Bug | Pass | Found (default) | Found (swap) | Status | Blamed | Reduced |
|---|---|---|---|---|---|---|
| `div-self-one` | constprop | program 17 | program 17 | mismatch | yes | 59 -> 9 lines |
| `fold-div-floor` | constprop | program 49 | program 49 | mismatch | yes | 55 -> 5 lines |
| `fold-shift-unmasked` | constprop | program 42 | program 42 | mismatch | yes | 14 -> 2 lines |
| `fold-unsigned-compare` | constprop | program 5 | program 5 | mismatch | yes | 13 -> 1 line |
| `meet-keep-first` | constprop | program 1 | program 1 | crash | yes | 27 -> 4 lines |
| `unreachable-false-branch` | unreachable | program 1 | program 1 | crash | yes | 27 -> 4 lines |
| `copyprop-no-kill` | copyprop | program 111 | program 111 | mismatch | yes | 62 -> 5 lines |
| `cse-no-kill` | cse | program 38 | program 38 | mismatch | yes | 69 -> 4 lines |
| `cse-sub-commutes` | cse | no (0/400) | program 242 | mismatch | no (passes together) | 193 -> 6 lines |
| `liveness-ignores-branch` | dce | program 1 | program 1 | crash | yes | 27 -> 4 lines |
| `liveness-no-backedge` | dce | program 1 | program 1 | crash | yes | 27 -> 4 lines |

- default: 10/11 bugs found; median programs to first detection: 11
- swap: 11/11 bugs found; median programs to first detection: 17
- either setting: 11/11 bugs found
- correct pass blamed: 10/11 detected bugs
- reduction: median 27 -> 4 lines
- false alarms on the correct compiler (default): 0/400 programs
- false alarms on the correct compiler (swap): 0/400 programs
- total time: 172 s

--- div-self-one: reduced program (59 -> 9 lines) ---
var k0 = 0;
while (k0 / k0 <= 0) {
    k0 = 1;
}
var f = k0;
if (f) {
} else {
    print 0;
}

--- fold-div-floor: reduced program (55 -> 5 lines) ---
var d = ~1;
if (1 / d) {
} else {
    print 0;
}

--- fold-shift-unmasked: reduced program (14 -> 2 lines) ---
var a = 1 << 255;
print a;

--- fold-unsigned-compare: reduced program (13 -> 1 line) ---
print 0 > -1;

--- meet-keep-first: reduced program (27 -> 4 lines) ---
var k0 = 0;
while (k0 < 1) {
    k0 = 1;
}

--- unreachable-false-branch: reduced program (27 -> 4 lines) ---
var k0 = 0;
while (k0) {
    k0 = 1;
}

--- copyprop-no-kill: reduced program (62 -> 5 lines) ---
var a = 1;
var b = 0;
b = a;
a = 0;
print b;

--- cse-no-kill: reduced program (69 -> 4 lines) ---
var c = 0;
c = c + c <= 0;
c = c + c;
print c;

--- cse-sub-commutes: reduced program (193 -> 6 lines) ---
var k3 = 0;
while (k3 < 1) {
    k3 = k3 + 1;
    print (0 > (0 || k3) ^ (0 != k3) < 1) - 1;
    print 1 - (0 > (0 || k3) ^ (0 != k3) < 1);
}

--- liveness-ignores-branch: reduced program (27 -> 4 lines) ---
var k0 = 0;
while (k0) {
    k0 = 1;
}

--- liveness-no-backedge: reduced program (27 -> 4 lines) ---
var k0 = 0;
while (k0 < 1) {
    k0 = 1;
}
