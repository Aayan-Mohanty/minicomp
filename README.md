# MiniComp: a self-testing optimizing compiler

An optimizing compiler for a small language (Mini) with functions, arrays,
global variables and `for`/`break`/`continue`, together with a differential fuzzer that generates random programs, runs each one
nine ways (an interpreter, the compiled code at several optimization
settings, and register-allocated machine code), finds miscompilations, and
automatically shrinks each failing program to a minimal example.

## Problem statement

Optimizing compilers rewrite programs to make them faster. If an
optimization is wrong, the compiled program silently computes a different
result from what the source code says: a *miscompilation*. These bugs are
hard to find because the source code is correct, the compiler reports no
error, and the wrong result may appear only for particular programs.

Writing test programs by hand covers only the cases the tester thinks of.
Random differential testing instead generates large numbers of valid
programs automatically and runs each one in several ways. Since every Mini
program has exactly one correct output, any difference means the compiler
has a bug. Because random failing programs are large, a reducer then
removes everything not needed to trigger the bug, leaving a small example a
developer can understand.

## Objectives

1. Define a small language with functions, arrays and globals, and **fully specified semantics** (no undefined behaviour).
2. Build its compiler: lexer, recursive-descent parser, semantic analysis,
   three-address code (TAC), control-flow graphs, global data-flow
   optimizations, and register allocation for a register machine.
3. Build a random generator of valid, always-terminating Mini programs.
4. Detect miscompilations by comparing the reference interpreter with every compiled version.
5. Automatically reduce failing programs to minimal examples.
6. Evaluate how many deliberately injected compiler bugs the fuzzer finds, and how quickly.

## Usage

```
python3 -m minicomp tokens  examples/gcd.mini     # lexer output
python3 -m minicomp ast     examples/gcd.mini     # syntax tree
python3 -m minicomp symbols examples/arrays.mini  # symbol table (variables, arrays, parameters, functions, globals used)
python3 -m minicomp fmt     examples/fib.mini     # formatted source
python3 -m minicomp run     examples/functions.mini  # run (reference interpreter)
python3 -m minicomp run     examples/arrays.mini  # arrays, for/break/continue, globals, recursion
python3 -m minicomp tac     examples/gcd.mini     # three-address code
python3 -m minicomp tac     examples/gcd.mini -O1 # optimized three-address code
python3 -m minicomp cfg     examples/functions.mini -O1  # basic blocks and CFG of each function
python3 -m minicomp vm      examples/fib.mini -O1 # compile and run on the TAC virtual machine
python3 -m minicomp asm     examples/gcd.mini     # -O1, then register allocation: machine code
python3 -m minicomp asm     examples/gcd.mini --registers 2   # with fewer registers (spills to memory)
python3 -m minicomp compare examples/functions.mini  # all 9 runs, sizes, verdict
python3 -m minicomp tac FILE --passes cse,dce     # run only some optimization passes
python3 -m minicomp generate --seed 3             # print the random program for seed 3
python3 -m minicomp generate --seed 3 --no-functions   # generator without functions
python3 -m minicomp generate --seed 3 --no-arrays --no-globals --no-recursion --no-loop-control
python3 -m minicomp fuzz -n 500                   # differential testing campaign
python3 -m minicomp fuzz -n 500 --seed 1000 --out failures --stop-after 5
python3 -m minicomp bugs                          # list the 22 planted compiler bugs
python3 -m minicomp fuzz -n 200 --bug regalloc-entry-params --stop-after 1   # hunt a planted bug
python3 -m minicomp reduce --seed 1 --bug regalloc-entry-params              # shrink its failing program
python3 -m minicomp compare FILE --bug fold-div-floor              # 9 runs with a bug planted
python3 -m minicomp evaluate -n 400 --out results/evaluation.json  # full evaluation (824 s in our run)
python3 -m pytest                                 # tests
```

No third-party packages are needed except pytest for the tests.

## Web app: MiniComp Console

