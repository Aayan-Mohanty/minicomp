"""Exact semantics of every Mini operator on 32-bit integers.

Every Mini program has exactly one correct output. There is no undefined
behaviour: overflow wraps, division by zero has a defined result, and
shift amounts are masked. This is what makes differential testing sound:
if two compilations of the same program print different values, one of
them is wrong.

    Integers   32-bit two's complement; every result wraps into
               [-2147483648, 2147483647].
    a / b      truncates toward zero (like C); a / 0 == 0.
    a % b      defined as a - (a / b) * b, so a % 0 == a and the sign
               follows a (like C).
    a << b     shift amount is b & 31; result wraps.
    a >> b     arithmetic shift (keeps the sign); amount is b & 31.
    comparisons, &&, ||, !   produce 1 (true) or 0 (false); any non-zero
               value counts as true.
"""

INT_MIN = -(1 << 31)
INT_MAX = (1 << 31) - 1


def wrap(x: int) -> int:
    """Reduce any Python int to the 32-bit two's complement range."""
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def div(a: int, b: int) -> int:
    if b == 0:
        return 0
    q = abs(a) // abs(b)                  # magnitude, rounded down
    return wrap(q if (a < 0) == (b < 0) else -q)   # sign applied -> truncation toward zero


def mod(a: int, b: int) -> int:
    return wrap(a - wrap(div(a, b) * b))


def binary(op: str, a: int, b: int) -> int:
    """Apply a binary operator to two in-range values."""
    if op == "+":  return wrap(a + b)
    if op == "-":  return wrap(a - b)
    if op == "*":  return wrap(a * b)
    if op == "/":  return div(a, b)
    if op == "%":  return mod(a, b)
    if op == "&":  return wrap(a & b)
    if op == "|":  return wrap(a | b)
    if op == "^":  return wrap(a ^ b)
    if op == "<<": return wrap(a << (b & 31))
    if op == ">>": return a >> (b & 31)          # Python >> on ints is arithmetic
    if op == "==": return int(a == b)
    if op == "!=": return int(a != b)
    if op == "<":  return int(a < b)
    if op == "<=": return int(a <= b)
    if op == ">":  return int(a > b)
    if op == ">=": return int(a >= b)
    if op == "&&": return int(a != 0 and b != 0)
    if op == "||": return int(a != 0 or b != 0)
    raise ValueError(f"unknown binary operator {op!r}")


def unary(op: str, a: int) -> int:
    if op == "-": return wrap(-a)
    if op == "!": return int(a == 0)
    if op == "~": return wrap(~a)
    raise ValueError(f"unknown unary operator {op!r}")
