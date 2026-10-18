"""P5 合同合成夹具，不启动 Gateway、不读取真实身份或配置。"""
import copy
from dataclasses import asdict

from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
from agent_py_agent.agent.gateway_parts.multiprocess_route import (
    RouteStamp,
    freeze_request_route,
    owner_digest,
    owner_worker,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def stamped_request(owner=None):
    owner = owner or OwnerIdentity.provider_user("synthetic", "alice")
    request = {"id": "p5-request", "kind": "ask", "prompt": "合成请求", "status": "pending",
               "user_id": "synthetic-user", "metadata": {"provider": "synthetic", "channel": "synthetic",
               "user_id": "synthetic-user", "chat_id": "chat-a"},
               "conversation": {"channel": "synthetic", "canonical_user_id": "synthetic-user",
                                "channel_conversation_id": "chat-a"}}
    from agent_py_agent.agent.gateway_parts.io import (
        GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
        gateway_request_fingerprint,
    )
    from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY

    request[REQUEST_OWNER_KEY] = asdict(owner)
    request["metadata"]["gateway_admin_binding_version"] = "binding-v1"
    request["request_fingerprint"] = gateway_request_fingerprint(request, request["id"])
    request["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    return freeze_request_route(request, owner, RouteStamp(2, "c-p5", 100.0, "binding-v1"))


def reservation_for(request):
    return {"schema_version": "multiprocess_admission_reservation.v2", "reservation_id": "admission-p5",
            "request_id": request["id"], "owner_sha": request["route"]["owner_sha"],
            "session_scope": session_scope_for_request(request), "route": copy.deepcopy(request["route"]),
            "state": "occupied", "terminal_fact": None}


def other_owner(request):
    original = OwnerIdentity(*request["route"]["owner"])
    candidates = (OwnerIdentity.provider_user("synthetic", f"other-{i}") for i in range(100))
    return next(owner for owner in candidates if owner != original and owner_worker(owner, 2) == owner_worker(original, 2))


def released_reservation(request):
    record = reservation_for(request)
    record.update(state="released", terminal_fact={"request_id": request["id"],
                  "canonical_ref": f"terminal/{request['id']}.json", "record_sha256": "a" * 64})
    assert record["owner_sha"] == owner_digest(OwnerIdentity(*request["route"]["owner"]))
    return record
