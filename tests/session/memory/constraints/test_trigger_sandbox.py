from __future__ import annotations

import pytest

from openviking.session.memory.constraints import evaluate_trigger_code, validate_trigger_code
from openviking.session.memory.constraints.trigger_sandbox import TriggerValidationError


def test_trigger_allows_ctx_tool_predicate():
    code = """
def should_trigger(ctx):
    return ctx.get("candidate_tool") == "refund_order"
"""

    validate_trigger_code(code)

    assert evaluate_trigger_code(code, {"candidate_tool": "refund_order"}) is True
    assert evaluate_trigger_code(code, {"candidate_tool": "lookup_order"}) is False


def test_trigger_allows_for_loop_over_messages():
    code = """
def should_trigger(ctx):
    for message in ctx.get("messages", []):
        if "refund" in message.get("content", "").lower():
            return True
    return False
"""

    assert (
        evaluate_trigger_code(
            code,
            {
                "candidate_tool": "lookup_order",
                "messages": [
                    {"role": "user", "content": "Where is my package?"},
                    {"role": "user", "content": "I need a refund"},
                ],
            },
        )
        is True
    )


def test_trigger_allows_regex_helpers_after_tool_gate():
    code = r'''
def should_trigger(ctx):
    if ctx.get("candidate_tool") != "book_reservation":
        return False
    for message in ctx.get("messages", []):
        if regex_search(r"(book|预订).*(flight|航班)|(flight|航班).*(book|预订)", message.get("content", "")):
            return True
    return False
'''

    validate_trigger_code(code)

    assert (
        evaluate_trigger_code(
            code,
            {
                "candidate_tool": "book_reservation",
                "messages": [{"role": "user", "content": "请帮我预订明天的航班"}],
            },
        )
        is True
    )
    assert (
        evaluate_trigger_code(
            code,
            {
                "candidate_tool": "lookup_reservation",
                "messages": [{"role": "user", "content": "请帮我预订明天的航班"}],
            },
        )
        is False
    )


@pytest.mark.parametrize(
    "code",
    [
        "import os\ndef should_trigger(ctx):\n    return True",
        "def should_trigger(ctx):\n    return open('/tmp/x').read()",
        "def should_trigger(ctx):\n    while True:\n        pass",
        "def other(ctx):\n    return True",
    ],
)
def test_trigger_rejects_dangerous_or_invalid_code(code):
    with pytest.raises(TriggerValidationError):
        validate_trigger_code(code)
    assert evaluate_trigger_code(code, {}) is False


def test_trigger_runtime_error_or_non_bool_is_false():
    assert (
        evaluate_trigger_code(
            "def should_trigger(ctx):\n    return ctx['missing']",
            {},
        )
        is False
    )
    assert (
        evaluate_trigger_code(
            "def should_trigger(ctx):\n    return 'yes'",
            {},
        )
        is False
    )


def test_trigger_timeout_is_false():
    code = """
def should_trigger(ctx):
    total = 0
    for item in ctx.get("items", []):
        total = total + item
    return total > 0
"""

    assert (
        evaluate_trigger_code(code, {"items": list(range(200000))}, timeout_seconds=0.0001) is False
    )


def test_trigger_smoke_test_requires_bool_return():
    from openviking.session.memory.constraints import smoke_test_trigger_code
    from openviking.session.memory.constraints.trigger_sandbox import TriggerValidationError

    smoke_test_trigger_code("def should_trigger(ctx):\n    return False\n")
    with pytest.raises(TriggerValidationError, match="must return bool"):
        smoke_test_trigger_code('def should_trigger(ctx):\n    return "yes"\n')
