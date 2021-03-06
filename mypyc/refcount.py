"""Transformation for inserting refrecence count inc/dec opcodes.

This transformation happens towards the end of compilation. Before this
transformation, reference count management is not explicitly handled at all.
By postponing this pass, the previous passes are simpler as they don't have
to update reference count opcodes.

The approach is to decrement reference counts soon after a value is no
longer live, to quickly free memory (and call __del__ methods), though
there are no strict guarantees -- other than that local variables are
freed before return from a function.

Function arguments are a little special. They are initially considered
'borrowed' from the caller and their reference counts don't need to be
decremented before returning. An assignment to a borrowed value turns it
into a regular, owned reference that needs to freed before return.
"""

from typing import List, Dict, Tuple, Set, Iterable, Optional

from mypyc.analysis import (
    get_cfg,
    analyze_must_defined_regs,
    analyze_live_regs,
    analyze_borrowed_arguments,
    AnalysisDict
)
from mypyc.ops import (
    FuncIR, BasicBlock, Assign, RegisterOp, DecRef, IncRef, Branch, Goto, Environment,
    Return, Op, Cast, Box, RType, Value, Register, AssignmentTargetRegister
)


DecIncs = Tuple[Tuple[Value, ...], Tuple[Value, ...]]

# A of basic blocks that decrement and increment specific values and
# then jump to some target block. This lets us cut down on how much
# code we generate in some circumstances.
BlockCache = Dict[Tuple[BasicBlock, DecIncs], BasicBlock]


def insert_ref_count_opcodes(ir: FuncIR) -> None:
    """Insert reference count inc/dec opcodes to a function.

    This is the entry point to this module.
    """
    cfg = get_cfg(ir.blocks)
    borrowed = set(reg for reg in ir.env.regs() if reg.is_borrowed)
    live = analyze_live_regs(ir.blocks, cfg)
    borrow = analyze_borrowed_arguments(ir.blocks, cfg, borrowed)
    cache = {}  # type: BlockCache
    for block in ir.blocks[:]:
        if isinstance(block.ops[-1], (Branch, Goto)):
            insert_branch_inc_and_decrefs(block,
                                          cache,
                                          ir.blocks,
                                          live.before,
                                          borrow.before,
                                          borrow.after,
                                          ir.env)
        transform_block(block, live.before, live.after, borrow.before, ir.env)


def maybe_append_dec_ref(ops: List[Op], dest: Value) -> None:
    if dest.type.is_refcounted:
        ops.append(DecRef(dest))


def maybe_append_inc_ref(ops: List[Op], dest: Value) -> None:
    if dest.type.is_refcounted:
        ops.append(IncRef(dest))


def transform_block(block: BasicBlock,
                    pre_live: AnalysisDict[Value],
                    post_live: AnalysisDict[Value],
                    pre_borrow: AnalysisDict[Value],
                    env: Environment) -> None:
    old_ops = block.ops
    ops = []  # type: List[Op]
    for i, op in enumerate(old_ops):
        key = (block, i)
        if isinstance(op, (Assign, Cast, Box)):
            dest = op.dest if isinstance(op, Assign) else op
            # These operations just copy/steal a reference and don't create new
            # references.
            if op.src in post_live[key] or op.src in pre_borrow[key]:
                maybe_append_inc_ref(ops, op.src)
                if (dest not in pre_borrow[key] and
                        dest in pre_live[key]):
                    maybe_append_dec_ref(ops, dest)
            ops.append(op)
            if dest not in post_live[key]:
                assert dest is not None
                maybe_append_dec_ref(ops, dest)
        elif isinstance(op, RegisterOp):
            # These operations construct a new reference.
            assert op not in pre_live[key]
            ops.append(op)
            for src in op.unique_sources():
                # Decrement source that won't be live afterwards.
                if src not in post_live[key] and src not in pre_borrow[key]:
                    maybe_append_dec_ref(ops, src)
            if not op.is_void and op not in post_live[key] and not op.is_borrowed:
                maybe_append_dec_ref(ops, op)
        elif isinstance(op, Return) and op.reg in pre_borrow[key]:
            # The return op returns a new reference.
            maybe_append_inc_ref(ops, op.reg)
            ops.append(op)
        else:
            ops.append(op)
    block.ops = ops


