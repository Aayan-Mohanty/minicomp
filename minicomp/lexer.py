"""Phase 1: lexical analysis. Turns source text into a list of tokens.

Token kinds:
    INT      decimal literal 0 .. 2147483647
    IDENT    [A-Za-z_][A-Za-z0-9_]*   (not a keyword)
    keyword  var print if else while for break continue fun return  (kind is the word itself)
    operator / punctuation            (kind is the symbol itself)
    EOF      end of input
Whitespace and // comments are skipped.

Implemented as a hand-written scanner: at each position the longest
operator that matches is taken ("<=" before "<", "<<" before "<").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .errors import CompileError
from .ops import INT_MAX

KEYWORDS = {"var", "print", "if", "else", "while", "for", "break", "continue", "fun", "return"}

# Longest first, so that maximal munch picks "<<" over "<".
OPERATORS = sorted([
    "&&", "||", "==", "!=", "<=", ">=", "<<", ">>",
    "+", "-", "*", "/", "%", "&", "|", "^", "~", "!", "<", ">", "=",
    "(", ")", "{", "}", "[", "]", ";", ",",
], key=len, reverse=True)


@dataclass(frozen=True)
class Token:
    kind: str       # "INT", "IDENT", "EOF", a keyword, or an operator symbol
    text: str
    line: int
    col: int

    @property
    def value(self) -> int:
        return int(self.text)


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    i, line, col = 0, 1, 1
    n = len(source)

    while i < n:
        ch = source[i]

        if ch == "\n":
            i, line, col = i + 1, line + 1, 1
            continue
        if ch in " \t\r":
            i, col = i + 1, col + 1
            continue
        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                i += 1
            continue

        start_col = col
        if ch.isdigit():
            j = i
            while j < n and source[j].isdigit():
                j += 1
            text = source[i:j]
            if j < n and (source[j].isalpha() or source[j] == "_"):
                raise CompileError("lexical", f"invalid number '{text}{source[j]}'", line, start_col)
            if len(text) > 1 and text[0] == "0":
                raise CompileError("lexical", f"leading zeros are not allowed: '{text}'", line, start_col)
            if int(text) > INT_MAX:
                raise CompileError("lexical", f"integer literal {text} is larger than {INT_MAX}", line, start_col)
            tokens.append(Token("INT", text, line, start_col))
            col += j - i
            i = j
            continue

        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            text = source[i:j]
            tokens.append(Token(text if text in KEYWORDS else "IDENT", text, line, start_col))
            col += j - i
            i = j
            continue

        for op in OPERATORS:
            if source.startswith(op, i):
                tokens.append(Token(op, op, line, start_col))
                i += len(op)
                col += len(op)
                break
        else:
            raise CompileError("lexical", f"unexpected character {ch!r}", line, start_col)

    tokens.append(Token("EOF", "", line, col))
    return tokens
