"""参数中心阶段 3：同一个数值常数名只允许在一个模块里定义（2026-09-27）。

背景：用户指出参数散落、同名不同义、改一处还要猜含义。盘点时 32 个常数名在多个文件里各定义一份，其中一部分是同一概念
写了多份（改一处漏一处就会漂移），一部分是名字撞了但含义不同（搜索时容易找错）。第一批已把同一概念收成一处定义、删掉死常数。

锁定：产品代码（agent_py_agent/agent 与 agent_py_agent/cli）里模块级的数值常数，按去掉前导下划线后的名字，只能在一个模块定义；
导入不算定义。确实各有含义的同名常数必须写进 _ALLOWED 并说明原因；名单里的名字不再重复时也失败，逼着及时删掉过期条目。
新增常数撞名时：同一概念就从已有模块导入，不同概念就起一个说明用途的具体名字。
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCANNED = ("agent", "cli")
_NAME = re.compile(r"^_*[A-Z][A-Z0-9_]*$")

# 名字相同但含义各自不同的常数：键是去掉前导下划线的名字，值是原因。合并或改名后必须删掉对应条目。
_ALLOWED = {
    "BODY_MAX_CHARS": "自学习提案正文上限与聊天 /skills 回执的正文展示上限，用途不同",
    "DESCRIPTION_MAX_CHARS": "Skill 索引里的描述上限与自学习提案草稿的描述上限，用途不同",
    "MAX_BODY_BYTES": "网页工具与采集源各自的 HTTP 响应体上限，分属两个子系统，可以独立调整",
    "MAX_CANDIDATES": "产物定位与插件观察两处各自的候选数上限（决策点的数量界限统一在 decision_point_limits）",
    "MAX_HINT_CHARS": "各决策点提示文字的上限按点位设定；决策点参数统一时一并处理",
    "MAX_INLINE_JSON": "工具动作摘要与编排实时摘要各自的内联 JSON 上限",
    "MAX_OWNERS": "管理员控制工具与审计记录工具各自一次列出的用户数上限",
    "MAX_RECORDS": "工具护栏记录与决策结果日志各自的保留条数",
    "POLL_SECONDS": "Gateway 重启等待、TUI 插件面板、升级跟随三种不同的轮询节奏",
    "PREVIEW_CHARS": "会话搜索结果预览与本地存储正文预览，用途不同",
    "PROBE_MAX_ATTEMPTS": "模型 HTTP 连接探测与看图能力探测各自的重试次数",
    "SALT_BYTES": "会话锁与管理员密码是两套独立的散列存储，参数随散列落盘；要统一须整体换成共用散列函数",
    "SCHEMA_VERSION": "各持久格式自己的版本号，互不相关",
    "SCRYPT_N": "同 SALT_BYTES：两套独立散列存储各自的 scrypt 参数",
    "SCRYPT_P": "同 SALT_BYTES：两套独立散列存储各自的 scrypt 参数",
    "SCRYPT_R": "同 SALT_BYTES：两套独立散列存储各自的 scrypt 参数",
    "TIMEOUT_S": "飞书卡片与飞书用户资料两个接口各自的请求超时",
    "WRITE_TIMEOUT_SECONDS": "命令流与 PTY 会话各自的写超时",
}


# 函数用途: 判断赋值右边是不是纯数值表达式（字面量、正负号、由字面量组成的四则与乘方），布尔不算。
def _is_numeric(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_numeric(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_numeric(node.left) and _is_numeric(node.right)
    return False


# 函数用途: 列出一个模块顶层定义的数值常数名（去掉前导下划线）；导入、函数内与类内的赋值不算。
def _module_constants(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target, value = node.target.id, node.value
        else:
            continue
        if _NAME.match(target) and _is_numeric(value):
            names.add(target.lstrip("_"))
    return names


# 函数用途: 扫描产品代码，返回在两个及以上模块里定义的常数名及其所在文件。
def _duplicated_constants() -> dict[str, list[str]]:
    owners: dict[str, list[str]] = defaultdict(list)
    for package in _SCANNED:
        for path in sorted((_ROOT / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name in _module_constants(tree):
                owners[name].append(str(path.relative_to(_ROOT)))
    return {name: files for name, files in owners.items() if len(files) > 1}


def test_numeric_constant_names_are_defined_in_one_module_only():
    duplicated = _duplicated_constants()
    unexpected = {name: files for name, files in duplicated.items() if name not in _ALLOWED}
    assert not unexpected, (
        "这些数值常数名在多个模块里各定义了一份；同一概念请从已有模块导入，不同概念请改成说明用途的具体名字，"
        f"确属不同含义才写进 _ALLOWED 并说明原因：{unexpected}"
    )
    stale = sorted(set(_ALLOWED) - set(duplicated))
    assert not stale, f"这些名字已经不再重复，请从 _ALLOWED 删掉：{stale}"


def test_scanner_counts_definitions_not_imports():
    tree = ast.parse(
        "from x import LIMIT\n_TIMEOUT_SECONDS = 15\nMAX_BYTES = 8 * 1024 * 1024\nFLAG = True\nNAME = 'a'\n"
        "def f():\n    INNER = 3\n"
    )
    assert _module_constants(tree) == {"TIMEOUT_SECONDS", "MAX_BYTES"}
