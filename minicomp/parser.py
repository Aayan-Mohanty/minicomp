"""Phase 2: syntax analysis. A hand-written recursive-descent parser.

Grammar (EBNF). Each precedence level is one function; lower levels
bind more loosely. All binary operators are left-associative.

    program    -> stmt* EOF
    stmt       -> "var" IDENT "=" expr ";"
                | IDENT "=" expr ";"
                | "print" expr ";"
                | "if" "(" expr ")" block [ "else" ( block | if_stmt ) ]
                | "while" "(" expr ")" block
                | block
    block      -> "{" stmt* "}"

    expr       -> or
    or         -> and        { "||" and }                    (lowest)
    and        -> bitor      { "&&" bitor }
    bitor      -> bitxor     { "|" bitxor }
    bitxor     -> bitand     { "^" bitand }
    bitand     -> equality   { "&" equality }
    equality   -> relational { ("==" | "!=") relational }
    relational -> shift      { ("<" | "<=" | ">" | ">=") shift }
    shift      -> additive   { ("<<" | ">>") additive }
    additive   -> term       { ("+" | "-") term }
    term       -> unary      { ("*" | "/" | "%") unary }
    unary      -> ("-" | "!" | "~") unary | primary
    primary    -> INT | IDENT | "(" expr ")"                  (highest)

The grammar is LL(1): one token of lookahead always decides which rule
applies, so the parser never backtracks.
"""

from __future__ import annotations

from typing import List

from .errors import CompileError
from .lexer import Token, tokenize
from .nodes import (Assign, Binary, Block, Expr, If, IntLit, Print, Program,
                    Stmt, Unary, Var, VarDecl, While)

# Binary precedence levels, loosest first. Used by the parser and the printer.
BINARY_LEVELS = [
    ["||"], ["&&"], ["|"], ["^"], ["&"],
    ["==", "!="], ["<", "<=", ">", ">="], ["<<", ">>"],
    ["+", "-"], ["*", "/", "%"],
]
PRECEDENCE = {op: i for i, level in enumerate(BINARY_LEVELS) for op in level}
UNARY_OPS = ("-", "!", "~")


def parse(source: str) -> Program:
    return Parser(tokenize(source)).program()


class Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # -- helpers ---------------------------------------------------------
    @property
    def tok(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        t = self.tokens[self.pos]
        if t.kind != "EOF":
            self.pos += 1
        return t

    def check(self, kind: str) -> bool:
        return self.tok.kind == kind

    def expect(self, kind: str, what: str) -> Token:
        if self.tok.kind == kind:
            return self.advance()
        found = "end of input" if self.tok.kind == "EOF" else f"'{self.tok.text}'"
        raise CompileError("syntax", f"expected {what}, found {found}", self.tok.line, self.tok.col)

    # -- statements ------------------------------------------------------
    def program(self) -> Program:
        body = []
        while not self.check("EOF"):
            body.append(self.statement())
        return Program(body)

    def statement(self) -> Stmt:
        t = self.tok
        if t.kind == "var":
            self.advance()
            name = self.expect("IDENT", "a variable name")
            self.expect("=", "'=' (a declaration needs an initial value)")
            init = self.expression()
            self.expect(";", "';'")
            return VarDecl(name.text, init, t.line, t.col)
        if t.kind == "print":
            self.advance()
            value = self.expression()
            self.expect(";", "';'")
            return Print(value, t.line, t.col)
        if t.kind == "if":
            return self.if_statement()
        if t.kind == "while":
            self.advance()
            self.expect("(", "'(' after 'while'")
            cond = self.expression()
            self.expect(")", "')'")
            return While(cond, self.block(), t.line, t.col)
        if t.kind == "{":
            return self.block()
        if t.kind == "IDENT":
            self.advance()
            self.expect("=", "'=' after a variable name")
            value = self.expression()
            self.expect(";", "';'")
            return Assign(t.text, value, t.line, t.col)
        found = "end of input" if t.kind == "EOF" else f"'{t.text}'"
        raise CompileError("syntax", f"expected a statement, found {found}", t.line, t.col)

    def if_statement(self) -> If:
        t = self.expect("if", "'if'")
        self.expect("(", "'(' after 'if'")
        cond = self.expression()
        self.expect(")", "')'")
        then = self.block()
        orelse = None
        if self.check("else"):
            self.advance()
            orelse = self.if_statement() if self.check("if") else self.block()
        return If(cond, then, orelse, t.line, t.col)

    def block(self) -> Block:
        t = self.expect("{", "'{'")
        body = []
        while not self.check("}"):
            if self.check("EOF"):
                raise CompileError("syntax", "missing '}' to close the block opened here", t.line, t.col)
            body.append(self.statement())
        self.advance()
        return Block(body, t.line, t.col)

    # -- expressions -----------------------------------------------------
    def expression(self) -> Expr:
        return self.binary_level(0)

    def binary_level(self, level: int) -> Expr:
        """One function body serves every precedence level of the grammar."""
        if level == len(BINARY_LEVELS):
            return self.unary()
        left = self.binary_level(level + 1)
        while self.tok.kind in BINARY_LEVELS[level]:
            op = self.advance()
            right = self.binary_level(level + 1)
            left = Binary(op.kind, left, right, op.line, op.col)
        return left

    def unary(self) -> Expr:
        if self.tok.kind in UNARY_OPS:
            op = self.advance()
            return Unary(op.kind, self.unary(), op.line, op.col)
        return self.primary()

    def primary(self) -> Expr:
        t = self.tok
        if t.kind == "INT":
            self.advance()
            return IntLit(t.value, t.line, t.col)
        if t.kind == "IDENT":
            self.advance()
            return Var(t.text, t.line, t.col)
        if t.kind == "(":
            self.advance()
            e = self.expression()
            self.expect(")", "')'")
            return e
        found = "end of input" if t.kind == "EOF" else f"'{t.text}'"
        raise CompileError("syntax", f"expected an expression, found {found}", t.line, t.col)
