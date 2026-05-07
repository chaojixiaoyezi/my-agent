# LLM: Shared code-size line accounting helpers; keep these pure across every checker.
# 模块用途: 统一找出 docstring、整行注释和物理跨度，避免维护说明文字被误算成实现代码。

import ast
import io
import tokenize


# LLM: is_docstring_expr 识别 AST docstring 节点。
# 函数用途: 判断节点是否是模块、类或函数 body 开头的字符串表达式。
def is_docstring_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(getattr(node, "value", None), ast.Constant)
        and isinstance(node.value.value, str)
    )


# LLM: docstring_line_numbers 统计 docstring 物理行。
# 函数用途: 遍历模块、类和函数节点，收集首个 docstring 覆盖的行号。
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


# LLM: physical_span 读取 AST 节点物理跨度。
# 函数用途: 根据 lineno/end_lineno 计算节点覆盖的源文件行数。
def physical_span(node: ast.AST) -> int:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    return max(0, end - start + 1)


# LLM: comment_line_numbers 统计整行注释。
# 函数用途: 用 tokenize 找出不跟随代码的注释行，内联注释不计入。
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


# LLM: non_code_line_numbers 合并非实现行口径。
# 函数用途: 返回 docstring 行和整行注释行，供文件/节点规模检查扣除。
def non_code_line_numbers(text: str, tree: ast.AST) -> set[int]:
    return docstring_line_numbers(tree) | comment_line_numbers(text)