def insert_branch_inc_and_decrefs(
        block: BasicBlock,
        cache: BlockCache,
        blocks: List[BasicBlock],
        pre_live: AnalysisDict[Value],
        pre_borrow: AnalysisDict[Value],
        post_borrow: AnalysisDict[Value],
        env: Environment) -> None:
    """Insert inc_refs and/or dec_refs after a branch/goto.

    Add dec_refs for registers that become dead after a branch.
    Add inc_refs for registers that become unborrowed after a branch or goto.

    Branches are special as the true and false targets may have a different
    live and borrowed register sets. Add new blocks before the true/false target
    blocks that tweak reference counts.

    Example where we need to add an inc_ref:

      def f(a: int) -> None
          if a:
              a = 1
          return a  # a is borrowed if condition is false and unborrowed if true
    """
    prev_key = (block, len(block.ops) - 1)
    source_live_regs = pre_live[prev_key]
    source_borrowed = post_borrow[prev_key]
    if isinstance(block.ops[-1], Branch):
        branch = block.ops[-1]
        # HAX: After we've checked against an error value the value we must not touch the
        #      refcount since it will be a null pointer. The correct way to do this would be
        #      to perform data flow analysis on whether a value can be null (or is always
        #      null).
        if branch.op == Branch.IS_ERROR:
            omitted = {branch.left}
        else:
            omitted = set()
        true_decincs = (
            after_branch_decrefs(
                branch.true, pre_live, source_borrowed, source_live_regs, env, omitted),
            after_branch_increfs(
                branch.true, pre_borrow, source_borrowed, env))
        branch.true = add_block(true_decincs, cache, blocks, branch.true)

        false_decincs = (
            after_branch_decrefs(
                branch.false, pre_live, source_borrowed, source_live_regs, env),
            after_branch_increfs(
                branch.false, pre_borrow, source_borrowed, env))
        branch.false = add_block(false_decincs, cache, blocks, branch.false)
    elif isinstance(block.ops[-1], Goto):
        goto = block.ops[-1]
        new_decincs = ((), after_branch_increfs(
            goto.label, pre_borrow, source_borrowed, env))
        goto.label = add_block(new_decincs, cache, blocks, goto.label)


def after_branch_decrefs(label: BasicBlock,
                         pre_live: AnalysisDict[Value],
                         source_borrowed: Set[Value],
                         source_live_regs: Set[Value],
                         env: Environment,
                         omitted: Iterable[Value] = ()) -> Tuple[Value, ...]:
    target_pre_live = pre_live[label, 0]
    decref = source_live_regs - target_pre_live - source_borrowed
    if decref:
        return tuple(reg
                     for reg in sorted(decref, key=lambda r: env.indexes[r])
                     if reg.type.is_refcounted and reg not in omitted)
    return ()


def after_branch_increfs(label: BasicBlock,
                         pre_borrow: AnalysisDict[Value],
                         source_borrowed: Set[Value],
                         env: Environment) -> Tuple[Value, ...]:
    target_borrowed = pre_borrow[label, 0]
    incref = source_borrowed - target_borrowed
    if incref:
        return tuple(reg
                     for reg in sorted(incref, key=lambda r: env.indexes[r])
                     if reg.type.is_refcounted)
    return ()


def add_block(decincs: DecIncs, cache: BlockCache,
              blocks: List[BasicBlock], label: BasicBlock) -> BasicBlock:
    decs, incs = decincs
    if not decs and not incs:
        return label

    # TODO: be able to share *partial* results
    if (label, decincs) in cache:
        return cache[label, decincs]

    block = BasicBlock()
    blocks.append(block)
    block.ops.extend(DecRef(reg) for reg in decs)
    block.ops.extend(IncRef(reg) for reg in incs)
    block.ops.append(Goto(label))
    cache[label, decincs] = block
    return block