```
python3 -m webapp.server          # then open http://127.0.0.1:8000
```

An IDE-style console. Cyan marks the reference path, amber the compiled path.

- **Pipeline bar**: every stage (lex, parse, sema, ref, tac, cfg, opt, vm, regs, diff)
  with its time in milliseconds and a count (tokens, nodes, instructions, spills, agreement).
- **diff**: the 9 runs side by side; values that differ from the reference are underlined in red.
- **front end**: tokens by kind, the syntax tree, the symbol table (variables, arrays with their sizes, parameters, functions).
- **tac**: three-address code at -O0 and -O1, and the machine code after register
  allocation with each function's variables, registers used and spills.
- **opt trace**: step through the optimizer pass by pass (arrow keys), with the
  lines each pass added and removed and a code-size bar per step.
- **cfg**: the control-flow graph of each function at -O0 or -O1.
- **fuzz**: a live campaign of up to 500 programs streamed from the server; each
  square is one program (green agree, red bug, grey did not finish); click one to open it.
- **console**: a timestamped log of every request with its server time and request id.

Shortcuts: Ctrl/Cmd+Enter compile, Alt+R random program, Alt+1..6 views.

### API (v1)

| Method and path | Purpose |
|---|---|
| `GET /api/v1/health` | Liveness, version, uptime |
| `GET /api/v1/meta` | Passes, registers, limits, language rules |
| `GET /api/v1/examples` | The programs in `examples/` |
| `POST /api/v1/compile` `{"code"}` | Every stage with timings, optimizer trace, CFGs per function, machine code, 9-way verdict |
| `POST /api/v1/generate` `{"seed"}` | The random program for a seed |
| `GET /api/v1/fuzz/stream?n=&seed=` | Server-Sent Events: one `program` event per program, then `done` (n <= 500) |
| `POST /api/v1/fuzz` `{"n","seed"}` | The same campaign as one reply, `{"ok", "summary", "results"}` (n <= 100); the page falls back to it if streaming fails |

Standard library only. Each response has an `X-Request-Id` header and each request
is logged as one JSON line. Clients are rate-limited per IP address (token bucket;
too many requests get HTTP 429). Programs run only inside the Mini interpreter, VM
and register machine, with step limits; bodies are limited to 50 KB and programs to
20,000 characters.

### Deploying on Render

Build command `pip install -r requirements.txt`; start command
`python -m webapp.server --host 0.0.0.0` (the port comes from `PORT`);
environment variable `PYTHON_VERSION` = `3.13.5`.

## The Mini language

```
fun add(a, b) {        // functions: top level only, called from anywhere (also recursively)
    return a + b;      // `return` needs a value; falling off the end returns 0
}
var x = 10;            // declaration (an initial value is required)
var t[8];              // array of 8 integers, all 0
x = x * add(2, 3);     // assignment; calls are expressions
t[x] = t[x - 1] + 1;   // element write and read; the index wraps (x mod 8)
add(1, 2);             // a call as a statement
print x;               // output
if (x > 5) { ... } else if (x == 0) { ... } else { ... }
while (x > 0) { ... }
for (var i = 0; i < 10; i = i + 1) { ... }   // each of the three parts may be left out
break;  continue;      // inside a while or for loop only
{ ... }                // block with its own scope
```

Operators, loosest to tightest:
`||`, `&&`, `|`, `^`, `&`, `== !=`, `< <= > >=`, `<< >>`, `+ -`, `* / %`,
unary `- ! ~`. All binary operators are left-associative. Comments start with `//`.

### Semantics (every program has exactly one correct output)

