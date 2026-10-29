Planted-bug evaluation: up to 400 programs per bug and setting, seeds 0-399.

| Bug | Pass | Found (default) | Found (no-swap) | Found (no-functions) | Found (no-arrays) | Found (no-globals) | Found (no-recursion) | Found (no-loop-control) | Status | Blamed | Reduced |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `div-self-one` | constprop | program 23 | program 23 | program 29 | program 13 | program 5 | program 30 | program 1 | mismatch | yes | 97 -> 6 lines |
| `fold-div-floor` | constprop | program 63 | program 63 | program 300 | program 94 | program 28 | program 31 | program 63 | mismatch | yes | 109 -> 3 lines |
| `fold-shift-unmasked` | constprop | program 33 | program 33 | program 11 | program 17 | program 6 | program 19 | program 31 | mismatch | yes | 114 -> 6 lines |
| `fold-unsigned-compare` | constprop | program 26 | program 26 | program 7 | program 2 | program 15 | program 3 | program 15 | mismatch | yes | 57 -> 4 lines |
| `meet-keep-first` | constprop | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | mismatch | yes | 53 -> 7 lines |
| `unreachable-false-branch` | unreachable | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | crash | yes | 53 -> 4 lines |
| `call-result-undef` | constprop | program 21 | program 21 | no (0/400) | program 13 | program 19 | program 85 | program 21 | mismatch | yes | 71 -> 8 lines |
| `copyprop-no-kill` | copyprop | program 8 | program 8 | program 8 | program 17 | program 21 | program 8 | program 27 | mismatch | yes | 37 -> 12 lines |
| `copyprop-meet-union` | copyprop | program 2 | program 2 | program 1 | program 1 | program 9 | program 8 | program 1 | crash | yes | 92 -> 13 lines |
| `cse-no-kill` | cse | program 10 | program 10 | program 3 | program 23 | program 32 | program 10 | program 38 | mismatch | yes | 35 -> 5 lines |
| `cse-sub-commutes` | cse | program 73 | no (0/400) | program 227 | program 126 | program 98 | program 73 | program 118 | mismatch | yes | 73 -> 5 lines |
| `cse-meet-union` | cse | program 2 | program 2 | program 1 | program 1 | program 1 | program 2 | program 1 | crash | yes | 92 -> 8 lines |
| `cse-load-ignores-store` | cse | program 9 | program 9 | program 9 | no (0/400) | program 79 | program 9 | program 24 | mismatch | no (passes together) | 54 -> 4 lines |
| `call-keeps-globals` | constprop | program 18 | program 18 | no (0/400) | program 1 | no (0/400) | program 13 | program 2 | mismatch | yes | 97 -> 7 lines |
| `copyprop-call-keeps-globals` | copyprop | program 2 | program 2 | no (0/400) | program 13 | no (0/400) | program 13 | program 2 | mismatch | yes | 92 -> 14 lines |
| `liveness-ignores-branch` | dce | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | crash | yes | 53 -> 4 lines |
| `liveness-no-backedge` | dce | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | program 1 | crash | yes | 53 -> 4 lines |
| `liveness-return-ignores-globals` | dce | program 3 | program 3 | no (0/400) | program 15 | no (0/400) | program 4 | program 20 | mismatch | yes | 204 -> 13 lines |
| `dce-deletes-call` | dce | program 2 | program 2 | no (0/400) | program 5 | program 6 | program 15 | program 1 | mismatch | yes | 92 -> 5 lines |
| `regalloc-dead-def` | regalloc | program 7 | program 7 | no (0/400) | program 6 | program 18 | program 15 | program 11 | crash | yes | 132 -> 5 lines |
| `regalloc-entry-params` | regalloc | program 2 | program 2 | no (0/400) | program 1 | program 13 | program 2 | program 12 | mismatch | yes | 92 -> 5 lines |
| `regalloc-scratch-shared` | regalloc | program 20 | program 39 | program 76 | program 11 | program 244 | program 81 | program 14 | mismatch | yes | 103 -> 16 lines |

- default: 22/22 bugs found; median programs to first detection: 7.5
- no-swap: 21/22 bugs found; median programs to first detection: 7
- no-functions: 15/22 bugs found; median programs to first detection: 7
- no-arrays: 21/22 bugs found; median programs to first detection: 6
- no-globals: 19/22 bugs found; median programs to first detection: 13
- no-recursion: 22/22 bugs found; median programs to first detection: 9.5
- no-loop-control: 22/22 bugs found; median programs to first detection: 11.5
- any setting: 22/22 bugs found
- correct pass blamed: 21/22 detected bugs
- reduction: median 92 -> 5.5 lines
- false alarms on the correct compiler (default): 0/400 programs
- false alarms on the correct compiler (no-swap): 0/400 programs
- false alarms on the correct compiler (no-functions): 0/400 programs
- false alarms on the correct compiler (no-arrays): 0/400 programs
- false alarms on the correct compiler (no-globals): 0/400 programs
- false alarms on the correct compiler (no-recursion): 0/400 programs
- false alarms on the correct compiler (no-loop-control): 0/400 programs
- total time: 824 s

