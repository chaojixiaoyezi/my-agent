#!/usr/bin/env python3
"""Boolean-switch Binary Law + hot-reload gate (stdlib only, AST + shipped config).

Binary Law (opus-boolean-switch-binary-law-audit, anchor main@09114315c):
  State A  a real runtime policy toggle: config field + shipped YAML key + parameter_registry entry,
           declared in settings/live_switches.py:LIVE_SWITCH_KEYS, registry effect "immediate", refreshed
           by refresh_live_switches() at the hook points, never frozen at construction time.
           Deployment-tier toggles are additionally declared in DEPLOY_LIVE_SWITCH_KEYS; they stay
           writable only by the deployment file / `my-agent config set` (CLI_DEPLOYMENT_WRITABLE_KEYS),
           never by /settings or the model.
  State B  internal performance / cache / index / connection reuse / bounded scan / dead switch:
           no switch and no slow fallback. A parameter kept for signature compatibility defaults to the
           fast value, and production callers never pass the slow value.

Exit codes: 0 clean (or only baselined violations), 1 new violations / baseline misuse, 2 gate cannot
resolve a declaration it needs (declarations must stay static literals).

Usage:
  python3 scripts/check_boolean_switches_and_hot_reload.py            # CI / pre-push (baseline aware)
  python3 scripts/check_boolean_switches_and_hot_reload.py --strict   # ignore baseline
  python3 scripts/check_boolean_switches_and_hot_reload.py --json     # machine readable report
  python3 scripts/check_boolean_switches_and_hot_reload.py --prune-baseline   # drop fixed entries only
  python3 scripts/check_boolean_switches_and_hot_reload.py --write-baseline   # only when no baseline exists
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PKG = "agent_py_agent"
TESTS_PREFIX = "agent_py_agent/tests/"
BASELINE_REL = "agent_py_agent/tests/fixtures/boolean_switch_pending_fixes.json"
BASELINE_SCHEMA = "boolean_switch_pending_fixes.v1"

CONFIG_CLASSES = {
    "agent_py_agent/agent/settings/config.py": (
        "AgentConfig", "_HomeProviderConfigFields", "_ToolConfigFields", "_RuntimeBudgetConfigFields"),
    "agent_py_agent/agent/capability/config.py": ("CapabilityConfig",),
    "agent_py_agent/agent/settings/deployment_policy.py": ("RuntimeGuardPolicy",),
}
FAMILY = {
    "agent_py_agent/agent/settings/config.py": "agent",
    "agent_py_agent/agent/capability/config.py": "capability",
    "agent_py_agent/agent/settings/deployment_policy.py": "runtime_guard",
}
YAML_FILES = {
    "agent": "agent_py_agent/config/agent_config.yaml",
    "capability": "agent_py_agent/config/capability_config.yaml",
}
LIVE_MODULE = "agent_py_agent/agent/settings/live_switches.py"
REGISTRY = "agent_py_agent/agent/settings/parameter_registry.py"
CHANGES = "agent_py_agent/agent/settings/parameter_changes.py"
UCC = "agent_py_agent/agent/settings/user_config_capability.py"
CAP_RELOAD = "agent_py_agent/agent/capability/runtime_config_reload.py"

# Closed State-B config switches (AgentConfig family + ToolRegistryParams). Each must disappear from the
# dataclass, the shipped YAML, the registry key lists and every production read.
STATE_B_CONFIG_KEYS = {
    "decision_connection_reuse_enabled": "connection reuse (P1: default False pays a TLS/proxy handshake per decision call)",
    "sqlite_connection_reuse": "connection reuse",
    "background_tick_cheap_checks": "cheap wake-probe switch for the background tick",
    "local_store_fts_enabled": "FTS index switch (LocalStore(enable_fts=True) stays the constructor default)",
    "thread_prompt_prefix_freeze_enabled": "prompt-cache prefix stability; persona deltas still reach the model",
    "tool_default_deferral_enabled": "tool catalog deferral (context economy); fast path is always-on",
    "memory_context_calibration_carry_enabled": "owner calibration-ratio cache carry",
    "catalog_default_deferral_enabled": "ToolRegistryParams mirror of tool_default_deferral_enabled",
    "catalog_show_truncated_notice": "dead ToolRegistryParams switch (config key already deleted, fixed on)",
}
# Fast-path vocabulary: a bool whose True value selects the cheap path. Slow-path vocabulary: True = slow.
FAST_TOKENS = frozenset({
    "cheap", "bounded", "budget", "lazy", "defer", "deferral", "reuse", "pool", "pooled", "cache", "cached",
    "caching", "fts", "prefetch", "memo", "memoize", "memoized", "incremental", "keepalive", "freeze", "fast",
})
SLOW_TOKENS = frozenset({"eager", "uncached", "rescan", "fullscan"})
SLOW_NAMES = frozenset({"include_archives", "full_scan", "force_full_scan", "no_cache", "bypass_cache",
                        "force_rescan", "eager_handlers", "eager_connect"})
# Pure fast-path parameters: production callers may only omit them or pass literal True.
PURE_FAST_PARAMS = frozenset({"cheap_checks", "bounded", "scan_budget", "lazy_handlers", "defer_mcp_connect"})

# Semantic names that share fast-path vocabulary but change observable behaviour (closed, with reasons).
SEMANTIC_NAME_EXCEPTIONS = {
    "anthropic_prompt_cache_enabled": "provider wire-compat: some Anthropic-compatible endpoints reject cache_control; model-profile scoped",
    "owner_quota_policy_available": "capability fact, not an optimisation",
    "budget_exhausted": "result state, not a switch",
    "cache_suspected": "model-call ledger diagnostic fact, not a switch",
}
# Semantic function parameters (file, qualname, param) -> reason. Closed: adding one is a reviewed change.
_A = "agent_py_agent/agent/"
SEMANTIC_PARAM_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (_A + "artifacts/shell_protection.py", "reconcile_shell_artifacts", "defer_unchanged_cleanup"):
        "artifact cleanup timing chosen by the caller; not output-equivalent",
    (_A + "backends/anthropic_prompt_cache.py", "anthropic_messages_with_optional_cache", "stable_system_cache_active"):
        "caller-supplied fact about the request's cache segment",
    (_A + "backends/anthropic_prompt_cache.py", "anthropic_native_payload_with_cache", "stable_system_cache_active"):
        "caller-supplied fact about the request's cache segment",
    (_A + "backends/gateway_helpers.py", "provider_attempt_observer", "cache_diagnostics"):
        "observability output selector for one provider thread",
    (_A + "backends/responses_ws_pool.py", "cache_usage", "incremental"):
        "describes the response mode being accounted",
    (_A + "conversation/agent_thread.py", "prepare_subagent_thread_turn", "defer_compact"):
        "compaction timing contract owned by the caller",
    (_A + "memory_store/decision_recall.py", "supplement_recalled_memories", "defer_access"):
        "access bookkeeping ownership moves to the caller; not output-equivalent",
    (_A + "runtime_db/repository.py", "RuntimeRepository.create_attempt", "reuse_pending"):
        "idempotent attempt-reuse semantics",
    ("agent_py_agent/cli/chat_parts/tui_runtime.py", "TuiRuntime.begin_turn", "defer_assistant_display"): "UI display ordering",
    ("agent_py_agent/cli/chat_parts/tui_runtime.py", "TuiTurnEventAdapter.__init__", "defer_assistant_display"): "UI display ordering",
    ("agent_py_agent/cli/gateway_worker_runtime.py", "CoordinatorWorkers.close", "defer_native"): "shutdown ordering",
}
# Module-level bool constants that are real CLI defaults (overridable by flags), not switches.
MODCONST_ALLOWLIST: dict[tuple[str, str], str] = {}
# Production reads of live switches at construction time that are legitimate (topology facts, validation).
SNAP_ALLOWLIST: dict[tuple[str, str, str], str] = {}

# Construction-time functions whose reads freeze a live switch for the lifetime of the agent.
CONSTRUCTION_FUNCS = frozenset({
    ("agent_py_agent/agent/core.py", "_build_tool_registry"),
    ("agent_py_agent/agent/core.py", "_register_orchestration_tools"),
    ("agent_py_agent/agent/core.py", "_initialize_agent_construction"),
    ("agent_py_agent/agent/core.py", "_tool_registry_mcp_servers"),
})
SNAP_HELPERS = frozenset({"_audit_enabled"})
HOOKS = (
    (CHANGES, "set_parameter"),
    (CHANGES, "reset_parameter"),
    (CHANGES, "revert_change"),
    ("agent_py_agent/agent/agent_core/runtime_mixin.py", "_run_once_with_params"),
    ("agent_py_agent/agent/auth/manager.py", None),
    ("agent_py_agent/cli/daemon.py", None),
)
HOOK_NAME = "refresh_live_switches"
PARAM_CLASS_RE = re.compile(r"(Params|Policy|Options|Settings|Config)$")
MODCONST_RE = re.compile(r"(^|_)(ENABLED|ENABLE|DISABLED|DISABLE)$")
ID_RE = re.compile(r"^[A-Z]-[A-Z-]+\|[^|]+\|[^|]*\|[^|]+$")


class GateError(Exception):
    pass


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    scope: str
    name: str
    message: str

    @property
    def id(self) -> str:
        return f"{self.rule}|{self.file}|{self.scope}|{self.name}"


def tokens(name: str) -> set[str]:
    return {part for part in name.lower().strip("_").split("_") if part}


def fast_named(name: str) -> bool:
    return name in PURE_FAST_PARAMS or bool(tokens(name) & FAST_TOKENS)


def slow_named(name: str) -> bool:
    parts = tokens(name)
    return name in SLOW_NAMES or bool(parts & SLOW_TOKENS) or {"full", "scan"} <= parts


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self._trees: dict[str, ast.Module | None] = {}

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def tree(self, rel: str) -> ast.Module | None:
        if rel not in self._trees:
            path = self.root / rel
            try:
                self._trees[rel] = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (OSError, SyntaxError, UnicodeDecodeError):
                self._trees[rel] = None
        return self._trees[rel]

    def production_files(self) -> list[str]:
        base = self.root / PKG
        out = []
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(self.root).as_posix()
            if rel.startswith(TESTS_PREFIX) or "/tests/" in rel:
                continue
            out.append(rel)
        return out


def module_literal(repo: Repo, rel: str, name: str, *, required: bool = True) -> set[str] | None:
    tree = repo.tree(rel)
    if tree is None:
        if required:
            raise GateError(f"cannot parse {rel}")
        return None
    env: dict[str, set[str]] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name):
                resolved = _resolve_literal(value, env)
                if resolved is not None:
                    env[target.id] = resolved
    if name in env:
        return env[name]
    if required:
        raise GateError(f"{rel}:{name} must stay a static literal (frozenset/set/dict/tuple of str, or | of such)")
    return None


def _resolve_literal(node: ast.expr | None, env: dict[str, set[str]]) -> set[str] | None:
    if node is None:
        return None
    if isinstance(node, ast.Dict):
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        return set(keys) if len(keys) == len(node.keys) else None
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        vals = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return set(vals) if len(vals) == len(node.elts) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"frozenset", "set", "tuple", "dict"}:
        if not node.args:
            return set()
        return _resolve_literal(node.args[0], env)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _resolve_literal(node.left, env), _resolve_literal(node.right, env)
        return None if left is None or right is None else left | right
    if isinstance(node, ast.Name):
        return env.get(node.id)
    return None


def class_bool_fields(repo: Repo, rel: str, class_names: tuple[str, ...] | None) -> list[dict]:
    tree = repo.tree(rel)
    if tree is None:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if class_names is not None and node.name not in class_names:
            continue
        if class_names is None and not PARAM_CLASS_RE.search(node.name):
            continue
        for item in node.body:
            if not (isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)):
                continue
            ann = ast.unparse(item.annotation)
            default, host_only = None, False
            value = item.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "field":
                for kw in value.keywords:
                    if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                        default = kw.value.value
                    if kw.arg == "metadata" and "host_only" in ast.unparse(kw.value):
                        host_only = True
            elif isinstance(value, ast.Constant):
                default = value.value
            if ann in {"bool", "bool | None", "Optional[bool]"} or isinstance(default, bool):
                out.append({"class": node.name, "name": item.target.id, "line": item.lineno,
                            "annotation": ann, "default": default, "host_only": host_only})
    return out


def yaml_top_keys(repo: Repo, rel: str) -> set[str]:
    path = repo.root / rel
    keys: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise GateError(f"missing shipped YAML {rel}") from None
    for raw in text.splitlines():
        if raw and not raw[0].isspace() and not raw.startswith("#") and ":" in raw:
            keys.add(raw.split(":", 1)[0].strip().strip("'\""))
    return keys


class ScopeVisitor(ast.NodeVisitor):
    """Walk a module keeping a qualname stack."""

    def __init__(self):
        self.stack: list[str] = []
        self.class_stack: list[ast.ClassDef] = []

    @property
    def scope(self) -> str:
        return ".".join(self.stack) or "<module>"

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.class_stack.append(node)
        self.generic_visit(node)
        self.class_stack.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self.on_function(node)
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def on_function(self, node):
        pass


def key_reads(node: ast.AST, keys: set[str]) -> list[tuple[str, int, str]]:
    """Return (key, line, kind) for attribute / getattr / .get / subscript reads of any key in `keys`."""
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load) and sub.attr in keys:
            found.append((sub.attr, sub.lineno, "attr:" + ast.unparse(sub.value)))
        elif isinstance(sub, ast.Call):
            func = sub.func
            fname = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if fname in {"getattr", "get", "pop", "setdefault"}:
                idx = 1 if fname == "getattr" else 0
                if len(sub.args) > idx and isinstance(sub.args[idx], ast.Constant) and sub.args[idx].value in keys:
                    found.append((sub.args[idx].value, sub.lineno, fname))
        elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) and sub.slice.value in keys:
            if isinstance(sub.ctx, ast.Load):
                found.append((sub.slice.value, sub.lineno, "subscript"))
    return found


def string_mentions(tree: ast.AST, keys: set[str]) -> list[tuple[str, int]]:
    out = []
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in keys:
            out.append((sub.value, sub.lineno))
        elif isinstance(sub, ast.Attribute) and sub.attr in keys:
            out.append((sub.attr, sub.lineno))
        elif isinstance(sub, ast.keyword) and sub.arg in keys:
            out.append((sub.arg, getattr(sub, "lineno", 0) or 0))
    return out


def run_gate(root: Path) -> tuple[list[Violation], dict]:
    repo = Repo(root)
    v: list[Violation] = []
    add = lambda *a: v.append(Violation(*a))  # noqa: E731

    # ---- inventory of config bools ---------------------------------------------------------------
    cfg_fields: dict[str, dict] = {}
    for rel, classes in CONFIG_CLASSES.items():
        for f in class_bool_fields(repo, rel, classes):
            f = {**f, "file": rel, "family": FAMILY[rel]}
            cfg_fields.setdefault(f["name"], f)
    if not cfg_fields:
        raise GateError("found no config bool fields; wrong --root?")
    state_b_present = {k for k in cfg_fields if k in STATE_B_CONFIG_KEYS or (
        (fast_named(k) or slow_named(k)) and k not in SEMANTIC_NAME_EXCEPTIONS)}
    state_a = {k for k in cfg_fields if k not in state_b_present}

    live_declared = repo.exists(LIVE_MODULE)
    live_keys = module_literal(repo, LIVE_MODULE, "LIVE_SWITCH_KEYS") if live_declared else set()
    deploy_live = module_literal(repo, LIVE_MODULE, "DEPLOY_LIVE_SWITCH_KEYS") if live_declared else set()
    scan_live = live_keys if live_declared else set(state_a)

    deployment_only = module_literal(repo, REGISTRY, "DEPLOYMENT_ONLY_KEYS")
    deploy_cap = module_literal(repo, CAP_RELOAD, "DEPLOYMENT_CAPABILITY_KEYS")
    tunable = module_literal(repo, UCC, "TUNABLE_KEYS")
    usb = module_literal(repo, UCC, "USER_SETTINGS_BOUNDARY_KEYS")
    cli_writable = module_literal(repo, CHANGES, "CLI_DEPLOYMENT_WRITABLE_KEYS")

    # ---- State B: config switches ------------------------------------------------------------------
    for k in sorted(state_b_present):
        f = cfg_fields[k]
        add("B-CFG", f["file"], f["class"], k, "State-B config switch: " + STATE_B_CONFIG_KEYS.get(k, "fast-path vocabulary"))
    param_fields = class_bool_fields(repo, "agent_py_agent/agent/tooling/registry.py", ("ToolRegistryParams",))
    for f in param_fields:
        if f["name"] in STATE_B_CONFIG_KEYS:
            add("B-CFG", "agent_py_agent/agent/tooling/registry.py", f["class"], f["name"],
                "State-B params switch: " + STATE_B_CONFIG_KEYS[f["name"]])
    for family, rel in YAML_FILES.items():
        for k in sorted(yaml_top_keys(repo, rel) & set(STATE_B_CONFIG_KEYS)):
            add("B-YAML", rel, "<yaml>", k, "State-B key shipped in YAML")
    state_b_all = set(STATE_B_CONFIG_KEYS)
    for rel in repo.production_files():
        tree = repo.tree(rel)
        if tree is None:
            continue
        if rel in CONFIG_CLASSES:
            continue  # the declaration itself is reported by B-CFG
        seen = set()
        for key, line in sorted(string_mentions(tree, state_b_all), key=lambda item: item[1]):
            if key in seen:
                continue
            seen.add(key)
            add("B-READ", rel, "<file>", key, f"production code still names State-B switch (first at line {line})")

    # ---- State B: dataclass fields / function parameters / calls / probes / module constants -------
    for rel in repo.production_files():
        tree = repo.tree(rel)
        if tree is None:
            continue
        for f in class_bool_fields(repo, rel, None):
            if rel in CONFIG_CLASSES and f["class"] in CONFIG_CLASSES[rel]:
                continue
            name, default = f["name"], f["default"]
            if name in SEMANTIC_NAME_EXCEPTIONS or name in STATE_B_CONFIG_KEYS:
                continue
            if (fast_named(name) and default is False) or (slow_named(name) and default is True):
                if (rel, f["class"], name) not in SEMANTIC_PARAM_ALLOWLIST:
                    add("B-FIELD", rel, f["class"], name, f"field defaults to the slow path ({default})")
        _scan_functions(repo, rel, tree, add)
        _scan_modconsts(rel, tree, add)

    # ---- State A: classification / registry / yaml / deploy writers --------------------------------
    for k in sorted(state_a):
        f = cfg_fields[k]
        if k not in live_keys:
            add("A-UNCLASSIFIED", f["file"], f["class"], k, "State-A bool not declared in LIVE_SWITCH_KEYS")
        if f["host_only"]:
            add("A-UNREGISTERED", f["file"], f["class"], k, "host_only bool is invisible to parameter_registry")
        if f["family"] in YAML_FILES and k not in yaml_top_keys(repo, YAML_FILES[f["family"]]):
            add("A-YAML", YAML_FILES[f["family"]], "<yaml>", k, "State-A bool missing from shipped YAML")
    for k in sorted(live_keys - set(cfg_fields)):
        add("A-LIVE-STALE", LIVE_MODULE, "LIVE_SWITCH_KEYS", k, "declared live key is not a config bool field")
    for k in sorted(live_keys & state_b_present):
        add("A-LIVE-STALE", LIVE_MODULE, "LIVE_SWITCH_KEYS", k, "State-B switch declared live")
    deploy_tier = {k for k in state_a if (cfg_fields[k]["family"] == "agent" and k in deployment_only)
                   or (cfg_fields[k]["family"] == "capability" and k in deploy_cap)
                   or cfg_fields[k]["family"] == "runtime_guard"}
    for k in sorted(deploy_tier - deploy_live):
        add("A-DEPLOY-SET", cfg_fields[k]["file"], cfg_fields[k]["class"], k, "deployment-tier bool not in DEPLOY_LIVE_SWITCH_KEYS")
    for k in sorted(deploy_live - deploy_tier):
        add("A-DEPLOY-SET", LIVE_MODULE, "DEPLOY_LIVE_SWITCH_KEYS", k, "declared deploy-live but not deployment-tier")
    for k in sorted(deploy_live & (tunable | usb)):
        add("A-DEPLOY-WRITER", UCC, "TUNABLE_KEYS|USER_SETTINGS_BOUNDARY_KEYS", k, "deploy-live bool writable by /settings or the model")
    for k in sorted(k for k in deploy_tier if cfg_fields[k]["family"] == "agent" and k not in cli_writable):
        add("A-DEPLOY-CLI", CHANGES, "CLI_DEPLOYMENT_WRITABLE_KEYS", k, "agent deploy-live bool not writable by `my-agent config set`")
    if not _registry_wired(repo):
        add("A-REGISTRY-EFFECT", REGISTRY, "parameter_registry", "LIVE_SWITCH_KEYS",
            "no registry function maps LIVE_SWITCH_KEYS to EFFECT_IMMEDIATE")
    for rel, func in HOOKS:
        if not _hook_present(repo, rel, func):
            add("A-HOOK", rel, func or "<module>", HOOK_NAME, f"{HOOK_NAME}() not called here")

    # ---- State A: construction-time snapshots --------------------------------------------------------
    for rel in repo.production_files():
        tree = repo.tree(rel)
        if tree is None or rel == LIVE_MODULE:
            continue
        _scan_snapshots(rel, tree, scan_live, cfg_fields, add)

    facts = {
        "config_bools": len(cfg_fields), "state_b_present": sorted(state_b_present), "state_a": len(state_a),
        "live_module": live_declared, "live_keys": len(live_keys), "deploy_live_keys": len(deploy_live),
        "deploy_tier": sorted(deploy_tier),
    }
    uniq: dict[str, Violation] = {}
    for item in v:
        uniq.setdefault(item.id, item)
    return sorted(uniq.values(), key=lambda x: (x.rule, x.file, x.scope, x.name)), facts


def _scan_functions(repo: Repo, rel: str, tree: ast.Module, add) -> None:
    class V(ScopeVisitor):
        param_stack: list[set[str]] = []

        def visit_FunctionDef(self, node):
            a = node.args
            self.param_stack.append({x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]})
            super().visit_FunctionDef(node)
            self.param_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def on_function(self, node):
            qual = ".".join([*self.stack, node.name])
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            for arg, default in pairs:
                if not (isinstance(default, ast.Constant) and isinstance(default.value, bool)):
                    continue
                name = arg.arg
                if name in SEMANTIC_NAME_EXCEPTIONS or (rel, qual, name) in SEMANTIC_PARAM_ALLOWLIST:
                    continue
                if (fast_named(name) and default.value is False) or (slow_named(name) and default.value is True):
                    add("B-PARAM", rel, qual, name, f"parameter defaults to the slow path ({default.value}) at line {node.lineno}")

        def visit_Call(self, node):
            for kw in node.keywords:
                if kw.arg not in PURE_FAST_PARAMS or (isinstance(kw.value, ast.Constant) and kw.value.value is True):
                    continue
                passthrough = (isinstance(kw.value, ast.Name) and kw.value.id == kw.arg
                               and bool(self.param_stack) and kw.arg in self.param_stack[-1])
                literal_false = isinstance(kw.value, ast.Constant) and kw.value.value is False
                # scan_budget also names budget objects in some callees: only the literal slow value counts there.
                if passthrough or (kw.arg == "scan_budget" and not literal_false):
                    continue
                if (rel, self.scope, kw.arg) not in SEMANTIC_PARAM_ALLOWLIST:
                    add("B-CALL", rel, self.scope, kw.arg,
                        f"passes {ast.unparse(kw.value)[:60]} to a pure fast-path parameter (line {node.lineno})")
            self.generic_visit(node)

        def visit_Compare(self, node):
            # 'scan_budget' in inspect.signature(fn).parameters  -> slow fallback branch for older signatures
            if (isinstance(node.left, ast.Constant) and node.left.value in PURE_FAST_PARAMS
                    and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
                    and "signature" in ast.unparse(node.comparators[0])):
                add("B-PROBE", rel, self.scope, node.left.value, f"signature probe keeps a slow fallback (line {node.lineno})")
            self.generic_visit(node)

    V().visit(tree)


def _scan_modconsts(rel: str, tree: ast.Module, add) -> None:
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, bool)):
                continue
            for t in targets:
                if isinstance(t, ast.Name) and t.id.isupper() and (MODCONST_RE.search(t.id) or fast_named(t.id.lower())):
                    if (rel, t.id) not in MODCONST_ALLOWLIST:
                        add("B-MODCONST", rel, "<module>", t.id, f"module-level bool switch constant = {value.value} (line {node.lineno})")


def _scan_snapshots(rel: str, tree: ast.Module, live: set[str], cfg_fields: dict, add) -> None:
    class V(ScopeVisitor):
        def on_function(self, node):
            qual = ".".join([*self.stack, node.name])
            is_ctor = node.name in {"__init__", "__post_init__"} or (rel, node.name) in CONSTRUCTION_FUNCS
            if not is_ctor:
                return
            own_fields = set()
            if self.class_stack:
                cls = self.class_stack[-1]
                own_fields = {i.target.id for i in cls.body if isinstance(i, ast.AnnAssign) and isinstance(i.target, ast.Name)}
                if rel in CONFIG_CLASSES and cls.name in CONFIG_CLASSES[rel]:
                    return
            for key, line, kind in key_reads(node, live):
                if kind == "attr:self" and key in own_fields:
                    continue
                if (rel, qual, key) in SNAP_ALLOWLIST:
                    continue
                add("A-SNAP", rel, qual, key, f"live switch read at construction time ({kind}, line {line})")
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in SNAP_HELPERS:
                    if (rel, qual, sub.func.id) not in SNAP_ALLOWLIST:
                        add("A-SNAP", rel, qual, sub.func.id, f"switch helper evaluated at construction time (line {sub.lineno})")

    V().visit(tree)


def _registry_wired(repo: Repo) -> bool:
    tree = repo.tree(REGISTRY)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            if "LIVE_SWITCH_KEYS" in names and "EFFECT_IMMEDIATE" in names:
                return True
    return False


def _hook_present(repo: Repo, rel: str, func: str | None) -> bool:
    tree = repo.tree(rel)
    if tree is None:
        return False
    scopes: list[ast.AST] = [tree]
    if func is not None:
        scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func]
    for scope in scopes:
        for sub in ast.walk(scope):
            if isinstance(sub, ast.Call):
                f = sub.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
                if name == HOOK_NAME:
                    return True
    return False


# ---- baseline ---------------------------------------------------------------------------------------
def load_baseline(path: Path) -> tuple[list[str], int] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != BASELINE_SCHEMA or not isinstance(data.get("entries"), list):
        raise GateError(f"{path}: not a {BASELINE_SCHEMA} file")
    entries = [str(e) for e in data["entries"]]
    bad = [e for e in entries if not ID_RE.match(e)]
    if bad:
        raise GateError(f"{path}: malformed entries {bad[:3]}")
    return entries, int(data.get("frozen_count", len(entries)))


def write_baseline(path: Path, entries: list[str], anchor: str) -> None:
    payload = {
        "schema": BASELINE_SCHEMA,
        "rule": "shrink-only: entries may only be removed (--prune-baseline); frozen_count never grows",
        "anchor": anchor,
        "frozen_count": len(entries),
        "entries": sorted(entries),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=None, help="repository root (default: parent of scripts/)")
    ap.add_argument("--baseline", default=None, help=f"baseline JSON (default: <root>/{BASELINE_REL})")
    ap.add_argument("--strict", action="store_true", help="ignore the baseline")
    ap.add_argument("--json", action="store_true", help="print a JSON report")
    ap.add_argument("--write-baseline", action="store_true", help="create the baseline (refuses to overwrite)")
    ap.add_argument("--prune-baseline", action="store_true", help="remove fixed entries, lower frozen_count")
    ap.add_argument("--anchor", default="", help="anchor commit recorded by --write-baseline")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    baseline_path = Path(args.baseline) if args.baseline else root / BASELINE_REL
    try:
        violations, facts = run_gate(root)
        ids = [x.id for x in violations]
        if args.write_baseline:
            if baseline_path.exists():
                print(f"refusing to overwrite {baseline_path}: the baseline is shrink-only (use --prune-baseline)")
                return 1
            write_baseline(baseline_path, ids, args.anchor)
            print(f"wrote {len(ids)} entries to {baseline_path}")
            return 0
        loaded = None if args.strict else load_baseline(baseline_path)
    except GateError as exc:
        print(f"boolean-switch gate error: {exc}", file=sys.stderr)
        return 2
    baseline, frozen = (loaded if loaded is not None else ([], 0))
    base_set = set(baseline)
    new = [x for x in violations if x.id not in base_set]
    fixed = sorted(base_set - set(ids))
    growth = len(baseline) > frozen
    if args.prune_baseline and loaded is not None:
        keep = [e for e in baseline if e in set(ids)]
        write_baseline(baseline_path, keep, json.loads(baseline_path.read_text(encoding="utf-8")).get("anchor", ""))
        print(f"pruned {len(baseline) - len(keep)} fixed entries; {len(keep)} remain")
        baseline, base_set, fixed = keep, set(keep), []
        new = [x for x in violations if x.id not in base_set]
    if args.json:
        print(json.dumps({
            "ok": not new and not growth, "facts": facts, "violations": [x.__dict__ | {"id": x.id} for x in violations],
            "new": [x.id for x in new], "baselined": len(violations) - len(new), "fixed_baseline_entries": fixed,
            "baseline_growth": growth,
        }, ensure_ascii=False, indent=1))
    else:
        for x in new:
            print(f"{x.rule} {x.file} [{x.scope}] {x.name} — {x.message}")
        if fixed:
            print(f"note: {len(fixed)} baseline entries are fixed; run --prune-baseline in the merge commit")
        if growth:
            print(f"baseline grew past frozen_count={frozen}: the baseline is shrink-only")
        print(f"boolean-switch gate: {len(new)} new, {len(violations) - len(new)} baselined, {len(fixed)} fixed-in-baseline"
              f" (config bools={facts['config_bools']}, live declared={facts['live_module']})")
    return 1 if new or growth else 0


if __name__ == "__main__":
    sys.exit(main())
