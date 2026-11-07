"""W2 纯规则评估；只消费快照，不访问文件、网络或时钟。

rules/task_rules 可混合 PolicyRule 与 ScopeRule；范围规则使用 kind/pattern。
规则来源返回 global/task。facts.paths 的符号链接由调用方解析，本模块只做
POSIX 词法规范化。TaskScope 相对子路径需要可选 facts.workspace_root；缺少
根目录时拒绝涉及该限制的路径操作，不从进程 cwd 或路径文本猜测工作区。
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from typing import Any


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """不可变裁决；reasons 也使用元组，避免调用方改写已形成的证据。"""

    decision: str
    rule_id: str = ""
    rule_source: str = ""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Candidate:
    decision: str
    rule_id: str = ""
    rule_source: str = ""
    reason: str = ""
    priority: int = 0
    argv_length: int = 0
    path_length: int = 0

    def order(self) -> tuple[Any, ...]:
        # ask 与 judge 同级；末尾 decision 只消除重复 ID 的畸形输入歧义。
        return (
            {"deny": 0, "ask": 1, "judge": 1, "allow": 2}[self.decision],
            {"task": 0, "global": 1}.get(self.rule_source, 2),
            -self.priority, -self.argv_length, -self.path_length,
            self.rule_id, self.decision,
        )


def _pick(current: _Candidate | None, candidate: _Candidate | None) -> _Candidate | None:
    # 热路径仅保留单个最小值，不对全部命中规则排序。
    if candidate is not None and (current is None or candidate.order() < current.order()):
        return candidate
    return current


def _result(candidate: _Candidate | None, warnings: tuple[str, ...] = ()) -> PolicyDecision:
    if candidate is None:
        return PolicyDecision("allow", reasons=warnings)
    reasons = ((candidate.reason,) if candidate.reason else ()) + warnings
    return PolicyDecision(candidate.decision, candidate.rule_id, candidate.rule_source, reasons)


def _iter_rules(
    snapshot: Mapping[str, Any], facts: Mapping[str, Any]
) -> Iterator[tuple[Mapping[str, Any], str]]:
    task_id = facts.get("task_id", "")
    for key, source in (("rules", "global"), ("task_rules", "task")):
        for rule in snapshot.get(key, ()):
            if not isinstance(rule, Mapping) or not rule.get("enabled", True):
                continue
            bound_task = rule.get("task_id", "")
            if bound_task and bound_task != task_id:
                continue
            if source == "task" and not task_id:
                continue
            yield rule, "task" if bound_task else source


def _path(value: Any) -> str:
    if not isinstance(value, str) or "\0" in value or not posixpath.isabs(value):
        return ""
    return posixpath.normpath(value)


def _domain(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        domain = value.lower().encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return ""
    if len(domain) > 253 or any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label)
        for label in domain.split(".")
    ):
        return ""
    return domain


def _ancestors(value: str, family: str) -> Iterator[str]:
    if family == "domain":
        while value:
            yield value
            _, separator, value = value.partition(".")
            if not separator:
                break
    else:
        # 完整分段祖先避免 /repo 误匹配 /repository。
        while value:
            yield value
            parent = posixpath.dirname(value)
            if parent == value:
                break
            value = parent
        if value.startswith("//"):
            yield "/"


def _within(value: str, prefix: str) -> bool:
    if not value or not prefix:
        return False
    if prefix in {"/", "//"}:
        return value.startswith(prefix)
    return value == prefix or value.startswith(prefix + "/")


def _argv(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def _commands(facts: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    # 同时核对入口 argv 与已解析的各子命令，commands=[] 不能隐去入口。
    commands = []
    argv = _argv(facts.get("argv", ()))
    if argv:
        commands.append(argv)
    for value in facts.get("commands", ()):
        command = _argv(value)
        if command:
            commands.append(command)
    return tuple(commands)


def evaluate_command_rules(
    snapshot: Mapping[str, Any], facts: Mapping[str, Any]
) -> PolicyDecision:
    """按危险度、来源、优先级、前缀长度和 ID 线性挑选命令规则。"""
    best = None
    commands = _commands(facts)
    paths = tuple(_path(value) for value in facts.get("paths", ()))
    for rule, source in _iter_rules(snapshot, facts):
        decision = rule.get("decision")
        if rule.get("kind") or decision not in {"allow", "ask", "judge", "deny"}:
            continue
        tool_pattern = rule.get("tool", "")
        if not isinstance(tool_pattern, str) or not fnmatchcase(facts.get("tool", ""), tool_pattern):
            continue
        prefix = _argv(rule.get("command_prefix", ()))
        if prefix is None:
            continue
        argv0 = rule.get("command_argv0", "")
        if (argv0 or prefix) and not any(
            (not argv0 or command[0] == argv0) and command[:len(prefix)] == prefix
            for command in commands
        ):
            continue
        raw_path = rule.get("path_prefix", "")
        path_prefix = _path(raw_path) if raw_path else ""
        if raw_path and (not path_prefix or not any(_within(path, path_prefix) for path in paths)):
            continue
        rule_id = rule.get("rule_id", "")
        reason = f"命中{source}命令规则 {rule_id}"
        if rule.get("reason"):
            reason += f"：{rule['reason']}"
        best = _pick(best, _Candidate(
            decision, rule_id, source, reason, rule.get("priority", 0),
            len(prefix), len(path_prefix),
        ))
    return _result(best)


@dataclass(slots=True)
class _ScopeIndex:
    """规范前缀索引；按资源深度查哈希表，不在规则间做嵌套全扫。"""

    entries: dict[str, _Candidate] = field(default_factory=dict)
    best: _Candidate | None = None

    def add(self, pattern: str, candidate: _Candidate) -> None:
        self.entries[pattern] = _pick(self.entries.get(pattern), candidate)
        self.best = _pick(self.best, candidate)

    def match(self, value: str, family: str) -> _Candidate | None:
        best = None
        for prefix in _ancestors(value, family):
            best = _pick(best, self.entries.get(prefix))
        return best


def _scope_candidate(rule: Mapping[str, Any], source: str, pattern: str) -> _Candidate:
    kind = rule["kind"]
    reason = f"命中{source}范围规则 {rule.get('rule_id', '')} ({kind})"
    if rule.get("note"):
        reason += f"：{rule['note']}"
    return _Candidate(
        "allow" if kind.endswith("_allow") else "deny", rule.get("rule_id", ""),
        source, reason, rule.get("priority", 0),
        path_length=len(pattern) if kind.startswith("path_") else 0,
    )


_SCOPE_KINDS = ("path_allow", "path_deny", "domain_allow", "domain_deny")


def _scope_indexes(snapshot, facts, resources):
    indexes = {(source, kind): _ScopeIndex() for source in ("global", "task") for kind in _SCOPE_KINDS}
    best = None
    for rule, source in _iter_rules(snapshot, facts):
        kind = rule.get("kind")
        if kind not in _SCOPE_KINDS:
            continue
        family = kind.split("_", 1)[0]
        raw = rule.get("pattern")
        # 前导点是域名后缀标记，不属于主机名；事实仍严格禁止空标签。
        if family == "domain" and isinstance(raw, str) and raw.startswith("."):
            raw = raw[1:]
        pattern = (_path if family == "path" else _domain)(raw)
        candidate = _scope_candidate(rule, source, pattern)
        if not pattern:
            # 非法的限制不能被当作“不存在”进而意外放行；契约校验仍由存储入口负责。
            if resources[family]:
                best = _pick(best, replace(candidate, decision="deny", reason=f"范围规则 {candidate.rule_id} 的 {kind} pattern 无效"))
            continue
        indexes[source, kind].add(pattern, candidate)

    warnings = []
    for family in ("path", "domain"):
        parent = indexes["global", family + "_allow"]
        child = indexes["task", family + "_allow"]
        if not parent.entries:
            continue
        confined = _ScopeIndex()
        widened = False
        for pattern, candidate in child.entries.items():
            if parent.match(pattern, family) is None:
                widened = True
            else:
                confined.add(pattern, candidate)
        indexes["task", family + "_allow"] = confined
        if widened:
            # 每类只输出一条警告，顺序固定；不为排列稳定性排序整份规则集合。
            warnings.append(f"已忽略试图拓宽 global {family}_allow 边界的任务 allow 规则")
    return indexes, best, tuple(warnings)


def _names(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))


def _tool_constraints(snapshot, facts, best):
    tool = facts.get("tool", "")
    # 缺字段表示调用方未提供此层能力门；显式空集合表示没有任何已有工具。
    if "allowed_tools" in facts and tool not in _names(facts["allowed_tools"]):
        best = _pick(best, _Candidate("deny", reason="工具不在 facts.allowed_tools 已有能力集合中"))
    task_id = facts.get("task_id", "")
    for constraint in snapshot.get("constraints", ()):
        if not isinstance(constraint, Mapping) or not task_id or constraint.get("task_id") != task_id:
            continue
        kind = constraint.get("kind")
        if kind not in {"tool_allowlist", "tool_denylist"}:
            continue
        listed = tool in _names(constraint.get("tools"))
        denied = (kind == "tool_allowlist" and not listed) or (kind == "tool_denylist" and listed)
        if denied or kind == "tool_allowlist":
            # 多个 allowlist 逐条相交；任何任务工具清单都不能扩充 inherited tools。
            candidate = _Candidate(
                "deny" if denied else "allow", constraint.get("constraint_id", ""), "task",
                f"任务工具约束 {constraint.get('constraint_id', '')} ({kind})" + (" 拒绝该工具" if denied else " 允许该已有工具"),
            )
            best = _pick(best, candidate)
    return best


def _task_scope(snapshot, facts, resources, indexes, best):
    scope = snapshot.get("task_scope")
    if not isinstance(scope, Mapping) or not facts.get("task_id") or scope.get("task_id") != facts.get("task_id"):
        return best
    subpaths = scope.get("workspace_subpaths", ())
    if subpaths and resources["path"]:
        root = _path(facts.get("workspace_root"))
        restricted = _ScopeIndex()
        invalid = not root
        for subpath in subpaths:
            if (
                not isinstance(subpath, str) or not subpath or subpath.startswith(("/", "-"))
                or "\0" in subpath or ".." in subpath.split("/")
            ):
                invalid = True
                continue
            target = posixpath.normpath(posixpath.join(root, subpath)) if root else ""
            if not _within(target, root):
                invalid = True
            else:
                restricted.add(target, _Candidate("allow"))
        if invalid:
            best = _pick(best, _Candidate("deny", rule_source="task", reason="task_scope 子路径无效或缺少绝对 facts.workspace_root"))
        elif any(restricted.match(path, "path") is None for path in resources["path"]):
            best = _pick(best, _Candidate("deny", rule_source="task", reason="路径超出 task_scope.workspace_subpaths"))
    if resources["domain"]:
        network = scope.get("network", "inherit")
        has_allowlist = any(indexes[source, "domain_allow"].entries for source in ("global", "task"))
        if network == "none" or (network == "allowlist" and not has_allowlist):
            best = _pick(best, _Candidate("deny", rule_source="task", reason=f"task_scope.network={network} 不允许本次域名访问"))
    return best


def evaluate_scope_rules(
    snapshot: Mapping[str, Any], facts: Mapping[str, Any]
) -> PolicyDecision:
    """范围 deny 取并集，allow 逐层收窄，且每个资源都必须符合限制。

    缺少规则默认 allow。工具能力只做交集；task_scope 空子路径表示继承，
    network=allowlist 则必须存在适用的 domain_allow 清单。
    """
    resources = {
        "path": tuple(_path(value) for value in facts.get("paths", ())),
        "domain": tuple(_domain(value) for value in facts.get("domains", ())),
    }
    indexes, best, warnings = _scope_indexes(snapshot, facts, resources)
    for family in ("path", "domain"):
        for value in resources[family]:
            if not value:
                best = _pick(best, _Candidate("deny", reason=f"facts 中的 {family} 资源无效"))
                continue
            for source in ("global", "task"):
                best = _pick(best, indexes[source, family + "_deny"].match(value, family))
                allowlist = indexes[source, family + "_allow"]
                if not allowlist.entries:
                    continue
                allowed = allowlist.match(value, family)
                if allowed is not None:
                    best = _pick(best, allowed)
                else:
                    # 对多目标调用，某个目标的 allow 不能掩盖另一个目标的越界。
                    denied = replace(
                        allowlist.best, decision="deny",
                        reason=f"{family} 资源不在 {source} {family}_allow 清单内",
                    )
                    best = _pick(best, denied)
    best = _tool_constraints(snapshot, facts, best)
    best = _task_scope(snapshot, facts, resources, indexes, best)
    return _result(best, warnings)
