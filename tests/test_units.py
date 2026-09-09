import pytest
from scalably_ga4_mcp import server


def test_filter_expression_wraps_shortcut_field_name():
    fe = server._build_filter_expression({"field_name": "country", "string_filter": {"value": "US"}})
    assert fe.filter.field_name == "country"
    assert fe.filter.string_filter.value == "US"


def test_filter_expression_and_group_recurses():
    fe = server._build_filter_expression({"and_group": {"expressions": [
        {"filter": {"field_name": "country", "string_filter": {"value": "US"}}},
        {"filter": {"field_name": "deviceCategory", "string_filter": {"value": "mobile"}}},
    ]}})
    assert len(fe.and_group.expressions) == 2
    assert fe.and_group.expressions[0].filter.field_name == "country"


def test_filter_expression_not_group_recurses():
    fe = server._build_filter_expression({"not_expression": {"field_name": "country", "string_filter": {"value": "US"}}})
    assert fe.not_expression.filter.field_name == "country"


def test_filter_expression_none_passthrough():
    assert server._build_filter_expression(None) is None


def test_filter_expression_rejects_unrecognized_spec():
    with pytest.raises(ValueError):
        server._build_filter_expression({"bogus": 1})


def test_filter_expression_rejects_non_dict():
    with pytest.raises(ValueError):
        server._build_filter_expression("not-a-dict")


def test_order_bys_empty_when_none():
    assert server._build_order_bys(None) == []


def test_order_bys_builds_dimension_and_metric():
    obs = server._build_order_bys([
        {"dimension": {"dimension_name": "country"}, "desc": True},
        {"metric": {"metric_name": "activeUsers"}},
    ])
    assert obs[0].dimension.dimension_name == "country" and obs[0].desc is True
    assert obs[1].metric.metric_name == "activeUsers"


def test_order_bys_rejects_entry_without_known_key():
    with pytest.raises(ValueError):
        server._build_order_bys([{"nonsense": True}])


def test_redact_hides_bearer_private_key_and_client_secret():
    text = 'Authorization: Bearer abc.def "private_key": "-----BEGIN" "client_secret": "shh"'
    red = server._redact(text)
    assert "abc.def" not in red and "BEGIN" not in red and "shh" not in red


def test_fail_is_plain_runtime_error():
    with pytest.raises(RuntimeError, match=r"^ga4_request_failed: boom "):
        server._fail("ga4_run_report", "boom")


def test_no_private_envelope_in_source():
    import inspect
    src = inspect.getsource(server)
    assert "tool-outcome" + "/v1" not in src and "OUTCOME_SCHEMA" not in src
