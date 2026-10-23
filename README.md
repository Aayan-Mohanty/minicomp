# MiniComp: a self-testing optimizing compiler

An optimizing compiler for a small language (Mini), together with a
differential fuzzer that generates random programs, compares optimized and
unoptimized results to find miscompilations, and automatically shrinks each
failing program to a minimal example.

## Problem statement

Optimizing compilers rewrite programs to make them faster. If an
optimization is wrong, the compiled program silently computes a different
result from what the source code says: a *miscompilation*. These bugs are
hard to find because the source code is correct, the compiler reports no
error, and the wrong result may appear only for particular programs.

Writing test programs by hand covers only the cases the tester thinks of.
Random differential testing instead generates large numbers of valid
programs automatically and runs each one in two ways (unoptimized and
optimized). Since every Mini program has exactly one correct output, any
difference means the compiler has a bug. Because random failing programs
are large, a reducer then removes everything not needed to trigger the
bug, leaving a small example a developer can understand.

## Objectives

1. Define a small language with **fully specified semantics** (no undefined behaviour).
2. Build its compiler: lexer, recursive-descent parser, semantic analysis,
   three-address code (TAC), control-flow graph, and classic optimizations.
3. Build a random generator of valid, always-terminating Mini programs.
4. Detect miscompilations by comparing reference, unoptimized and optimized outputs.
5. Automatically reduce failing programs to minimal examples.
6. Evaluate how many deliberately injected optimizer bugs the fuzzer finds, and how quickly.

## Components and progress

| # | Component | Weight | Status |
|---|---|---|---|
| 1 | Language definition + lexer | 7% | Done |
| 2 | Recursive-descent parser -> AST (+ source printer) | 10% | Done |
| 3 | Semantic analysis with scoped symbol table | 5% | Done |
| 4 | Reference interpreter (ground truth) | 5% | Done |
| 5 | Three-address code generation + TAC virtual machine | 13% | Done |
| 6 | Basic blocks and control-flow graph | 5% | Done |
| 7 | Optimizations: constant folding/propagation, copy propagation, dead-code elimination, common-subexpression elimination | 15% | Done |
| 8 | Random program generator | 12% | Done |
| 9 | Differential testing harness | 8% | Done |
| 10 | Test-case reducer (delta debugging) | 10% | Done |
| 11 | Evaluation with injected optimizer bugs | 10% | Done |

All 11 components are done (**100%**).

## Usage

```
python3 -m minicomp tokens  examples/gcd.mini     # lexer output
python3 -m minicomp ast     examples/gcd.mini     # syntax tree
python3 -m minicomp symbols examples/scopes.mini  # symbol table
python3 -m minicomp fmt     examples/fib.mini     # formatted source
python3 -m minicomp run     examples/fib.mini     # run (reference interpreter)
python3 -m minicomp tac     examples/gcd.mini     # three-address code
python3 -m minicomp tac     examples/gcd.mini -O1 # optimized three-address code
python3 -m minicomp cfg     examples/gcd.mini     # basic blocks and control-flow graph
python3 -m minicomp vm      examples/fib.mini -O1 # compile and run on the TAC virtual machine
python3 -m minicomp compare examples/fib.mini     # reference vs -O0 vs -O1, sizes, verdict
python3 -m minicomp tac FILE --passes cse,dce     # run only some optimization passes
python3 -m minicomp generate --seed 3             # print the random program for seed 3
python3 -m minicomp fuzz -n 500                   # differential testing campaign
python3 -m minicomp fuzz -n 500 --seed 1000 --out failures --stop-after 5
python3 -m minicomp bugs                          # list the 11 planted optimizer bugs
python3 -m minicomp fuzz -n 200 --bug cse-no-kill --stop-after 1   # hunt a planted bug
python3 -m minicomp reduce --seed 110 --bug copyprop-no-kill       # shrink its failing program
python3 -m minicomp compare FILE --bug fold-div-floor              # 8 runs with a bug planted
python3 -m minicomp evaluate -n 400 --out results/evaluation.json  # full evaluation (about 3 min)
python3 -m pytest                                 # tests
```

