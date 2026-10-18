# LLM: 此叶子只在主HTTP认证/摘要/actor/media构造之后接原C实际work；receipt-first不重签，未知不下放foreign Agent，不把运输投影当业务权。
# 模块用途: 冷/健康C受理原输入，内层typed仅在HTTP边界固定503等待；Count0原入口和权限不变。
from dataclasses import dataclass

from . import input_delivery_service as inputs
from .control_plane import server_wait_projection
from .im_dispatch_policy import IM_DISPATCH_KEY
from .multiprocess_input_receipt import CoordinatorInputReceiptSource


# LLM: 仅汇集原HTTP已鉴权/摘要/actor/media输入；不是授权对象，所有写仍经过原actual-work source与最后门。
# 类用途: 将原handler这次完整受理参数一起交给C叶子，避免多份位置参数相互错配。
@dataclass(frozen=True)
class CoordinatorInputRequest:
    prepared_request: dict
    scope: object
    request_id: str
    client_input_digest: str
    client_message_id: str
    guidance_key: str
    allow_active_turn: bool


# LLM: 只调用固定C三动作；existing只重放原身份，fresh由真实创建链决定，quarantine/delegated完整排队，未决运输不能再选新目标。
# 函数用途: 在这次实际HTTP工作片内保存输入并返回原结构化去向，不运行业务Agent。
def _handle_coordinator_input(handler, server, request):
    from .http_handlers import _prepare_http_gateway_input

    prepared_request, scope, request_id = request.prepared_request, request.scope, request.request_id
    client_input_digest, client_message_id = request.client_input_digest, request.client_message_id
    guidance_key, allow_active_turn = request.guidance_key, request.allow_active_turn
    source = CoordinatorInputReceiptSource(handler, server)
    receipt, created = source.prepare(inputs.GatewayInputPreparation(request_id=request_id, client_input_digest=client_input_digest,
        client_message_id=client_message_id, guidance_dedupe_key=guidance_key, prepared_request=prepared_request,
        prepare_request=lambda prepared: _prepare_http_gateway_input(server, scope, prepared)))
    wait = server_wait_projection(server, receipt.prepared_request) if receipt.state != 'consumed' else {}
    delegated = (receipt.prepared_request.get(IM_DISPATCH_KEY) or {}).get('mode') == 'delegated'
    if created:
        receipt = source.route_fresh(receipt, scope, allow_active=allow_active_turn and not wait and not delegated)
    elif receipt.state == 'queued':
        receipt = source.queue_unbound(receipt)
    elif receipt.transport is not None:
        receipt = source.bind_original_guidance(receipt)
    if wait:
        from .wait_acceptance import confirm_wait_acceptance

        receipt = confirm_wait_acceptance(server.paths, request_id, wait, receipt=receipt)
        if receipt.state == 'consumed':
            wait = {}
    if created and receipt.state == 'queued' and not wait:
        from .event_points import prompt_queued

        prompt_queued(server, receipt.prepared_request)
    source.require_original()
    handler._send_json(202, {**inputs.gateway_input_status_payload(receipt), **wait})


# LLM: 原CoordinatorInputRequest仅聚合已鉴权字段；内层actual typed同对象到本次HTTP最外边界才投影固定503，不写终态/ACK/许可，KeyboardInterrupt/SystemExit不吞。
# 函数用途: 把本次C收件未确认事实安全返给原request_id；不将部分提交说成未执行/已接受/普通失败。
def handle_coordinator_input(handler, server, request: CoordinatorInputRequest):
    from ..runtime_errors import DataCorruptionError
    from .multiprocess_global_write import GlobalWriteError
    from .multiprocess_input_reconcile import _UNCERTAIN

    request_id = request.request_id
    try:
        return _handle_coordinator_input(handler, server, request)
    except (KeyboardInterrupt, SystemExit):
        raise
    except (*_UNCERTAIN, GlobalWriteError) as error:
        handler._send_json(503, {'error': 'input acceptance durability is unconfirmed',
            'error_code': str(getattr(error, 'error_code', '') or type(error).__name__),
            'request_id': request_id, 'terminal': False, 'status': 'waiting'})
    except DataCorruptionError:
        handler._send_json(503, {'error': 'gateway input receipt is temporarily unavailable',
            'error_code': 'DataCorruptionError', 'request_id': request_id, 'terminal': False})
    except ValueError:
        handler._send_json(409, {'error': 'gateway input identity conflicts',
            'error_code': 'IDEMPOTENCY_CONFLICT', 'request_id': request_id, 'terminal': False})
    except OSError as error:
        handler._send_json(503, {'error': 'input acceptance durability is unconfirmed',
            'error_code': type(error).__name__, 'request_id': request_id, 'terminal': False})
