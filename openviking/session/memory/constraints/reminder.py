# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Reminder message rendering for triggered constraint experiences."""

from __future__ import annotations

from openviking.session.memory.constraints.schema import ConstraintExperience


def _render_structured_experience_reminder(
    experience: ConstraintExperience,
    *,
    candidate_tool: str,
) -> str:
    tool_name = str(candidate_tool or "某个工具/方法").strip() or "某个工具/方法"
    return (
        "<experience_reminder>\n"
        f"<experience_name>{experience.name}</experience_name>\n"
        f"<experience_uri>{experience.uri}</experience_uri>\n"
        f"<triggered_before_tool>{tool_name}</triggered_before_tool>\n"
        "<instruction>\n"
        "下面是一条经验 reminder，它是在你可能要调用上述工具/方法前被触发的。\n"
        "请先参考这段经验，再决定下一步是否以及如何调用该工具/方法。\n"
        "当前系统规则、用户事实和工具结果优先于这段经验。\n"
        "</instruction>\n"
        "<experience>\n"
        f"{experience.constraint}\n"
        "</experience>\n"
        "</experience_reminder>"
    )


def render_reminder_text(
    experience: ConstraintExperience,
    *,
    candidate_tool: str,
) -> str:
    """Render the structured user-level reminder text plus activation context."""

    return _render_structured_experience_reminder(
        experience,
        candidate_tool=candidate_tool,
    )


def render_reminder_message(
    experience: ConstraintExperience,
    *,
    candidate_tool: str,
) -> dict[str, str]:
    """Return the user message appended before rerunning the agent step."""

    return {
        "role": "user",
        "content": render_reminder_text(
            experience,
            candidate_tool=candidate_tool,
        ),
    }
