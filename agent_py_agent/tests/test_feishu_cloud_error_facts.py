"""HTTP/CLI共用供应商数值错误，不用正文推断副作用。"""
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.adapter import feishu_cli, feishu_cloud


# LLM: 只替换供应商运输；HTTP/API状态与原共享分类器保持真实入口，不调用飞书租户。
# 类用途: 返回有界JSON和实际HTTP包装的隔离响应。
class _Response:
    def __init__(self, status, code):
        self.status = status
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps({'code':self.code, 'msg':'SYNTHETIC_SECRET_NOT_FOR_LOG'}).encode()


@pytest.mark.parametrize('backend', ['http', 'cli'])
@pytest.mark.parametrize('http_status', [200, 400, 503])
@pytest.mark.parametrize('code,expected', [(90202,'FEISHU_CLOUD_RANGE_INVALID'),
    (90215,'FEISHU_CLOUD_SHEET_NOT_FOUND'), (90213,'FEISHU_CLOUD_RESOURCE_FORBIDDEN')])
@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_sheet_rejection_preserves_actual_status_and_code(monkeypatch, backend, http_status, code, expected, method):
    def send(*_a, **_k):
        if http_status >= 400:
            raise HTTPError('https://synthetic.invalid', http_status, 'SYNTHETIC_SECRET_NOT_FOR_LOG', {},
                io.BytesIO(json.dumps({'code':code}).encode()))
        return _Response(http_status, code)
    monkeypatch.setattr(feishu_cloud, 'open_authenticated_request', send)
    monkeypatch.setattr(feishu_cli, 'execute_cli_request', lambda *_a, **_k:
        {'http_status':http_status, 'request_sent':True, 'response':{'code':code}})
    adapter = SimpleNamespace(_get_tenant_access_token=lambda:'synthetic-token')
    request = feishu_cloud.request_cloud_api if backend == 'http' else feishu_cli.request_cli_cloud_api
    with pytest.raises(feishu_cloud.FeishuCloudError) as error:
        request(adapter, method, '/sheets/v2/spreadsheets/synthetic/values_append', {})
    assert error.value.code == expected
    assert error.value.public_facts() == {'http_status':http_status, 'provider_code':code,
        'reason':{90202:'sheet_range_invalid',90215:'sheet_not_found',90213:'resource_permission_required'}[code],
        'request_stage':'provider_response', 'effect_outcome':'not_started'}
    assert 'SYNTHETIC_SECRET' not in str(error.value)


@pytest.mark.parametrize('backend', ['http', 'cli'])
@pytest.mark.parametrize('code,path', [(777777,'/sheets/v2/spreadsheets/synthetic/values_append'),
    (90202,'/docx/v1/documents/synthetic/blocks/root/children')])
def test_unfamiliar_rejection_or_wrong_action_contract_stays_unknown(monkeypatch, backend, code, path):
    monkeypatch.setattr(feishu_cloud, 'open_authenticated_request', lambda *_a, **_k:_Response(200, code))
    monkeypatch.setattr(feishu_cli, 'execute_cli_request', lambda *_a, **_k:
        {'http_status':200, 'request_sent':True, 'response':{'code':code}})
    request = feishu_cloud.request_cloud_api if backend == 'http' else feishu_cli.request_cli_cloud_api
    with pytest.raises(feishu_cloud.FeishuCloudError) as error:
        request(SimpleNamespace(_get_tenant_access_token=lambda:'synthetic'), 'POST', path, {})
    assert error.value.unknown and error.value.http_status == 200