No third-party packages are needed except pytest for the tests.

## Web app: MiniComp Console

```
python3 -m webapp.server          # then open http://127.0.0.1:8000
```

An IDE-style console. Cyan marks the reference path, amber the compiled path.

- **Pipeline bar**: every stage (lex, parse, sema, ref, tac, cfg, opt, vm, diff)
  with its time in milliseconds and a count (tokens, nodes, instructions, agreement).
- **diff**: the 8 runs side by side; values that differ from the reference are underlined in red.
- **front end**: tokens by kind, the syntax tree, the symbol table.
- **tac**: three-address code at -O0 and -O1.
- **opt trace**: step through the optimizer pass by pass (arrow keys), with the
  lines each pass added and removed and a code-size bar per step.
- **cfg**: the control-flow graph at -O0 or -O1.
- **fuzz**: a live campaign of up to 500 programs streamed from the server; each
  square is one program (green agree, red bug, grey did not finish); click one to open it.
- **console**: a timestamped log of every request with its server time and request id.

Shortcuts: Ctrl/Cmd+Enter compile, Alt+R random program, Alt+1..6 views.

### API (v1)

| Method and path | Purpose |
|---|---|
| `GET /api/v1/health` | Liveness, version, uptime |
| `GET /api/v1/meta` | Passes, limits, language rules |
| `GET /api/v1/examples` | The programs in `examples/` |
| `POST /api/v1/compile` `{"code"}` | Every stage with timings, optimizer trace, CFGs, 8-way verdict |
| `POST /api/v1/generate` `{"seed"}` | The random program for a seed |
| `GET /api/v1/fuzz/stream?n=&seed=` | Server-Sent Events: one `program` event per program, then `done` (n <= 500) |
| `POST /api/v1/fuzz` `{"n","seed"}` | The same campaign as one reply, `{"ok", "summary", "results"}` (n <= 100); the page falls back to it if streaming fails |

Standard library only. Each response has an `X-Request-Id` header and each request
is logged as one JSON line. Clients are rate-limited per IP address (token bucket;
too many requests get HTTP 429). Programs run only inside the Mini interpreter and VM
with step limits; bodies are limited to 50 KB and programs to 20,000 characters.

### Deploying on Render

Build command `pip install -r requirements.txt`; start command
`python -m webapp.server --host 0.0.0.0` (the port comes from `PORT`);
environment variable `PYTHON_VERSION` = `3.13.5`.

## The Mini language

