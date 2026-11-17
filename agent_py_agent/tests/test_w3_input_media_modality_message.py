"""The attachment refusal text must cover the model-modality case.

Live clone (2026-11-17): a video sent to the MiniMax profile (declared input: image, text) was refused before any
model call with "当前模型档案明确不支持本次附件模态", but the user only saw "please re-add the attachment", which
cannot help; the way forward is another model.
"""
from __future__ import annotations

from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message


def test_user_text_names_the_unsupported_modality_case_and_the_model_switch() -> None:
    text = gateway_client_error_message("INPUT_MEDIA_INVALID")
    assert "当前模型不支持" in text and "/model" in text and "重新添加" in text


def test_recovery_hint_offers_a_model_that_declares_the_modality() -> None:
    hint = error_contract("INPUT_MEDIA_INVALID").recovery_hint
    assert "模态" in hint and "不要去掉附件改发纯文字" in hint