| Rule | Definition |
|---|---|
| Integers | 32-bit two's complement; every result wraps |
| `a / b` | truncates toward zero; `a / 0 == 0` |
| `a % b` | `a - (a / b) * b`, so `a % 0 == a` |
| `a << b`, `a >> b` | shift amount is `b & 31`; `>>` keeps the sign |
| Comparisons, `&&`, `\|\|`, `!` | result is 1 or 0; any non-zero value is true |
| `&&`, `\|\|` | the right operand is evaluated only when needed |
| Evaluation order | left to right: operands, then call arguments; in `a[i] = v;` the index, then the value |
| Scope | a block's variables end with the block; inner blocks may shadow |
| `var x = e;` | `e` is evaluated before `x` exists, so it refers to an outer `x` |
| Arrays | `var a[n];` (1 <= n <= 1024) creates n zeros; `a[i]` is element `i mod n` with the remainder in 0..n-1, so every index is valid (`a[-1]` is the last element). Arrays are used only through elements (no whole-array assignment or array arguments) |
| Loops | `break` leaves the innermost loop; `continue` goes to its condition (`while`) or its update (`for`); a `for` header's variable belongs to the loop |
| Functions | see their parameters, their own variables, and the program's top-level variables and arrays (globals); a local hides a global of the same name |
| Globals | every top-level variable and array exists, holding 0, from the start of the program; its declaration then sets its value |

Real languages such as C leave overflow, division by zero, large shifts and
out-of-bounds array accesses *undefined*, which makes a generated program's
correct output unknowable. Defining every case is what lets differential
testing treat any output difference as a compiler bug.

Resource limits: programs that run more than a fixed number of steps, or nest
calls more than 200 deep, are stopped; such programs are excluded from
comparisons. (Generated programs never hit either limit.)

## Code

| File | Phase |
|---|---|
| `minicomp/lexer.py` | Lexical analysis (maximal munch, positions, lexical errors) |
| `minicomp/parser.py` | Syntax analysis (recursive descent over a table of precedence levels) |
| `minicomp/nodes.py` | AST node types |
| `minicomp/semantic.py` | Semantic analysis, scoped symbol table, array, loop and function rules |
| `minicomp/ops.py` | Exact operator semantics, used by the optimizer, TAC VM and register machine |
| `minicomp/interp.py` | Reference interpreter, with its own independent implementation of the arithmetic |
| `minicomp/printer.py` | AST -> source (used by the generator and reducer) |
| `minicomp/tac.py` | Three-address code instructions, functions as units |
| `minicomp/lower.py` | Intermediate code generation (AST -> TAC); unique names for shadowed variables; globals as `@x` |
| `minicomp/vm.py` | Virtual machine that runs TAC (with a call stack) and counts executed instructions |
| `minicomp/cfg.py` | Basic blocks (leaders), control-flow graph per function, linearization back to TAC |
| `minicomp/opt.py` | Optimizer: passes listed below, repeated on each function until it stops changing |
| `minicomp/regalloc.py` | Register allocation by graph coloring; the register machine |
| `minicomp/generator.py` | Random generator of valid, always-terminating programs with functions |
| `minicomp/harness.py` | Differential testing: runs all 9 versions, compares, blames, runs campaigns |
| `minicomp/reduce.py` | Test-case reducer: shrinks a failing program while the same failure still shows |
| `minicomp/bugs.py` | 22 planted compiler bugs, switched on only inside `with planted(name):` |
| `minicomp/evaluate.py` | Planted-bug evaluation: detection, programs needed, blame, reduction, false alarms |
| `results/evaluation.md`, `results/evaluation.json` | Output of the full evaluation run |
| `webapp/server.py`, `webapp/static/index.html` | Web backend and page |

## Optimizations

Each pass runs on one function (or main) at a time and works across all of its blocks.

| Pass | Technique |
|---|---|
| `constprop` | Constant propagation by forward data-flow analysis (worklist; lattice UNDEF > constant > NAC; meet at joins), then constant folding, algebraic identities, and replacing branches on constants with gotos. Parameters, call results, array reads and globals on entry are NAC. |
| `unreachable` | Removes blocks not reachable from the entry |
| `copyprop` | Available-copies analysis (forward, intersection at joins): after `x = y`, a use of `x` reads `y` wherever the copy holds on every path |
| `cse` | Available-expressions analysis (forward, intersection at joins): `d = a op b` (or an array read `d = a[i]`) becomes `d = h` where `h` holds the same value on every path (operands of commutative operators are normalized; a store to the array ends a remembered read) |
| `dce` | Deletes assignments and array reads whose result is never read, using liveness analysis (backward data-flow); calls, prints, branches, array stores and array creations are never deleted |

