# LLM: Runtime context-bundle bridge keeps root run cards out of the broader loop support file.
# 模块用途: 从 RuntimeContextRequest 生成主代理 context bundle，并隔离 task-local 不继承主代理上下文。

from __future__ import annotations

from ..user_space.context_bundle import MainContextBundleRequest, build_main_context_bundle
from .runtime_loop_models import RuntimeContextRequest


# LLM: build_runtime_main_context_bundle keeps root context injection out of isolated task-local runs.
# 函数用途: 根据运行范围和保存边界生成主代理 context bundle；子代理/控制面隔离运行不继承主代理 bundle。
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
            tool_specs=tuple(_tool_specs_for_context(agent, request)),
        )
    )


# LLM: _tool_specs_for_context exposes a compact tool manifest in the root context bundle.
# 函数用途: 从工具注册表读取当前可见工具规格；失败时返回空列表，让 bundle self-check 降级而非中断 run。
def _tool_specs_for_context(agent, request: RuntimeContextRequest) -> list[object]:
    try:
        return agent.tools.specs(
            allowed_tools=request.allowed_tools,
            granted_capabilities=request.granted_capabilities,
            include_orchestration=True,
        )
    except Exception:
        return []


__all__ = ["build_runtime_main_context_bundle"]
