# LLM: ID仅由认证scope与不透明message_id编码；调用方不得把ID当owner/任务权限，HTTP与本地提交共用唯一实现。
# 模块用途: 为普通输入在effect前分配与服务端一致的幂等请求ID，不读写状态。
import hashlib
import json


# LLM: 保持原HTTP身份JSON编码与字节完全一致，正文/投递意图只进入另一个client digest。
# 函数用途: 计算稳定普通输入ID，不发送请求、不创建owner。
def http_input_identity(*, user_id: str, channel: str, conversation_id: str, message_id: str):
    if not str(message_id or "").strip():
        return "", {}
    identity = {
        "user_id": str(user_id or "").strip(),
        "channel": str(channel or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "message_id": str(message_id or "").strip(),
    }
    identity_json = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "gwreq-msg-" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:32], identity