Algebraic identities are used only if they hold for every 32-bit value.
For example `x - x -> 0` is used, but `x / x -> 1` is not, because in
Mini `0 / 0 == 0`. A call may print, so calls are never deleted, merged or
folded. A call can change globals (`@x`: the top-level variables and arrays
that functions use) but no other variable of its caller, so at a call every
fact about a global is dropped (its constant becomes NAC; copies,
expressions and array reads that mention it are forgotten), and liveness
treats a call, and a function's `return`, as reading every global.

## Register allocation

`regalloc.py` maps every variable of a function to one of 5 registers
(`r0`-`r4`) or, when that is impossible, to a memory slot (`[s0]`, ...):
liveness analysis, an interference graph (at every definition of `d`, an
edge to every variable live after it, except the source of a copy; all
parameters interfere with each other), Chaitin's simplify/select with
Briggs' optimistic coloring, then a rewrite in which spilled operands go
through two scratch registers `t0` and `t1`. Globals never get registers
(a callee may change them): they are read and written in memory like spilled
variables. Arrays live in memory; only their indexes and values use
registers. The register machine gives each call a fresh register file,
slots and local arrays, and writes the arguments into the callee's
parameter locations.

## Independent reference arithmetic

The interpreter does not use `ops.py`: it implements the language rules a
second time, a different way (`ctypes.c_int32` wrap-around, floor division
corrected toward zero, `% 32` for shift amounts). A mistake in either
implementation shows up as a difference instead of being shared by the
reference and the compiler. A unit test also compares the two directly.

## Random program generator

Each generated program is guaranteed to be valid (the generator tracks
scopes exactly like semantic analysis), to terminate, and to finish within
50,000 interpreter steps (a slower candidate is replaced by the next
candidate for the same seed). Termination: every `while` and `for` loop has
its own counter, incremented first in a `while` body or in the `for`
header and never assigned elsewhere (`break` and `continue` only make a loop
shorter); a function calls only earlier functions, and itself only if it is
recursive, in which case its first parameter `n` is a depth that is never
assigned, its body starts with `if (n < 1) { return ...; }`, every recursive
call passes `n - 1` and every other call passes a constant from 0 to 3.

Programs start with globals `g0`, `g1` and possibly an array `ga`, then 0 to
3 functions that read and write them, and end by printing every top-level
variable and every element of every top-level array, so wrong values show
up in the output. Expressions are biased toward what stresses a compiler:
edge-case constants, `x op x` patterns, repeated subexpressions and array
reads (a repeated binary expression is written with its operands swapped 30%
of the time), array indexes outside the array (they wrap), constant branch
conditions, calls, and shadowed variables. The same seed always gives the
same program. `--no-swap`, `--no-functions`, `--no-arrays`, `--no-globals`,
`--no-recursion` and `--no-loop-control` (or the matching `GenConfig`
fields) turn those features off.

## Differential testing harness

Each program is run as: the reference interpreter, unoptimized TAC (`O0`),
fully optimized TAC (`O1`), TAC optimized by each pass alone, and the `O1`
code after register allocation on the register machine (`machine`). Any
output that differs from the reference, or any crash, is a failure; passes
that fail on their own are reported as suspects, and the register allocator
is the suspect when only `machine` fails. Compiled code may run at most 20
times the reference's steps plus 1,000 instructions. Correct code needs at
most 16 times: the reference counts a step per statement, loop iteration,
call, argument, operator and array read; lowering emits at most 4 TAC
instructions per step, no optimization adds an executed instruction, and
register allocation adds at most 3 per instruction (see `harness.py`; a test
checks this on adversarial programs). So a miscompiled infinite loop fails
fast without ever flagging correct code. Failing programs are saved with their seed and all
outputs (`--out DIR`), and can be regenerated with `generate --seed N`.

