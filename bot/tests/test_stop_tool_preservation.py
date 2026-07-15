"""Tests for stop-tool empty-reply preservation in AgentLoop._run_agent_loop.

When the model returns empty final content, that empty answer must be preserved
(instead of replaced by a filler string) whenever a stop tool was used ANYWHERE
in the turn -- not only when the stop tool happened to be the LAST tool in a
batch. The fix replaces a ``tools_used[-1]`` positional check with ``any(...)``
over the whole ``tools_used`` list, and also guards the post-loop filler
fallback (the second ``final_content`` guard near the end of the method) against
overwriting a legitimate stop-tool empty reply.

The decision is buried in a ~300-line async method that drives LLM streaming and
tool execution, so exercising it end-to-end would require heavy mocking of every
collaborator. Instead we (1) verify the real source uses the position-independent
``any(...)`` predicate at BOTH guard sites, and (2) document the intended
semantics with behavioral checks on an equivalent predicate covering all
batch positions and edge cases.
"""

import inspect

from vikingbot.agent.loop import AgentLoop


def _stop_reply_preserved(final_content, tools_used, stop_tools):
    """Mirror of the fixed guard: preserve an empty reply if any stop tool ran."""
    return final_content == "" and any(
        t.get("tool_name") in stop_tools for t in tools_used
    )


# ---------------------------------------------------------------------------
# Source-code assertions: verify the real implementation uses the correct
# position-independent ``any(...)`` pattern at every relevant guard site.
# ---------------------------------------------------------------------------


def test_source_uses_position_independent_any_check():
    """The line-978 guard must use ``any(...)``, not positional indexing."""
    src = inspect.getsource(AgentLoop._run_agent_loop)
    assert 'any(t.get("tool_name") in stop_tools for t in tools_used)' in src
    # The old, batch-position-sensitive form must be gone.
    assert 'tools_used[-1].get("tool_name") in stop_tools' not in src


def test_source_guards_post_loop_filler_with_stop_tool_check():
    """The line-1019 filler fallback must also respect stop-tool usage.

    Even after the primary guard at line 978 preserves the empty content, a
    second ``final_content`` check near the end of the method unconditionally
    replaces an empty or None ``final_content`` with a filler message.  That
    second guard must skip the replacement when any stop tool was used,
    otherwise the stop-tool empty reply is always clobbered.
    """
    src = inspect.getsource(AgentLoop._run_agent_loop)
    # The second guard must also reference stop_tools to avoid overwriting
    # a legitimate stop-tool empty reply.
    assert 'not any(t.get("tool_name") in stop_tools for t in tools_used)' in src, (
        "The post-loop filler guard (near the end of _run_agent_loop) must "
        "check stop_tools before replacing empty final_content."
    )


# ---------------------------------------------------------------------------
# Predicate behavioral tests: cover every position in a multi-tool batch and
# all relevant edge cases.
# ---------------------------------------------------------------------------

STOP_TOOLS = {"finish"}


def test_stop_tool_first_in_batch():
    """Stop tool is the very first entry in a multi-tool batch."""
    tools_used = [
        {"tool_name": "finish"},
        {"tool_name": "search"},
        {"tool_name": "read_file"},
    ]
    assert _stop_reply_preserved("", tools_used, STOP_TOOLS) is True


def test_stop_tool_middle_in_batch():
    """Stop tool is surrounded by other tools."""
    tools_used = [
        {"tool_name": "search"},
        {"tool_name": "finish"},
        {"tool_name": "read_file"},
    ]
    assert _stop_reply_preserved("", tools_used, STOP_TOOLS) is True


def test_stop_tool_last_in_batch():
    """Stop tool is the last entry (the case the original positional check
    happened to cover by accident)."""
    tools_used = [
        {"tool_name": "search"},
        {"tool_name": "read_file"},
        {"tool_name": "finish"},
    ]
    assert _stop_reply_preserved("", tools_used, STOP_TOOLS) is True


def test_stop_tool_only_tool():
    """Batch contains exactly one tool and it is the stop tool."""
    tools_used = [{"tool_name": "finish"}]
    assert _stop_reply_preserved("", tools_used, STOP_TOOLS) is True


def test_multiple_stop_tools_in_batch():
    """Batch contains more than one stop tool (should still detect)."""
    tools_used = [
        {"tool_name": "finish"},
        {"tool_name": "search"},
        {"tool_name": "finish"},
    ]
    assert _stop_reply_preserved("", tools_used, STOP_TOOLS) is True


def test_empty_reply_not_preserved_without_stop_tool():
    """No stop tool anywhere in the batch -- empty reply should NOT be preserved."""
    tools_used = [{"tool_name": "search"}, {"tool_name": "read_file"}]
    assert _stop_reply_preserved("", tools_used, STOP_TOOLS) is False


def test_empty_reply_not_preserved_with_empty_tools_used():
    """tools_used is empty (e.g. the loop had no tool-call iterations)."""
    assert _stop_reply_preserved("", [], STOP_TOOLS) is False


def test_non_empty_final_content_not_preserved_even_with_stop_tool():
    """When final_content is not empty, the guard is irrelevant and should
    return False regardless of stop tools."""
    tools_used = [
        {"tool_name": "finish"},
        {"tool_name": "search"},
    ]
    assert _stop_reply_preserved("Actual reply text", tools_used, STOP_TOOLS) is False


def test_stop_tool_with_different_stop_set():
    """Stop-tool names are drawn from the caller-provided set, not hard-coded."""
    custom_stop = {"terminate", "abort"}
    tools_used = [
        {"tool_name": "search"},
        {"tool_name": "terminate"},
    ]
    assert _stop_reply_preserved("", tools_used, custom_stop) is True


def test_empty_stop_set_never_preserves():
    """An empty stop_tools set means no tool counts as a stop tool."""
    tools_used = [{"tool_name": "finish"}, {"tool_name": "search"}]
    assert _stop_reply_preserved("", tools_used, set()) is False
