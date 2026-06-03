
import ast
import io
import tokenize


def is_docstring_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(getattr(node, "value", None), ast.Constant)
        and isinstance(node.value.value, str)
    )


def docstring_line_numbers(node: ast.AST) -> set[int]:
    lines: set[int] = set()
    for current in ast.walk(node):
        if not isinstance(current, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not current.body or not is_docstring_expr(current.body[0]):
            continue
        first = current.body[0]
        start = getattr(first, "lineno", 0)
        end = getattr(first, "end_lineno", start)
        lines.update(range(start, end + 1))
    return lines


def physical_span(node: ast.AST) -> int:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    return max(0, end - start + 1)


def comment_line_numbers(text: str) -> set[int]:
    comment_lines: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    except tokenize.TokenError:
        return comment_lines
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        prefix = token.line[: token.start[1]]
        if prefix.strip():
            continue
        comment_lines.add(token.start[0])
    return comment_lines


def non_code_line_numbers(text: str, tree: ast.AST) -> set[int]:
    return docstring_line_numbers(tree) | comment_line_numbers(text)