--- div-self-one: reduced program (97 -> 6 lines) ---
var k3 = 0;
while (k3 < 1) {
    k3 = 1;
}
var d = 1 != k3;
print d / d;

--- fold-div-floor: reduced program (109 -> 3 lines) ---
var g0 = 0;
g0 = -1 / 16;
print g0;

--- fold-shift-unmasked: reduced program (114 -> 6 lines) ---
fun f1() {
    if (35 >> -31) {
        print 0;
    }
}
print f1();

--- fold-unsigned-compare: reduced program (57 -> 4 lines) ---
var g0 = 1;
var f = -1;
g0 = 0 <= f;
print g0;

--- meet-keep-first: reduced program (53 -> 7 lines) ---
var g0 = 1;
var k2 = 0;
while (k2 < 1 && -1) {
    k2 = 1;
    g0 = 0;
}
print g0;

--- unreachable-false-branch: reduced program (53 -> 4 lines) ---
fun f1() {
    for (var k1 = 0; k1; k1 = 1) {
    }
}

--- call-result-undef: reduced program (71 -> 8 lines) ---
fun f0(n) {
    if (n < 1) {
        return 1;
    }
    print 1 && f0(0);
}
while (f0(1)) {
}

--- copyprop-no-kill: reduced program (37 -> 12 lines) ---
var g0 = 0;
var f = 0;
g0 = f;
f = 1;
var k2 = 0;
while (k2 < 1) {
    k2 = 1;
    if (g0) {
        continue;
    }
    print 0;
}

--- copyprop-meet-union: reduced program (92 -> 13 lines) ---
fun f0(p) {
}
fun f1(p) {
    var e = 0;
    var k0 = 0;
    while (k0 < 1) {
        k0 = 1;
        e = f0(p);
        p = 0 || 0 / g0;
    }
}
var g0 = 0;
g0 = f1(0);

--- cse-no-kill: reduced program (35 -> 5 lines) ---
for (var k0 = 0; k0 < 5; k0 = k0 + 1) {
    var g0 = k0;
    g0 = 1 ^ 3 <= g0;
    print 3 <= g0;
}

--- cse-sub-commutes: reduced program (73 -> 5 lines) ---
fun f0(q) {
    var d = 1073741824 - q;
    print q - 1073741824;
}
var a = f0(0);

--- cse-meet-union: reduced program (92 -> 8 lines) ---
fun f2(p, q) {
    if (g0 || q + p) {
    }
    if (q + p) {
    }
}
var g0 = 1;
g0 = f2(0, 0);

--- cse-load-ignores-store: reduced program (54 -> 4 lines) ---
var ga[8];
ga[0] = ga[0];
ga[0] = 1;
print ga[0];

--- call-keeps-globals: reduced program (97 -> 7 lines) ---
fun f0() {
    g0 = 0;
}
var g0 = 1;
var g1 = 0;
g1 = f0();
print g0;

--- copyprop-call-keeps-globals: reduced program (92 -> 14 lines) ---
fun f0() {
    g0 = 0;
}
fun f1(p) {
    print p;
}
fun f2() {
    g0 = !ga[0];
    var d = g0;
    d = f1(f0() ^ d);
}
var g0 = 0;
var ga[4];
g0 = f2();

--- liveness-ignores-branch: reduced program (53 -> 4 lines) ---
var k2 = 0;
while (k2) {
    k2 = 1;
}

--- liveness-no-backedge: reduced program (53 -> 4 lines) ---
var k2 = 0;
while (k2 < 1) {
    k2 = 1;
}

--- liveness-return-ignores-globals: reduced program (204 -> 13 lines) ---
fun f0(n) {
    if (n < 1) {
        return 0;
    }
    if (f0(n - 1) ^ g0) {
        g0 = 0;
    } else if (1) {
        print 0;
    } else {
    }
}
var g0 = 1;
var d = f0(3);

--- dce-deletes-call: reduced program (92 -> 5 lines) ---
fun f2() {
    print 0;
}
var g0 = 0;
g0 = f2();

--- regalloc-dead-def: reduced program (132 -> 5 lines) ---
fun f1() {
}
for (var k6 = 0; k6 < 2; k6 = k6 + 1) {
    var g = f1();
}

--- regalloc-entry-params: reduced program (92 -> 5 lines) ---
fun f1(p, q) {
    print p;
}
var g0 = 0;
g0 = f1(1, 0);

--- regalloc-scratch-shared: reduced program (103 -> 16 lines) ---
fun f0(p, q) {
}
fun f1(p, q, r) {
    if ((8 >> p ^ g0 & q) >= 0) {
    } else {
        f0(0, 0 < r > !p);
        print 0;
    }
    if (r + r) {
        var d = f0(8 >> p ^ q, 0);
        if (p) {
        }
    }
}
var g0 = 1;
g0 = f1(0, -1, 0);
