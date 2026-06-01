from __future__ import annotations


def test_string_list_supports_common_model_parameter_forms() -> None:
    from agent_py_agent.agent.common.value_parsing import StringListOptions, string_list

    assert string_list('["a", "b"]', StringListOptions(parse_json_list=True)) == ["a", "b"]
    assert string_list("- a\n- b", StringListOptions(split_lines=True, strip_bullets=True)) == ["a", "b"]
    assert string_list("a,b", StringListOptions(split_commas=True)) == ["a", "b"]
    assert string_list("a,b") == ["a,b"]


def test_common_numeric_and_bool_parsers_keep_existing_semantics() -> None:
    from agent_py_agent.agent.common.value_parsing import bool_value, non_negative_int, positive_int

    assert bool_value("apply") is True
    assert bool_value("dry-run") is False
    assert positive_int("4", default=1) == 4
    assert non_negative_int("bad", default=3) == 3