## Test-case reducer

`reduce.py` shrinks a failing program, in the style of delta debugging and
C-Reduce. A change is kept only if the program still passes semantic
analysis, the reference interpreter still finishes, and every compiled
version that failed still fails with the same status (mismatch or crash).
Four families of changes are repeated until none succeeds:

1. delete chunks of each statement list, including whole functions (half, then quarters, ... then single statements);
2. replace an `if`, `while`, `for` or block by its body (a `for` also by its first part and its body), or drop an `else`;
3. replace an expression by `0`, `1` or one of its operands (a call by one of its arguments);
4. remove a function parameter together with the matching argument at every call.

At a fixed point the result is 1-minimal with respect to these changes (no
single one of them keeps the failure); it is not necessarily the smallest
failing program.

Example (`reduce --seed 1 --bug regalloc-entry-params`): a 92-line generated
program shrinks to 5 lines:

```
fun f1(p, q) {
    print p;      // correct: 1; with the bug, p and q share a register and it prints 0
}
var g0 = 0;
g0 = f1(1, 0);
```

## Planted bugs and evaluation

`bugs.py` contains 22 realistic mistakes, each in one optimization pass or in
the register allocator. A bug is active only inside `with planted(name):` and
is always undone afterwards; the reference interpreter, the VM and the register
machine are never changed. "Crash" below means the compiler, VM or machine
failed, or the compiled program did not finish although the reference did.

| Bug | Pass | What is wrong |
|---|---|---|
| `div-self-one` | constprop | algebraic shortcut `x / x -> 1` (wrong when x is 0) |
| `fold-div-floor` | constprop | constant division rounds down instead of toward zero |
| `fold-shift-unmasked` | constprop | constant shifts by 32 or more give 0 instead of using the low 5 bits |
| `fold-unsigned-compare` | constprop | constant comparisons treat negative numbers as large unsigned ones |
| `meet-keep-first` | constprop | at a join, two different constants merge into the first one instead of NAC |
| `unreachable-false-branch` | unreachable | reachability ignores the false edge of a conditional branch |
| `call-result-undef` | constprop | a call's result is treated as "no value yet" instead of NAC |
| `copyprop-no-kill` | copyprop | after `x = y`, a later assignment to `y` does not cancel the copy |
| `copyprop-meet-union` | copyprop | a copy available on ANY incoming path is used after a join (union, not intersection) |
| `cse-no-kill` | cse | a remembered `a op b` is reused after `a` or `b` was reassigned |
| `cse-sub-commutes` | cse | `a - b` is treated as the same expression as `b - a` |
| `cse-meet-union` | cse | an expression available on ANY incoming path is reused after a join |
| `cse-load-ignores-store` | cse | a remembered array read `a[i]` is reused after a store to `a` |
| `call-keeps-globals` | constprop | a global's known constant value survives a call that may change it |
| `copyprop-call-keeps-globals` | copyprop | a copy `x = @g` survives a call that may change `@g` |
| `liveness-ignores-branch` | dce | liveness forgets that a conditional branch reads its condition |
| `liveness-no-backedge` | dce | liveness makes one backward sweep instead of iterating to a fixed point |
| `liveness-return-ignores-globals` | dce | liveness forgets that a function's caller may read globals after it returns |
| `dce-deletes-call` | dce | a call whose result is unused is deleted, with its prints |
| `regalloc-dead-def` | regalloc | a result that is never read gets no interference edges |
| `regalloc-entry-params` | regalloc | parameters get no interference edges at the function's entry |
| `regalloc-scratch-shared` | regalloc | two spilled operands of one instruction are both loaded into `t0` |

