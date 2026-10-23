"""Basic blocks and the control-flow graph (CFG) of a TAC program.

Leaders (first instruction of a basic block):
  1. the first instruction
  2. every label (a jump may land there)
  3. every instruction that follows a jump or conditional branch

A block runs straight through: control enters only at its start and
leaves only at its end. Edges:
  goto L              -> the block labelled L
  if a goto L1 else L2 -> both blocks
  anything else        -> falls through to the next block in program order
                          (or to EXIT after the last block)

Blocks keep their program order, so `linearize` can turn the graph back
into TAC: fall-through edges stay valid as long as removed blocks are
unreachable (an unreachable block is never the fall-through target of a
reachable one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .tac import Instr

EXIT = -1


@dataclass
class BasicBlock:
    id: int
    labels: List[str] = field(default_factory=list)   # labels that name this block
    instrs: List[Instr] = field(default_factory=list)  # excluding the label instructions
    succ: List[int] = field(default_factory=list)      # may contain EXIT
    pred: List[int] = field(default_factory=list)


@dataclass
class CFG:
    blocks: List[BasicBlock]            # in program order; blocks[0] is the entry

    def by_id(self) -> Dict[int, BasicBlock]:
        return {b.id: b for b in self.blocks}

    def __str__(self) -> str:
        out = []
        for b in self.blocks:
            names = f"  ({', '.join(b.labels)})" if b.labels else ""
            succ = ", ".join("EXIT" if s == EXIT else f"B{s}" for s in b.succ)
            out.append(f"B{b.id}{names}   preds: {', '.join(f'B{p}' for p in b.pred) or '-'}")
            for ins in b.instrs:
                out.append(f"    {ins}")
            out.append(f"    -> {succ}")
        return "\n".join(out)


def build_cfg(code: List[Instr]) -> CFG:
    blocks: List[BasicBlock] = []
    current: BasicBlock = None
    for ins in code:
        if ins.kind == "label":
            # a label starts a new block unless the current block is still empty
            if current is None or current.instrs:
                current = BasicBlock(len(blocks))
                blocks.append(current)
            current.labels.append(ins.target)
            continue
        if current is None:
            current = BasicBlock(len(blocks))
            blocks.append(current)
        current.instrs.append(ins)
        if ins.kind in ("jump", "cbr"):
            current = None                      # the next instruction is a leader
    connect(blocks)
    return CFG(blocks)


def connect(blocks: List[BasicBlock]) -> None:
    """(Re)compute successor and predecessor lists from the instructions."""
    where = {lab: b.id for b in blocks for lab in b.labels}
    ids = [b.id for b in blocks]
    for b in blocks:
        b.succ, b.pred = [], []
    for i, b in enumerate(blocks):
        last = b.instrs[-1] if b.instrs else None
        if last is not None and last.kind == "jump":
            targets = [where[last.target]]
        elif last is not None and last.kind == "cbr":
            targets = [where[last.target], where[last.target2]]
        else:
            targets = [ids[i + 1] if i + 1 < len(blocks) else EXIT]
        for t in targets:
            if t not in b.succ:
                b.succ.append(t)
    by_id = {b.id: b for b in blocks}
    for b in blocks:
        for s in b.succ:
            if s != EXIT and b.id not in by_id[s].pred:
                by_id[s].pred.append(b.id)


def reachable(cfg: CFG) -> set:
    if not cfg.blocks:
        return set()
    by_id = cfg.by_id()
    seen, stack = set(), [cfg.blocks[0].id]
    while stack:
        bid = stack.pop()
        if bid in seen or bid == EXIT:
            continue
        seen.add(bid)
        stack.extend(by_id[bid].succ)
    return seen


def linearize(cfg: CFG) -> List[Instr]:
    """Turn the CFG back into TAC, dropping jumps to the next block and unused labels."""
    code: List[Instr] = []
    for b in cfg.blocks:
        for lab in b.labels:
            code.append(Instr("label", target=lab))
        code.extend(b.instrs)
    # `goto L` immediately followed by `L:` (possibly among other labels) is redundant
    cleaned: List[Instr] = []
    for i, ins in enumerate(code):
        if ins.kind == "jump":
            j = i + 1
            following = set()
            while j < len(code) and code[j].kind == "label":
                following.add(code[j].target)
                j += 1
            if ins.target in following:
                continue
        cleaned.append(ins)
    used = {ins.target for ins in cleaned if ins.kind in ("jump", "cbr")} | \
           {ins.target2 for ins in cleaned if ins.kind == "cbr"}
    return [ins for ins in cleaned if ins.kind != "label" or ins.target in used]
