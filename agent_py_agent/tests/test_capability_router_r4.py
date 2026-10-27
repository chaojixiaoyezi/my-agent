"""选中方法必须到达真实提示入口，空 scope 不构成限制。"""
import pytest

from agent_py_agent.agent.capability.router import CapabilityCard, CapabilityRouter, _scope_matches


# LLM: 调用真实生产索引入口，不伪造渲染结果；选中引用仍须与当前名卡相交。
# 函数用途: 证明选中脚本/流程可见且读取入口不被误标成Skill。
@pytest.mark.parametrize('kind,tool', [('script', 'scripts'), ('workflow', 'workflows')])
def test_r4_f01_selected_asset_card_reaches_model_prompt(kind, tool):
    reference = f'{kind}:owner:asset'
    card = CapabilityCard(reference, kind, 'asset', '核对 CSV 数据的可复用方法', metadata={
        'stable_id': reference, 'asset_id': 'asset', 'next_read': {
            'tool': tool, 'arguments': {'action': 'get', kind+'_id': 'asset'}}})
    router = CapabilityRouter(extra_cards=[card])
    text = router.render_skill_metadata_index(selected_skill_ids=(reference,))
    assert f'[{kind}]' in text and reference in text
    assert tool in text and f'"{kind}_id": "asset"' in text
    assert card.description in text
    assert 'asset' not in router.render_skill_metadata_index(selected_skill_ids=())
    assert not router.render_skill_metadata_index(selected_skill_ids=('script:owner:not-authorized',))


# LLM: 空列表只取消该已知维限制，不能吞掉其它维或未知维要求。
# 函数用途: 覆盖四维空列表及维间AND。
@pytest.mark.parametrize('dimension', ['channels', 'domains', 'workbenches', 'workspace_tags'])
def test_r4_b3_empty_dimension_list_does_not_restrict(dimension):
    assert _scope_matches({dimension: []}, {})
    assert _scope_matches({dimension: [], 'domains': ['data']} if dimension != 'domains'
                          else {'domains': [], 'channels': ['tui']}, {'domain': 'data', 'channel': 'tui'})
    assert not _scope_matches({dimension: [], 'unknown': 'required'}, {})


# LLM: 压缩描述时保留完整原生next_read，直接核对真实提示的token及字符预算，不靠独审推导。
# 函数用途: 覆盖脚本/流程长说明下的读取入口与预算边界。
@pytest.mark.parametrize('kind,tool', [('script', 'scripts'), ('workflow', 'workflows')])
def test_r4_f01_asset_locator_survives_description_budget(kind, tool):
    reference = f'{kind}:owner:asset'
    card = CapabilityCard(reference, kind, 'asset', '描述'*10000, metadata={
        'stable_id': reference, 'asset_id': 'asset', 'next_read': {
            'tool': tool, 'arguments': {'action': 'get', kind+'_id': 'asset'}}})
    text = CapabilityRouter(extra_cards=[card]).render_skill_metadata_index(
        selected_skill_ids=(reference,), context_window_tokens=20000)
    assert reference in text and f'"{kind}_id": "asset"' in text and tool in text
    assert (len(text.encode('utf-8'))+3)//4 <= 400
    assert len(text) <= 12000
