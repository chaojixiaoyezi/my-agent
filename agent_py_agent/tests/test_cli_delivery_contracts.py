from __future__ import annotations


# LLM: Delivery-contract CLI loading should be observable when a machine contract is unreadable.
# 函数用途: 验证坏 JSON 不会静默吞掉，方便运行链路定位合同文件问题。
def test_delivery_contract_from_file_warns_on_unreadable_contract(tmp_path, caplog) -> None:
    from agent_py_agent.cli.delivery_contracts import delivery_contract_from_file

    path = tmp_path / "delivery_contract.json"
    path.write_text("{bad json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        payload = delivery_contract_from_file(str(path))

    assert payload is None
    assert "failed to read delivery contract" in caplog.text


# LLM: Valid delivery contracts should still load as plain machine fields.
# 函数用途: 防止 warning 增强影响正常 --delivery-contract-file 读取。
def test_delivery_contract_from_file_loads_json_object(tmp_path) -> None:
    from agent_py_agent.cli.delivery_contracts import delivery_contract_from_file

    path = tmp_path / "delivery_contract.json"
    path.write_text('{"schema_version": "delivery.v1", "artifacts": []}', encoding="utf-8")

    assert delivery_contract_from_file(str(path)) == {
        "schema_version": "delivery.v1",
        "artifacts": [],
    }
