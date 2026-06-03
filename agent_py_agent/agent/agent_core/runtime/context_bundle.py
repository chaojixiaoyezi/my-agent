
from __future__ import annotations

from ...runtime_errors import runtime_error_report
from ...user_space.context_bundle import MainContextBundleRequest, build_main_context_bundle
from .loop_models import RuntimeContextRequest


def build_runtime_main_context_bundle(
    agent,
    request: RuntimeContextRequest,
    *,
    memories: list,
    runtime_injections: list,
    routed_context,
    resume_context_injected: bool,
    task_local: bool,
):
    if task_local:
        return None
    auto_save = bool(getattr(agent.config, "auto_save_memory", True))
    do_save = auto_save if request.save is None else bool(request.save)
    tool_specs, tool_spec_errors = _tool_specs_for_context(agent, request)
    return build_main_context_bundle(
        MainContextBundleRequest(
            root=agent.root,
            home_paths=getattr(agent, "home_paths", None),
            user_prompt=request.user_prompt,
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            source=request.source,
            context_scope=request.context_scope,
            save=do_save,
            memory_count=len(memories),
            runtime_injection_count=len(runtime_injections),
            routed_required_read_paths=tuple(getattr(routed_context, "required_read_paths", ()) or ()),
            routed_candidate_paths=tuple(getattr(routed_context, "candidate_paths", ()) or ()),
            resume_context_injected=resume_context_injected,
            task_attributes=request.task_attributes,
            workspace_roots=tuple(str(item) for item in getattr(agent, "workspace_roots", []) or ()),
            write_boundary=request.write_boundary,
            allowed_tools=tuple(request.allowed_tools or ()),
            granted_capabilities=tuple(request.granted_capabilities or ()),
            tool_specs=tuple(tool_specs),
            tool_spec_errors=tuple(tool_spec_errors),
        )
    )


def _tool_specs_for_context(agent, request: RuntimeContextRequest) -> tuple[list[object], list[dict[str, object]]]:
    try:
        return agent.tools.specs(
            allowed_tools=request.allowed_tools,
            granted_capabilities=request.granted_capabilities,
            include_orchestration=True,
        ), []
    except Exception as exc:
        return [], [runtime_error_report(exc, context="main_context_bundle.tool_specs")]


__all__ = ["build_runtime_main_context_bundle"]
