# LLM: 只做固定mock和有界AST解释；不使用eval/exec/compile、不调用用户函数、不取属性、不访问外部。
# 模块用途: 为通用调试提供纯内存表达式和唯一可在默认副作用门内运行的分析预览。
from __future__ import annotations

import ast
import json
import operator

from .models import payload

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Mod: operator.mod}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.In: operator.contains}


# LLM: 仅JSON内存复制，无LLM/网络/脚本；引擎按函数对象身份允许默认预览，不能用分析器自报纯属性替代。
# 函数用途: 返回标明mock/llm_called=False的确定性分析预览，支持config.mock_output或渲染输入。
def analysis_preview(spec, context, resolved_skills):
    output = spec.get('config', {}).get('mock_output', context.step_inputs)
    if not isinstance(output, dict):
        raise ValueError('mock_output必须是对象')
    return {'mock': True, 'llm_called': False, 'output': payload(output)}


analysis_preview.llm_involved = False


# LLM: AST最多128节点/4096字符，所有中间值有界；不支持幂、格式化%、调用、属性或隐式Python对象。
# 函数用途: 计算inputs上的JSON纯表达式，失败抛ValueError交给原debug错误合同。
def evaluate_expression(expression, inputs):
    if type(expression) is not str or not 0 < len(expression) <= 4096:
        raise ValueError('表达式长度无效')
    tree = ast.parse(expression, mode='eval')
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError('表达式过于复杂')
    result = _evaluate(tree.body, payload(inputs))
    if len(str(result)) > 65536:
        raise ValueError('表达式输出过大')
    return payload(result)


# LLM: 每个递归结果限制JSON字节和整数位数，防多层容器重复放大；解析始终只处理JSON，禁止任意对象协议。
# 函数用途: 对表达式中间值施加统一预算，不等最终输出形成后才检查。
def _evaluate(node, inputs):
    value = _evaluate_node(node, inputs)
    if type(value) is int and value.bit_length() > 4096 or len(json.dumps(value, allow_nan=False)) > 65536:
        raise ValueError('表达式中间值过大')
    return value


# LLM: 复制运算先按完整序列化成本检查，不按顶层len低估嵌套容器；未知AST立即失败且不调用外部回调。
# 函数用途: 解释允许的JSON字面量、输入引用、算术、逻辑与比较，不执行用户代码。
def _evaluate_node(node, inputs):
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
        return node.value
    if isinstance(node, ast.Name) and node.id == 'inputs':
        return inputs
    if isinstance(node, ast.Subscript):
        return _evaluate(node.value, inputs)[_evaluate(node.slice, inputs)]
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise ValueError('不允许展开映射')
        return {_evaluate(key, inputs): _evaluate(value, inputs) for key, value in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_evaluate(item, inputs) for item in node.elts]
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, inputs)
        if isinstance(node.op, ast.Not):
            return not value
        if type(value) in (int, float) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BoolOp):
        values = []
        for item in node.values:
            value = _evaluate(item, inputs)
            values.append(value)
            if isinstance(node.op, ast.And) and not value or isinstance(node.op, ast.Or) and value:
                return value
        return values[-1]
    if isinstance(node, ast.IfExp):
        return _evaluate(node.body if _evaluate(node.test, inputs) else node.orelse, inputs)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _evaluate(node.left, inputs), _evaluate(node.right, inputs)
        if isinstance(node.op, ast.Mod) and (type(left) not in (int, float) or type(right) not in (int, float)):
            raise ValueError('不允许格式字符串')
        if isinstance(node.op, ast.Mult):
            sequence, count = (left, right) if isinstance(left, (str, list)) else (right, left)
            if isinstance(sequence, (str, list)) and (type(count) is not int
                    or len(json.dumps(sequence, allow_nan=False)) * max(0, count) > 65536):
                raise ValueError('表达式中间值过大')
        if type(left) is int and type(right) is int and left.bit_length() + right.bit_length() > 4096:
            raise ValueError('整数过大')
        if isinstance(node.op, ast.Add) and isinstance(left, (str, list)) and len(left) + len(right) > 65536:
            raise ValueError('表达式中间值过大')
        return _BINARY[type(node.op)](left, right)
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, inputs)
        for operation, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, inputs)
            if type(operation) not in _COMPARE:
                raise ValueError('不支持的比较')
            matched = operator.contains(right, left) if isinstance(operation, ast.In) else _COMPARE[type(operation)](left, right)
            if not matched:
                return False
            left = right
        return True
    raise ValueError('仅支持纯JSON表达式')
