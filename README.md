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
| 5 | Three-address code generation + TAC virtual machine | 13% | |
| 6 | Basic blocks and control-flow graph | 5% | |
| 7 | Optimizations: constant folding/propagation, copy propagation, dead-code elimination, common-subexpression elimination | 15% | |
| 8 | Random program generator | 12% | |
| 9 | Differential testing harness | 8% | |
| 10 | Test-case reducer (delta debugging) | 10% | |
| 11 | Evaluation with injected optimizer bugs | 10% | |

Done so far: components 1-4 = **27%**.

## Usage

```
python3 -m minicomp tokens  examples/gcd.mini     # lexer output
python3 -m minicomp ast     examples/gcd.mini     # syntax tree
python3 -m minicomp symbols examples/scopes.mini  # symbol table
python3 -m minicomp fmt     examples/fib.mini     # formatted source
python3 -m minicomp run     examples/fib.mini     # run (reference interpreter)
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

## References

- Aho, Lam, Sethi, Ullman. *Compilers: Principles, Techniques, and Tools*, 2nd ed., 2006.
- McKeeman. "Differential Testing for Software." *Digital Technical Journal*, 1998.
- Yang, Chen, Eide, Regehr. "Finding and Understanding Bugs in C Compilers." PLDI 2011 (Csmith).
- Le, Afshari, Su. "Compiler Validation via Equivalence Modulo Inputs." PLDI 2014.
- Zeller, Hildebrandt. "Simplifying and Isolating Failure-Inducing Input." IEEE TSE, 2002 (delta debugging).
- Regehr et al. "Test-Case Reduction for C Compiler Bugs." PLDI 2012 (C-Reduce).