```
var x = 10;            // declaration (an initial value is required)
x = x * 2;             // assignment
print x;               // output
if (x > 5) { ... } else if (x == 0) { ... } else { ... }
while (x > 0) { ... }
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
| Comparisons, `&&`, `||`, `!` | result is 1 or 0; any non-zero value is true |
| Scope | a block's variables end with the block; inner blocks may shadow |
| `var x = e;` | `e` is evaluated before `x` exists, so it refers to an outer `x` |

Real languages such as C leave overflow, division by zero and large shifts
*undefined*, which makes a generated program's correct output unknowable.
Defining every case is what lets differential testing treat any output
difference as a compiler bug.

Infinite loops are stopped after a fixed number of steps; such programs are
excluded from comparisons.

## Code

| File | Phase |
|---|---|
| `minicomp/lexer.py` | Lexical analysis (maximal munch, positions, lexical errors) |
| `minicomp/parser.py` | Syntax analysis (recursive descent, one function per precedence level) |
| `minicomp/nodes.py` | AST node types |
| `minicomp/semantic.py` | Semantic analysis, scoped symbol table |
| `minicomp/ops.py` | Exact operator semantics, shared by every back end |
| `minicomp/interp.py` | Reference interpreter |
| `minicomp/printer.py` | AST -> source (used later by the generator and reducer) |
| `minicomp/tac.py` | Three-address code instructions |
| `minicomp/lower.py` | Intermediate code generation (AST -> TAC); unique names for shadowed variables |
| `minicomp/vm.py` | Virtual machine that runs TAC and counts executed instructions |
| `minicomp/cfg.py` | Basic blocks (leaders), control-flow graph, linearization back to TAC |
| `minicomp/opt.py` | Optimizer: passes listed below, repeated until the code stops changing |
| `minicomp/generator.py` | Random generator of valid, always-terminating programs |
| `minicomp/harness.py` | Differential testing: runs every configuration, compares, blames passes, runs campaigns |
| `minicomp/reduce.py` | Test-case reducer: shrinks a failing program while the same bug still shows |
| `minicomp/bugs.py` | 11 planted optimizer bugs, switched on only inside `with planted(name):` |
| `minicomp/evaluate.py` | Planted-bug evaluation: detection, programs needed, blame, reduction, false alarms |
| `results/evaluation.md`, `results/evaluation.json` | Output of the full evaluation run |
| `webapp/server.py`, `webapp/static/index.html` | Web backend and page |

## Optimizations

| Pass | Scope | Technique |
|---|---|---|
| `constprop` | Global | Constant propagation by forward data-flow analysis (worklist; lattice UNDEF > constant > NAC; meet at joins), then constant folding, algebraic identities, and replacing branches on constants with gotos |
| `unreachable` | Global | Removes blocks not reachable from the entry |
| `copyprop` | Local (basic block) | After `x = y`, later uses of `x` read `y` |
| `cse` | Local (basic block) | Reuses an earlier identical `a op b` (operands of commutative operators are normalized) |
| `dce` | Global | Deletes assignments whose result is never read, using liveness analysis (backward data-flow) |

Algebraic identities are used only if they hold for every 32-bit value.
For example `x - x -> 0` is used, but `x / x -> 1` is not, because in
Mini `0 / 0 == 0`.

## Random program generator

Each generated program is guaranteed to be valid (the generator tracks
scopes exactly like semantic analysis) and to terminate (every loop has its
own counter, incremented first in the body and never assigned elsewhere).
The program ends by printing every top-level variable, so wrong values show
up in the output. Expressions are biased toward what stresses an optimizer:
edge-case constants, `x op x` patterns, repeated subexpressions, constant
branch conditions, and shadowed variables. The same seed always gives the
same program.

Optional setting `GenConfig(swap_reuse=0.5)` (`--swap` on the command line):
reused subexpressions are sometimes written with their operands swapped
(`b - a` after `a - b`). It is off by default and draws from a separate random
stream, so with it off every seed produces exactly the same program as before;
it was added because the evaluation showed one bug that needs it (below).

## Differential testing harness

Each program is run as: the reference interpreter, unoptimized TAC (`O0`),
fully optimized TAC (`O1`), and TAC optimized by each pass alone. Any output
that differs from the reference, or any crash, is a failure; passes that
fail on their own are reported as suspects. Failing programs are saved with
their seed and all outputs (`--out DIR`), and can be regenerated with
`generate --seed N`.

## Test-case reducer

`reduce.py` shrinks a failing program to a minimal one that still fails in
the same way, in the style of delta debugging and C-Reduce. A change is kept
only if the program still passes semantic analysis, the reference interpreter
still finishes, and exactly the same compiled versions still fail with the
same status (mismatch or crash). That last rule keeps the reducer on the same
bug and keeps the blame unchanged. Three families of changes are repeated
until none succeeds:

1. delete chunks of each statement list (half, then quarters, ... then single statements);
2. replace an `if`, `while` or block by its body, or drop an `else`;
3. replace an expression by `0`, `1` or one of its operands.

Example (`reduce --seed 110 --bug copyprop-no-kill`): a 62-line generated
program shrinks to 5 lines in 70 candidate tests:

```
var a = 1;
var b = 0;
b = a;
a = 0;
print b;      // correct: 1; with the bug, copy propagation prints 0
```

## Planted bugs and evaluation

`bugs.py` contains 11 realistic mistakes, each in one optimization pass. A bug
is active only inside `with planted(name):` and is always undone afterwards; the
reference interpreter and the VM are never changed. "Crash" below means the
compiler or VM failed, or the compiled program did not finish although the
reference did.

| Bug | Pass | What is wrong |
|---|---|---|
| `div-self-one` | constprop | algebraic shortcut `x / x -> 1` (wrong when x is 0) |
| `fold-div-floor` | constprop | constant division rounds down instead of toward zero |
| `fold-shift-unmasked` | constprop | constant shifts by 32 or more give 0 instead of using the low 5 bits |
| `fold-unsigned-compare` | constprop | constant comparisons treat negative numbers as large unsigned ones |
| `meet-keep-first` | constprop | at a join, two different constants merge into the first one instead of NAC |
| `unreachable-false-branch` | unreachable | reachability ignores the false edge of a conditional branch |
| `copyprop-no-kill` | copyprop | after `x = y`, a later assignment to `y` does not cancel the copy |
| `cse-no-kill` | cse | a remembered `a op b` is reused after `a` or `b` was reassigned |
| `cse-sub-commutes` | cse | `a - b` is treated as the same expression as `b - a` |
| `liveness-ignores-branch` | dce | liveness forgets that a conditional branch reads its condition |
| `liveness-no-backedge` | dce | liveness makes one backward sweep instead of iterating to a fixed point |

`evaluate.py` runs, for every bug and both generator settings, a campaign of up
to 400 programs (seeds 0-399) until the first failure, then reduces it. It also
runs both settings on the correct compiler to count false alarms.

### Results (`results/evaluation.md`)

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
| `cse-sub-commutes` | cse | not in 400 | program 242 | mismatch | no (passes together) | 193 -> 6 lines |
| `liveness-ignores-branch` | dce | program 1 | program 1 | crash | yes | 27 -> 4 lines |
| `liveness-no-backedge` | dce | program 1 | program 1 | crash | yes | 27 -> 4 lines |

| Measure | Value |
|---|---|
| Bugs found, default generator | 10 of 11 (median 11 programs to the first failure) |
| Bugs found, either setting | 11 of 11 |
| Failure blamed on the pass that holds the bug | 10 of 11 |
| Reduction | median 27 -> 4 lines |
| False alarms on the correct compiler | 0 of 400 programs (default), 0 of 400 (swap) |

Findings:

- **A generator blind spot.** `cse-sub-commutes` needs `a - b` and `b - a`
  in the same basic block. The default generator reuses expressions only as
  exact copies, so it never produced that pair in 400 programs. Adding the
  optional operand swap found the bug at program 242.
- **A bug that needs two passes.** The reduced program for `cse-sub-commutes`
  computes the same subexpression into two different temporaries; CSE alone
  sees different operand names, and only after copy propagation renames them
  does the wrong reuse happen. The harness therefore reports it as an
  interaction ("passes together") instead of blaming CSE alone.
- **Hang bugs.** `meet-keep-first` and `liveness-no-backedge` make a loop's
  exit condition constant or delete the loop's update, so the compiled code
  never finishes; the harness reports this as a crash.

The planted bugs were written by us, so they show which kinds of mistakes the
fuzzer can find, not how it would perform on bugs in a production compiler.

## Results on the correct compiler

Campaign of 2,000 generated programs (seeds 0-1999):

| Measure | Value |
|---|---|
| Programs agreeing with the reference in every configuration | 2,000 of 2,000 |
| Skipped (did not terminate) | 0 |
| Code size, O0 -> O1 | 382,931 -> 171,716 instructions (55% smaller) |
| Programs where each pass changed the code | constprop 100%, copyprop 95%, cse 86%, dce 80%, unreachable 21% |

## References

- Aho, Lam, Sethi, Ullman. *Compilers: Principles, Techniques, and Tools*, 2nd ed., 2006.
- Kildall. "A Unified Approach to Global Program Optimization." POPL 1973.
- McKeeman. "Differential Testing for Software." *Digital Technical Journal*, 1998.
- Yang, Chen, Eide, Regehr. "Finding and Understanding Bugs in C Compilers." PLDI 2011 (Csmith).
- Le, Afshari, Su. "Compiler Validation via Equivalence Modulo Inputs." PLDI 2014.
- Zeller, Hildebrandt. "Simplifying and Isolating Failure-Inducing Input." IEEE TSE, 2002 (delta debugging).
- Regehr et al. "Test-Case Reduction for C Compiler Bugs." PLDI 2012 (C-Reduce).