`evaluate.py` runs, for every bug and each of seven generator settings
(default, and six that each switch one feature off: `no-swap`,
`no-functions`, `no-arrays`, `no-globals`, `no-recursion`,
`no-loop-control`), a campaign of up to 400 programs (seeds 0-399) until
the first failure, then reduces the first failing program found. It also
runs each setting on the correct compiler to count false alarms.

### Results (`results/evaluation.md`)

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

| Measure | Value |
|---|---|
| Bugs found, default generator | 22 of 22 (median 7.5 programs to the first failure) |
| Without operand swaps | 21 of 22 (`cse-sub-commutes` is missed) |
| Without functions | 15 of 22 (the seven bugs about calls, parameters and globals are missed) |
| Without arrays | 21 of 22 (`cse-load-ignores-store` is missed) |
| Without globals | 19 of 22 (the three bugs about globals are missed) |
| Without recursion | 22 of 22 (median 9.5 programs) |
| Without `for`, `break`, `continue` | 22 of 22 (median 11.5 programs) |
| Failure blamed on the pass that holds the bug | 21 of 22 |
| Reduction | median 92 -> 5.5 lines |
| False alarms on the correct compiler | 0 of 400 programs in each of the 7 settings |
| Time for the whole evaluation | 824 s |

Findings:

- **Each language feature exposes bugs nothing else does.** Without arrays,
  `cse-load-ignores-store` is never found in 400 programs; without globals,
  the three bugs about globals are never found; without functions, seven bugs
  are never found; without swapped operands, `cse-sub-commutes` is never found.
- **No bug needs recursion or loop control, and their effect on speed is
  mixed.** With recursion, 8 bugs are found sooner than without it, 4 later and
  10 at the same program (`call-result-undef` at program 21 instead of 85, but
  `fold-unsigned-compare` at 26 instead of 3). With `for`/`break`/`continue`,
  7 are found sooner, 8 later and 7 at the same program. The median over all
  bugs is lower with both: 7.5 programs, against 9.5 without recursion and
  11.5 without loop control.
- **Back-end bugs are blamed correctly.** All three register-allocation bugs
  make only the machine run fail, so the harness blames `regalloc`.
- **One bug first appears as an interaction.** In its first failing program,
  `cse-load-ignores-store` shows up only at `-O1`, so no single pass is
  blamed; the reduced 4-line program then also fails with `cse` alone.
- **Hang bugs.** Several bugs make a loop's exit condition wrong, so the
  compiled code never finishes; the step limit turns that into a fast "crash".

The planted bugs were written by us, so they show which kinds of mistakes the
fuzzer can find, not how it would perform on bugs in a production compiler.

## Results on the correct compiler

Campaign of 2,000 generated programs (seeds 0-1999, default generator):

| Measure | Value |
|---|---|
| Programs agreeing with the reference in all 9 runs | 2,000 of 2,000 |
| Skipped (did not finish) | 0 |
| Code size, O0 -> O1 | 570,995 -> 343,337 instructions (40% smaller, labels not counted) |
| Programs where each pass changed the code | constprop 100%, copyprop 98%, cse 97%, dce 97%, unreachable 91% |
| Time | 177 s (11 programs per second) |

## References

- Aho, Lam, Sethi, Ullman. *Compilers: Principles, Techniques, and Tools*, 2nd ed., 2006.
- Kildall. "A Unified Approach to Global Program Optimization." POPL 1973.
- McKeeman. "Differential Testing for Software." *Digital Technical Journal*, 1998.
- Yang, Chen, Eide, Regehr. "Finding and Understanding Bugs in C Compilers." PLDI 2011 (Csmith).
- Le, Afshari, Su. "Compiler Validation via Equivalence Modulo Inputs." PLDI 2014.
- Zeller, Hildebrandt. "Simplifying and Isolating Failure-Inducing Input." IEEE TSE, 2002 (delta debugging).
- Regehr et al. "Test-Case Reduction for C Compiler Bugs." PLDI 2012 (C-Reduce).
