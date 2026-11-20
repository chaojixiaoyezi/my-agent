from __future__ import annotations

import ast
import inspect
from importlib import import_module
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_HOISTED_CLASSES = {
    "agent.owner_snapshot_files": ("_WindowsApiInfo", "_WindowsFdModeBasicInfo"),
    "agent.tooling.audit_requests_topic": ("_ScopeOwnersCandidateOwners",),
    "agent.capability.asset_evolution_routes": ("_LiftEvolutionLiteralsLift",),
    "agent.capability.skill_learning": ("_AdoptCommittedLessonsIsolatedCommittedProposalProxy",),
    "agent.capability.asset_evolution": ("_EvolutionFamilyFingerprintNormalize",),
    "agent.memory_store.daily": ("_QuotaAdmissionContext",),
    "agent.memory_store.candidates": ("_QuotaAdmissionContext",),
    "agent.common.nofollow_fs": ("_WindowsChangeQueryBasicInfo",),
    "agent.gateway_parts.native_subscription_source": (
        "_PrepareNativeSubscriptionNativeSubscription",
        "_PrepareNativeSubscriptionRoutedBackground",
    ),
    "agent.gateway_parts.business_api": (
        "_RequireBusinessTransportSourceResponse",
        "_RegisterBusinessToolsBusinessDomainTool",
        "_RegisterBusinessToolsBusinessAcceptTool",
    ),
    "agent.memory_archive.tool_output_retention": ("_SweepLocksSweepBudgetStop",),
    "agent.user_space.home_index_compact": ("_CompactIndexPageSourceIdentity",),
    "agent.conversation.runtime_reconciliation": ("_SchedulerEffectReadonlyReadOnlyHistory",),
}


def test_no_classes_nested_in_functions():
    nested = []
    for module in _HOISTED_CLASSES:
        relative = module.replace(".", "/") + ".py"
        tree = ast.parse((_PACKAGE_ROOT / relative).read_bytes(), filename=relative)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            enclosing = parents.get(node)
            while enclosing is not None:
                if isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested.append(f"{relative}:{node.lineno} {enclosing.name}.{node.name}")
                    break
                enclosing = parents.get(enclosing)
    assert not nested, "Classes defined inside functions:\n" + "\n".join(nested)


def test_hoisted_methods_have_no_freevars():
    problems = []
    for module_name, class_names in _HOISTED_CLASSES.items():
        module = import_module(f"agent_py_agent.{module_name}")
        for class_name in class_names:
            cls = getattr(module, class_name, None)
            qualified = f"{module_name}.{class_name}"
            if not inspect.isclass(cls) or cls.__module__ != module.__name__ or cls.__qualname__ != class_name:
                problems.append(f"{qualified} is missing as a module-level class")
                continue
            for name, member in vars(cls).items():
                if isinstance(member, (classmethod, staticmethod)):
                    member = member.__func__
                methods = (member.fget, member.fset, member.fdel) if isinstance(member, property) else (member,)
                for method in methods:
                    if inspect.isfunction(method) and method.__code__.co_freevars:
                        problems.append(f"{qualified}.{name} captures {method.__code__.co_freevars}")
    assert not problems, "\n".join(problems)
