
from __future__ import annotations

"""AST-level checks for functions, classes, parameters, and nesting."""

import ast

from code_size_docstrings import docstring_line_numbers, physical_span
from code_size_rules import (
    CLASS_HARD_LIMIT,
    CLASS_SOFT_LIMIT,
    FUNCTION_HARD_LIMIT,
    FUNCTION_SOFT_LIMIT,
    MIXIN_HARD_LIMIT,
    MIXIN_SOFT_LIMIT,
    NESTING_HARD_LIMIT,
    NESTING_SOFT_LIMIT,
    PARAM_HARD_LIMIT,
    PARAM_SOFT_LIMIT,
    Finding,
)
from code_size_thresholds import (
    FindingInput,
    LimitFindingInput,
    is_near_soft,
    limit_finding,
    near_soft_finding,
)


def node_span(node: ast.AST, ignored_lines: set[int] | None = None) -> int:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    ignored = set(docstring_line_numbers(node))
    if ignored_lines:
        ignored.update(line for line in ignored_lines if start <= line <= end)
    return max(0, physical_span(node) - len(ignored))


def check_function_node(
    rel: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    ignored_lines: set[int] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    span = node_span(node, ignored_lines)
    if span > FUNCTION_SOFT_LIMIT:
        findings.append(
            limit_finding(
                LimitFindingInput(
                    FindingInput("function", rel, node.name, span, FUNCTION_SOFT_LIMIT, "function too long"),
                    FUNCTION_HARD_LIMIT,
                )
            )
        )
    elif is_near_soft(span, FUNCTION_SOFT_LIMIT):
        findings.append(near_soft_finding(FindingInput("function", rel, node.name, span, FUNCTION_SOFT_LIMIT, "function approaching soft limit")))

    params = arg_count(node)
    if params > PARAM_SOFT_LIMIT:
        findings.append(limit_finding(LimitFindingInput(FindingInput("params", rel, node.name, params, PARAM_SOFT_LIMIT, "too many parameters"), PARAM_HARD_LIMIT)))
    elif is_near_soft(params, PARAM_SOFT_LIMIT):
        findings.append(near_soft_finding(FindingInput("params", rel, node.name, params, PARAM_SOFT_LIMIT, "parameter count approaching soft limit")))

    nesting = max_nesting(node)
    if nesting > NESTING_SOFT_LIMIT:
        findings.append(limit_finding(LimitFindingInput(FindingInput("nesting", rel, node.name, nesting, NESTING_SOFT_LIMIT, "nesting too deep"), NESTING_HARD_LIMIT)))
    elif is_near_soft(nesting, NESTING_SOFT_LIMIT):
        findings.append(near_soft_finding(FindingInput("nesting", rel, node.name, nesting, NESTING_SOFT_LIMIT, "nesting depth approaching soft limit")))
    return findings


def check_class_node(rel: str, node: ast.ClassDef, ignored_lines: set[int] | None = None) -> list[Finding]:
    span = node_span(node, ignored_lines)
    is_mixin = node.name.endswith("Mixin")
    kind = "mixin" if is_mixin else "class"
    soft = MIXIN_SOFT_LIMIT if is_mixin else CLASS_SOFT_LIMIT
    hard = MIXIN_HARD_LIMIT if is_mixin else CLASS_HARD_LIMIT
    if span > soft:
        message = "mixin too long" if is_mixin else "class too long"
        return [limit_finding(LimitFindingInput(FindingInput(kind, rel, node.name, span, soft, message), hard))]
    if is_near_soft(span, soft):
        message = "mixin approaching soft limit" if is_mixin else "class approaching soft limit"
        return [near_soft_finding(FindingInput(kind, rel, node.name, span, soft, message))]
    return []


def arg_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = node.args
    positional_args = list(args.posonlyargs) + list(args.args)
    implicit_receiver = 1 if positional_args and positional_args[0].arg in {"self", "cls"} else 0
    arg_names = {item.arg for item in positional_args} | {item.arg for item in args.kwonlyargs}
    if arg_names & {"params", "options", "request"}:
        # but the service-facing contract is the single params/options/request bundle.
        return (
            len(positional_args)
            + 1
            + (1 if args.vararg else 0)
            + (1 if args.kwarg else 0)
            - implicit_receiver
        )
    return (
        len(positional_args)
        + len(args.kwonlyargs)
        + (1 if args.vararg else 0)
        + (1 if args.kwarg else 0)
        - implicit_receiver
    )


def max_nesting(node: ast.AST) -> int:
    branch_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)

    def walk(current: ast.AST, depth: int) -> int:
        next_depth = depth + 1 if isinstance(current, branch_nodes) else depth
        child_depths = [walk(child, next_depth) for child in ast.iter_child_nodes(current)]
        return max([next_depth, *child_depths])

    return walk(node, 0)
