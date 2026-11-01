"""工作流列表仅有名称/关联空间；编号绑定稳定 workflow_ref 而不是排序位置。"""
from .space_commands import run_business_cli


class WorkflowTable:
    def __init__(self):
        self._numbers: dict[str, int] = {}
        self._visible: set[str] = set()

    def update(self, items: list[dict]) -> tuple[tuple[str, str], ...]:
        self._visible = {row['workflow_ref'] for row in items}
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

    def resolve(self, number: str) -> str:
        for ref, value in self._numbers.items():
            if str(value) == str(number) and ref in self._visible:
                return ref
        raise ValueError('编号已失效，请重新查看列表')


def cmd_workflow(args) -> int:
    return run_business_cli(args, command='workflow')
