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
| 10 | Test-case reducer (delta debugging) | 10% | |
| 11 | Evaluation with injected optimizer bugs | 10% | |

Done so far: components 1-9 = **80%**.

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
python3 -m pytest                                 # tests
```

No third-party packages are needed except pytest for the tests.

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

## Differential testing harness

Each program is run as: the reference interpreter, unoptimized TAC (`O0`),
fully optimized TAC (`O1`), and TAC optimized by each pass alone. Any output
that differs from the reference, or any crash, is a failure; passes that
fail on their own are reported as suspects. Failing programs are saved with
their seed and all outputs (`--out DIR`), and can be regenerated with
`generate --seed N`.

## Results so far

Campaign of 2,000 generated programs (seeds 0-1999) on the current compiler:

| Measure | Value |
|---|---|
| Programs agreeing with the reference in every configuration | 2,000 of 2,000 |
| Skipped (did not terminate) | 0 |
| Code size, O0 -> O1 | 382,931 -> 171,716 instructions (55% smaller) |
| Programs where each pass changed the code | constprop 100%, copyprop 95%, cse 86%, dce 80%, unreachable 21% |

Detection check with two planted bugs (in the test suite):

| Planted bug | Detected as | Blamed pass |
|---|---|---|
| Wrong identity `x / x -> 1` | mismatch | constprop (where folding happens) |
| Liveness ignores the operand of a conditional branch | crash (variable read before it is set) | dce |

A systematic evaluation over a set of planted bugs, with detection rates and
time to detection, is component 11.

## References

- Aho, Lam, Sethi, Ullman. *Compilers: Principles, Techniques, and Tools*, 2nd ed., 2006.
- Kildall. "A Unified Approach to Global Program Optimization." POPL 1973.
- McKeeman. "Differential Testing for Software." *Digital Technical Journal*, 1998.
- Yang, Chen, Eide, Regehr. "Finding and Understanding Bugs in C Compilers." PLDI 2011 (Csmith).
- Le, Afshari, Su. "Compiler Validation via Equivalence Modulo Inputs." PLDI 2014.
- Zeller, Hildebrandt. "Simplifying and Isolating Failure-Inducing Input." IEEE TSE, 2002 (delta debugging).
- Regehr et al. "Test-Case Reduction for C Compiler Bugs." PLDI 2012 (C-Reduce).
