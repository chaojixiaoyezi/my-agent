# LLM: 动作分类不是资源授权，真实授权链另跑现有合成HTTP回归。
# 模块用途: 锁定飞书唯一动作表及派生读写集合。
from agent_py_agent.agent.tooling import feishu_cloud_tool as tool
from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
from agent_py_agent.agent.user_space import feishu_cloud_authorization as authorization


# LLM: schema、授权分类和工具副作用均须来自同一表；未知动作仍按写处理。
# 函数用途: 防止sheet_meta遗漏和动作集合漂移。
def test_single_cloud_operation_authority():
    operations = getattr(authorization, 'FEISHU_CLOUD_OPERATIONS', None)
    assert operations == {'sheet_meta': 'read', 'sheet_read': 'read', 'sheet_append': 'write',
                          'sheet_update': 'write', 'document_read': 'read',
                          'document_append': 'write', 'wiki_resolve': 'read'}
    assert getattr(tool, 'FEISHU_CLOUD_OPERATIONS', None) is operations
    reads = frozenset(key for key, mode in operations.items() if mode == 'read')
    assert authorization._READ_ACTIONS == tool._READ_ACTIONS == reads
    assert set(tool._TYPES) == set(operations)
    assert tool.FeishuCloudTool.model_spec.input_schema['properties']['action']['enum'] == list(operations)
    for action, mode in operations.items():
        assert tool_effect_for_runtime_policy(tool.FeishuCloudTool.runtime_policy, {'action': action}) == (
            'read_only' if mode == 'read' else 'mutating')
    assert tool_effect_for_runtime_policy(tool.FeishuCloudTool.runtime_policy, {'action': 'unknown'}) == 'mutating'
