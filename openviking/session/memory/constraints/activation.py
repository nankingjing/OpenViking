# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Activation logic for conditional experience reminders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from openviking.session.memory.constraints.reminder import render_reminder_message
from openviking.session.memory.constraints.schema import (
    ConstraintExperience,
    build_trigger_context,
)
from openviking.session.memory.constraints.trigger_sandbox import evaluate_trigger_code


@dataclass(slots=True)
class ConstraintActivationInput:
    """Inputs for pre-tool-call constraint activation."""

    messages: list[Any]
    candidate_tool: str
    candidate_tool_args: Mapping[str, Any] | None
    experiences: Iterable[Any]
    reminded_exp_uris: set[str] = field(default_factory=set)
    timeout_seconds: float = 0.05


@dataclass(slots=True)
class ConstraintActivationResult:
    """Result of maybe appending one reminder message."""

    reminded: bool
    messages: list[Any]
    experience_uri: str | None = None
    experience_name: str | None = None
    reminder_message: dict[str, str] | None = None
    experience_uris: list[str] = field(default_factory=list)
    experience_names: list[str] = field(default_factory=list)
    reminder_messages: list[dict[str, str]] = field(default_factory=list)
    triggered_uris: list[str] = field(default_factory=list)
    event: dict[str, Any] | None = None


def select_triggered_experiences(
    *,
    experiences: Iterable[Any],
    ctx: dict[str, Any],
    reminded_exp_uris: set[str] | None = None,
    timeout_seconds: float = 0.05,
) -> list[ConstraintExperience]:
    """Return all trigger matches in retrieval order, excluding reminded URIs."""

    reminded = set(reminded_exp_uris or set())
    triggered: list[ConstraintExperience] = []
    for raw_exp in experiences or []:
        exp = _coerce_experience(raw_exp)
        if exp is None or exp.uri in reminded:
            continue
        if evaluate_trigger_code(exp.trigger_code, ctx, timeout_seconds=timeout_seconds):
            triggered.append(exp)
    return triggered


def select_triggered_experience(
    *,
    experiences: Iterable[Any],
    ctx: dict[str, Any],
    reminded_exp_uris: set[str] | None = None,
    timeout_seconds: float = 0.05,
) -> tuple[ConstraintExperience | None, list[ConstraintExperience]]:
    """Backward-compatible helper returning the first trigger match."""

    triggered = select_triggered_experiences(
        experiences=experiences,
        ctx=ctx,
        reminded_exp_uris=reminded_exp_uris,
        timeout_seconds=timeout_seconds,
    )
    return (triggered[0] if triggered else None), triggered


def apply_experience_constraint_reminder(
    activation_input: ConstraintActivationInput,
) -> ConstraintActivationResult:
    """Append one reminder user message when a candidate experience triggers.

    The caller owns rerunning the agent step.  This function only appends a
    normal user-level message and marks the URI as reminded in the provided set.
    """

    original_messages = list(activation_input.messages or [])
    ctx = build_trigger_context(
        messages=original_messages,
        candidate_tool=activation_input.candidate_tool,
        candidate_tool_args=activation_input.candidate_tool_args,
    )
    triggered = select_triggered_experiences(
        experiences=activation_input.experiences,
        ctx=ctx,
        reminded_exp_uris=activation_input.reminded_exp_uris,
        timeout_seconds=activation_input.timeout_seconds,
    )
    triggered_uris = [exp.uri for exp in triggered]
    if not triggered:
        return ConstraintActivationResult(
            reminded=False,
            messages=original_messages,
            triggered_uris=triggered_uris,
        )

    reminders = [
        render_reminder_message(exp, candidate_tool=activation_input.candidate_tool)
        for exp in triggered
    ]
    for exp in triggered:
        activation_input.reminded_exp_uris.add(exp.uri)
    event = {
        "type": "experience_constraint_reminder",
        "experience_uris": triggered_uris,
        "experience_names": [exp.name for exp in triggered],
        "candidate_tool": str(activation_input.candidate_tool or ""),
        "triggered_uris": triggered_uris,
    }
    first = triggered[0]
    return ConstraintActivationResult(
        reminded=True,
        messages=[*original_messages, *reminders],
        experience_uri=first.uri,
        experience_name=first.name,
        reminder_message=reminders[0],
        experience_uris=triggered_uris,
        experience_names=[exp.name for exp in triggered],
        reminder_messages=reminders,
        triggered_uris=triggered_uris,
        event=event,
    )


def _coerce_experience(raw_exp: Any) -> ConstraintExperience | None:
    if isinstance(raw_exp, ConstraintExperience):
        return raw_exp
    exp = ConstraintExperience.from_policy(raw_exp)
    if exp is not None:
        return exp
    return ConstraintExperience.from_memory_file(raw_exp)
