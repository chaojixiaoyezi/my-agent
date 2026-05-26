from __future__ import annotations


def test_context_manifest_clues_and_outputs_are_refs_not_startup_gates(tmp_path):
    from agent_py_agent.agent.agent_core.runner_ref_fields import (
        params_input_refs,
        params_output_refs,
    )

    params = {
        "context_manifest": {
            "key_clue": "45.155.205.233",
            "output_path": "outputs/investigation_result.json",
            "target_sources": ["inputs/sources/source_01.txt"],
        }
    }

    assert params_input_refs(params) == ["inputs/sources/source_01.txt"]
    assert params_output_refs(params) == ["outputs/investigation_result.json"]


def test_dotted_numeric_clue_is_not_a_file_ref():
    from agent_py_agent.agent.agent_core.runner_ref_fields import params_input_refs

    assert params_input_refs({"context_manifest": {"target": "203.0.113.42"}}) == []
