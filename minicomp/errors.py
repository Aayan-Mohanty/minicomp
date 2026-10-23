"""Error type shared by every compiler phase."""


class CompileError(Exception):
    """A lexical, syntax or semantic error, with its source position."""

    def __init__(self, phase: str, message: str, line: int, col: int) -> None:
        super().__init__(f"{line}:{col}: {phase} error: {message}")
        self.phase = phase        # "lexical", "syntax" or "semantic"
        self.message = message
        self.line = line
        self.col = col
