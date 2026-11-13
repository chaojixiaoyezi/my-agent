"""受理时冻结当前发送者的空间选择；执行重放只校验原快照。"""
from __future__ import annotations

REQUEST_SPACE_KEY = "host_session_space.v1"
_FIELDS = {"owner_id", "actor_id", "thread_id", "space_id", "revision", "source"}


def capture_request_space(home, thread, *, actor_id):
    from ..conversation.session_space import read_session_space

    selected = read_session_space(home, thread, actor_id=actor_id)
    if selected["space_id"] is None:
        return None
    return {"owner_id": str(home.owner_id), "actor_id": actor_id,
            "thread_id": thread.thread_id, **selected}


def request_space_for_execution(request, *, owner_id, thread_id, actor_id):
    """只接受原受理快照，空间切换不重写排队/恢复中的请求。"""
    snapshot = request.get(REQUEST_SPACE_KEY)
    if snapshot is None:
        return None
    if (not isinstance(snapshot, dict) or set(snapshot) != _FIELDS
            or snapshot.get("owner_id") != owner_id or snapshot.get("thread_id") != thread_id
            or snapshot.get("actor_id") != actor_id
            or not isinstance(snapshot.get("space_id"), str) or not snapshot["space_id"]
            or type(snapshot.get("revision")) is not int or snapshot["revision"] < 1
            or snapshot.get("source") != "thread"):
        raise ValueError("受理空间快照与当前用户或会话不一致")
    return dict(snapshot)
