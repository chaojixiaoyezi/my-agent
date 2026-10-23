# LLM: 动作分类不是资源授权，真实授权链另跑现有合成HTTP回归。
# 模块用途: 锁定飞书唯一动作表及派生读写集合。
from agent_py_agent.agent.tooling import feishu_cloud_tool as tool
from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
from agent_py_agent.agent.user_space import feishu_cloud_authorization as authorization


# LLM: schema、授权分类和工具副作用均须来自同一表；未知动作仍按写处理。
# 函数用途: 防止sheet_meta遗漏和动作集合漂移。
def test_single_cloud_operation_authority():
    operations = authorization.CLOUD_OPERATIONS
    assert {action: (operation.resource_type, operation.write) for action, operation in operations.items()} == {
        'sheet_meta': ('sheet', False), 'sheet_read': ('sheet', False),
        'sheet_append': ('sheet', True), 'sheet_update': ('sheet', True),
        'document_read': ('docx', False), 'document_append': ('docx', True),
        'wiki_resolve': ('wiki', False)}
    assert tool.CLOUD_OPERATIONS is operations
    reads = frozenset(key for key, operation in operations.items() if not operation.write)
    assert authorization.READ_ACTIONS == tool.READ_ACTIONS == reads
    assert not hasattr(authorization, '_READ_ACTIONS') and not hasattr(tool, '_READ_ACTIONS')
    assert set(tool._TYPES) == set(operations)
    assert tool.FeishuCloudTool.model_spec.input_schema['properties']['action']['enum'] == list(operations)
    for action, operation in operations.items():
        assert tool_effect_for_runtime_policy(tool.FeishuCloudTool.runtime_policy, {'action': action}) == (
            'mutating' if operation.write else 'read_only')
    assert tool_effect_for_runtime_policy(tool.FeishuCloudTool.runtime_policy, {'action': 'unknown'}) == 'mutating'
