"""工作流列表仅有名称/关联空间；编号绑定稳定 workflow_ref 而不是排序位置。"""
from __future__ import annotations

# LLM: 工作流客户端命令与表格编号映射，列表输出严格只含名称/关联空间两列；
# 编号绑定特定快照中的 workflow_ref，支持 cross-process CLI 稳定解析（V02-02 / V07-03 / V13-03）。
# 模块用途: 提供工作流表格编号维护、详情渲染与 CLI 入口。
import json

from .space_commands import run_business_cli


# LLM: 跟踪 workflows.list 快照与表格序号映射，快照过期或条目不可见时抛明确结构化错误，
# 非纯数字参数则作为稳定的 cross-process workflow_ref 直接放行。
# 类用途: 维护终端/会话内的两列工作流展示表及编号解析。
class WorkflowTable:
    def __init__(self):
        self._numbers: dict[str, int] = {}
        self._visible: set[str] = set()
        self.list_snapshot_id: str | None = None

    # LLM: 将工作流列表更新为两列展示元组，并绑定当前 list_snapshot_id。
    # 函数用途: 返回规范化的 (名称, 关联空间) 两列展示数据并更新可见集合。
    def update(self, items: list[dict], *, list_snapshot_id: str | None = None) -> tuple[tuple[str, str], ...]:
        self._visible = {row['workflow_ref'] for row in items}
        if list_snapshot_id is not None:
            self.list_snapshot_id = str(list_snapshot_id)
        output = []
        for row in items:
            ref = row['workflow_ref']
            if ref not in self._numbers:
                self._numbers[ref] = len(self._numbers) + 1
            scope = row.get('scope') or {}
            mode = scope.get('scope_mode')
            spaces = '、'.join(scope.get('space_ids') or []) if mode == 'spaces' else {
                'owner_global': '所有空间', 'unassigned': '未分配'}.get(mode, '未知')
            output.append((f'{self._numbers[ref]} {row.get("name", ref)}', spaces))
        return tuple(output)

    # LLM: 按编号解析原始 workflow_ref，遇到非数字则作为稳定跨进程 ref 直接返回；若校验快照不符则报错。
    # 函数用途: 将用户输入的编号或工作流引用解析为确切的 workflow_ref。
    def resolve(self, number_or_ref: str, *, expected_snapshot_id: str | None = None) -> str:
        if expected_snapshot_id is not None and self.list_snapshot_id and self.list_snapshot_id != expected_snapshot_id:
            raise ValueError('快照已更新，请重新查看列表')
        raw = str(number_or_ref).strip()
        for ref, value in self._numbers.items():
            if str(value) == raw and ref in self._visible:
                return ref
        if raw in self._visible:
            return raw
        if not raw.isdecimal():
            return raw
        raise ValueError('编号已失效，请重新查看列表')


# LLM: 格式化 workflows.get 详情回执，展示用途、IO、版本、步骤、运行引用、状态及宿主已裁决动作（V07-03）。
# 函数用途: 将工作流详情 DTO 转换为多行文本展示。
def format_workflow_detail(data: dict) -> str:
    workflow = data.get('workflow', {})
    detail = data.get('detail', {})
    run_refs = data.get('run_refs', [])
    purpose = detail.get('summary') or workflow.get('name', '')
    io_contract = detail.get('io_contract')
    version = workflow.get('definition_revision', 1)
    steps = detail.get('steps', [])
    lifecycle = workflow.get('lifecycle', 'active')
    enabled = workflow.get('enabled', True)
    draft_status = 'draft' if not enabled else lifecycle

    lines = [
        f"工作流: {workflow.get('name', '')} ({workflow.get('workflow_ref', '')})",
        f"用途: {purpose}",
        f"版本: {version}",
        f"状态: {draft_status}",
        f"IO合同: {json.dumps(io_contract, ensure_ascii=False) if io_contract else '无'}",
        f"步骤 ({len(steps)}):",
    ]
    for step in steps:
        step_id = step.get('step_id', '')
        name = step.get('name', '')
        mode = step.get('mode', '')
        lines.append(f"  - [{step_id}] {name} ({mode})")
    lines.append(f"运行引用 ({len(run_refs)}): {', '.join(run_refs) if run_refs else '无'}")
    for view in data.get('run_views', [])[:5]:
        run = view.get('run', {})
        run_id = run.get('workflow_run_id', '')
        if view.get('observation_error'):
            lines.append(f"运行 {run_id}: 未确认 · {view['observation_error']}")
            continue
        status = run.get('status', 'unknown')
        label = '已暂停／待恢复' if status == 'paused_for_resume' else status
        lines.append(f"运行 {run_id}: {label} [{status}] · 效果 {run.get('effect_outcome', 'unknown')}")
        for step in view.get('steps', [])[:100]:
            lines.append(f"  步骤 [{step.get('step_id', '')}]: {step.get('status', 'unknown')}")
        breakpoint = view.get('breakpoint') or {}
        if breakpoint:
            lines.append(f"  断点: {breakpoint.get('reason_code', 'unknown')} · 下一步: {breakpoint.get('required_action', 'unknown')}")
    actions = data.get('allowed_actions')
    if 'allowed_actions' not in data:
        lines.append('可用操作: 未知（宿主未返回动作清单，请重新读取详情）')
    elif not isinstance(actions, list) or any(not isinstance(action, str) or not action for action in actions):
        lines.append('可用操作: 未知（宿主动作清单格式不正确，请重新读取详情）')
    elif not actions:
        lines.append('可用操作: 无（宿主未授予当前对象的编辑或管理操作）')
    else:
        # 展示标签不参与权限裁决；新动作保留宿主机器名，不因客户端尚不认识而隐藏。
        labels = {'workflows.update': '编辑内容', 'workflows.scope.set': '调整关联空间',
                  'workflows.run': '运行', 'workflows.recycle': '回收', 'workflows.restore': '恢复'}
        lines.append('可用操作: ' + '、'.join(
            f'{labels[action]} [{action}]' if action in labels else action for action in actions))
    return "\n".join(lines)


# LLM: 将 workflow 命令行请求通过统一的 run_business_cli 转派给 BusinessClient。
# 函数用途: 执行 my-agent workflow 命令。
def cmd_workflow(args) -> int:
    return run_business_cli(args, command='workflow')
