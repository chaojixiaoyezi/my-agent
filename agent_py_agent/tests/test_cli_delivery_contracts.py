from __future__ import annotations


def test_delivery_contract_from_file_warns_on_unreadable_contract(tmp_path, caplog) -> None:
    from agent_py_agent.cli.delivery_contracts import delivery_contract_from_file

    path = tmp_path / "delivery_contract.json"
    path.write_text("{bad json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        payload = delivery_contract_from_file(str(path))

    assert payload is None
    assert "failed to read delivery contract" in caplog.text


def test_delivery_contract_from_file_loads_json_object(tmp_path) -> None:
    from agent_py_agent.cli.delivery_contracts import delivery_contract_from_file

    path = tmp_path / "delivery_contract.json"
    path.write_text('{"schema_version": "delivery.v1", "artifacts": []}', encoding="utf-8")

    assert delivery_contract_from_file(str(path)) == {
        "schema_version": "delivery.v1",
        "artifacts": [],
    }


def test_delivery_contract_from_file_attaches_preflight_findings(tmp_path, caplog) -> None:
    from agent_py_agent.cli.delivery_contracts import delivery_contract_from_file

    path = tmp_path / "delivery_contract.json"
    path.write_text('{"schema_version": "delivery.v1", "artifacts": ["bad"]}', encoding="utf-8")

    with caplog.at_level("WARNING"):
        payload = delivery_contract_from_file(str(path))

    assert payload is not None
    findings = payload["_preflight_findings"]
    assert findings[0]["code"] == "DELIVERY_CONTRACT_ARTIFACT_INVALID"
    assert "delivery contract preflight findings" in caplog.text
