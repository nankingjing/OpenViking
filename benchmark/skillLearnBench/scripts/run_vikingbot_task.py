#!/usr/bin/env python3
"""Run one SkillLearnBench task with host-side VikingBot.

The adapter keeps SkillLearnBench's Docker task environment and verifier intact,
while replacing the in-container coding agent with VikingBot. VikingBot runs in
this OpenViking checkout and receives tools that proxy file/shell operations into
the running task container.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BENCH_DIR = SCRIPT_DIR.parent
REPO_ROOT = BENCH_DIR.parents[1]

for import_root in (REPO_ROOT, REPO_ROOT / "bot"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

VIKINGBOT_IMPORT_ERROR: Exception | None = None
AgentLoop = Any
MessageBus = None
SessionKey = None
SessionManager = None
ensure_config = None
_init_bot_data = None
_make_provider = None
ToolContext = Any


class Tool:
    """Small protocol-compatible base for the Docker-backed tools."""

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        return []

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


DEFAULT_TASK_ID = "anthropic-poster-design/anthropic-poster-design-1"
DEFAULT_ROOT_CANDIDATES = [
    Path(os.environ["SKILLLEARNBENCH_ROOT"]).expanduser()
    if os.environ.get("SKILLLEARNBENCH_ROOT")
    else None,
    Path("/private/tmp/SkillLearnBench"),
    REPO_ROOT / "SkillLearnBench",
    BENCH_DIR / "SkillLearnBench",
]

TEXT_FILE_LIMIT = 200_000
OPENVIKING_MESSAGE_BATCH_SIZE = 100
SEARCH_RESOLUTION_CONTEXT_LIMIT = 28_000


def _default_openviking_config_path() -> Path:
    raw = os.environ.get("OPENVIKING_CONFIG_FILE")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".openviking" / "ov.conf").resolve()


def _result_tail(stdout: str, stderr: str, limit: int = 1200) -> str:
    text = ""
    if stdout.strip():
        text += stdout.strip()
    if stderr.strip():
        text += ("\n" if text else "") + stderr.strip()
    return text[-limit:]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _json_dumps(value)


def _truncate_text(value: Any, limit: int) -> str:
    text = _content_to_text(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n... (truncated, {len(text) - limit} more chars)"


def _float_or_none(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _require_vikingbot_runtime() -> None:
    global VIKINGBOT_IMPORT_ERROR
    global AgentLoop
    global MessageBus
    global SessionKey
    global SessionManager
    global ensure_config
    global _init_bot_data
    global _make_provider

    if MessageBus is not None:
        return

    try:
        from vikingbot.agent.loop import AgentLoop as LoadedAgentLoop
        from vikingbot.bus.queue import MessageBus as LoadedMessageBus
        from vikingbot.cli.commands import _init_bot_data as loaded_init_bot_data
        from vikingbot.cli.commands import _make_provider as loaded_make_provider
        from vikingbot.config.loader import ensure_config as loaded_ensure_config
        from vikingbot.config.schema import SessionKey as LoadedSessionKey
        from vikingbot.session.manager import SessionManager as LoadedSessionManager
    except ModuleNotFoundError as exc:
        VIKINGBOT_IMPORT_ERROR = exc

    if VIKINGBOT_IMPORT_ERROR is not None:
        raise RuntimeError(
            "VikingBot runtime dependencies are not importable. Run this script "
            "with the OpenViking virtualenv or install the bot dependencies first. "
            f"Original import error: {VIKINGBOT_IMPORT_ERROR!r}"
        ) from VIKINGBOT_IMPORT_ERROR

    AgentLoop = LoadedAgentLoop
    MessageBus = LoadedMessageBus
    SessionKey = LoadedSessionKey
    SessionManager = LoadedSessionManager
    ensure_config = loaded_ensure_config
    _init_bot_data = loaded_init_bot_data
    _make_provider = loaded_make_provider


def _check_vikingbot_runtime(config_path: Path) -> tuple[bool, str]:
    old_config = os.environ.get("OPENVIKING_CONFIG_FILE")
    os.environ["OPENVIKING_CONFIG_FILE"] = str(config_path)
    try:
        _require_vikingbot_runtime()
    except Exception as exc:
        return False, repr(exc)
    finally:
        if old_config is None:
            os.environ.pop("OPENVIKING_CONFIG_FILE", None)
        else:
            os.environ["OPENVIKING_CONFIG_FILE"] = old_config
    return True, "ok"


def _check_openviking_llm_config(config_path: Path) -> tuple[bool, str]:
    if not config_path.exists():
        return False, str(config_path)

    old_config = os.environ.get("OPENVIKING_CONFIG_FILE")
    os.environ["OPENVIKING_CONFIG_FILE"] = str(config_path)
    try:
        _require_vikingbot_runtime()
        config = ensure_config(config_path)
        model = config.agents.model
        provider = config.agents.provider or "(auto)"
        api_key_present = bool(config.agents.api_key)
        api_base_present = bool(config.agents.api_base)
    except Exception as exc:
        return False, repr(exc)
    finally:
        if old_config is None:
            os.environ.pop("OPENVIKING_CONFIG_FILE", None)
        else:
            os.environ["OPENVIKING_CONFIG_FILE"] = old_config

    if not model:
        return False, "model is missing in ov.conf"
    if not api_key_present and not model.startswith("bedrock/"):
        return (
            False,
            f"model={model}, provider={provider}, api_key=missing, api_base_present={api_base_present}",
        )
    return (
        True,
        f"model={model}, provider={provider}, api_key=present, api_base_present={api_base_present}",
    )


def _check_openviking_memory_user_key_config(config_path: Path) -> tuple[bool, str]:
    if not config_path.exists():
        return False, str(config_path)

    old_config = os.environ.get("OPENVIKING_CONFIG_FILE")
    os.environ["OPENVIKING_CONFIG_FILE"] = str(config_path)
    try:
        _require_vikingbot_runtime()
        config = ensure_config(config_path)
        ov_server = config.ov_server
        mode = str(ov_server.mode or "").lower()
        api_key_type = str(ov_server.api_key_type or "").lower()
        api_key_present = bool(ov_server.api_key)
        server_url = ov_server.server_url
    except Exception as exc:
        return False, repr(exc)
    finally:
        if old_config is None:
            os.environ.pop("OPENVIKING_CONFIG_FILE", None)
        else:
            os.environ["OPENVIKING_CONFIG_FILE"] = old_config

    if mode == "local":
        return True, f"mode=local, server_url={server_url}"
    if api_key_type != "user":
        return False, f"mode={mode}, api_key_type={api_key_type}; expected user"
    if not api_key_present:
        return False, f"mode={mode}, api_key_type=user, api_key=missing"
    return True, f"mode={mode}, api_key_type=user, api_key=present, server_url={server_url}"


def _tool_call_id(tool_call: dict[str, Any], fallback_index: int) -> str:
    raw = tool_call.get("id") or f"tool-call-{fallback_index}"
    return _safe_name(str(raw)) or f"tool-call-{fallback_index}"


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    return str(fn.get("name") or tool_call.get("name") or "").strip()


def _tool_call_input(tool_call: dict[str, Any]) -> Any:
    fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    raw_args = fn.get("arguments") if "arguments" in fn else tool_call.get("arguments")
    parsed = _safe_json_loads(raw_args or {})
    if isinstance(parsed, dict):
        return parsed
    return {"raw_args": parsed}


def _tool_result_messages(messages: list[Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if tool_call_id:
            results[str(tool_call_id)] = message
    return results


def _next_tool_execution(
    tools_used: list[Any],
    start_index: int,
    tool_name: str,
) -> tuple[dict[str, Any] | None, int]:
    for index in range(start_index, len(tools_used)):
        item = tools_used[index]
        if not isinstance(item, dict):
            continue
        if str(item.get("tool_name") or "") == tool_name:
            return item, index + 1
    return None, start_index


def _tool_output_from_sources(
    tool_result: dict[str, Any] | None,
    tool_execution: dict[str, Any] | None,
) -> str:
    if tool_result is not None and "content" in tool_result:
        return _content_to_text(tool_result.get("content"))
    if tool_execution is not None and "result" in tool_execution:
        return _content_to_text(tool_execution.get("result"))
    return ""


def _tool_status(tool_execution: dict[str, Any] | None, output: str) -> str:
    if tool_execution is not None and tool_execution.get("execute_success") is False:
        return "error"
    if output.startswith("Error:") or "\nExit code: " in output or output.startswith("STDERR:\n"):
        return "error"
    return "completed"


def _trajectory_memory_policy() -> dict[str, Any]:
    return {
        "self": {"enabled": True},
        "peer": {"enabled": True},
    }


def _trajectory_memory_session_id(
    *,
    skill_config: str,
    task_id: str,
    trial_id: str,
    explicit_session_id: str | None,
) -> str:
    if explicit_session_id:
        return explicit_session_id
    task_name, subtask_name = Path(task_id).parts[-2:]
    return _safe_name(f"slb-{skill_config}-{task_name}-{subtask_name}-{trial_id}")


def _trajectory_memory_peer_id(task_id: str) -> str:
    task_name, subtask_name = Path(task_id).parts[-2:]
    return _safe_name(f"slb-{task_name}-{subtask_name}")


def _trajectory_to_openviking_messages(
    payload: dict[str, Any],
    *,
    task_id: str,
    trial_id: str,
    skill_config: str,
    session_id: str,
    peer_id: str,
) -> list[dict[str, Any]]:
    """Convert a VikingBot trajectory payload into OpenViking session messages."""
    source_messages = payload.get("messages") or []
    if not isinstance(source_messages, list):
        source_messages = []
    tool_results = _tool_result_messages(source_messages)
    tools_used = payload.get("tools_used") or []
    if not isinstance(tools_used, list):
        tools_used = []

    base_time = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    converted: list[dict[str, Any]] = []
    tool_execution_index = 0

    def created_at() -> str:
        return (base_time + dt.timedelta(seconds=len(converted))).isoformat().replace("+00:00", "Z")

    def add_text_message(role: str, text: str) -> None:
        if not text.strip():
            return
        message: dict[str, Any] = {
            "role": role,
            "parts": [{"type": "text", "text": text}],
            "created_at": created_at(),
            "peer_id": peer_id,
        }
        converted.append(message)

    header = {
        "task_id": task_id,
        "trial_id": trial_id,
        "skill_config": skill_config,
        "source": "SkillLearnBench VikingBot trajectory",
        "peer_id": peer_id,
        "trajectory_fields": [
            "messages",
            "assistant tool_calls",
            "tool results",
            "tools_used",
            "final_content",
            "token_usage",
            "iteration",
        ],
        "tools_used_mapping": {
            "tool_name": "ToolPart.tool_name",
            "args": "ToolPart.tool_input",
            "result": "ToolPart.tool_output",
            "duration": "ToolPart.duration_ms",
            "execute_success": "ToolPart.tool_status",
        },
    }
    add_text_message("user", "SkillLearnBench trajectory import metadata:\n" + _json_dumps(header))

    for index, source_message in enumerate(source_messages, start=1):
        if not isinstance(source_message, dict):
            continue
        source_role = str(source_message.get("role") or "").strip().lower()
        if source_role == "tool":
            continue

        content = _content_to_text(source_message.get("content")).strip()

        if source_role == "system":
            add_text_message(
                "user",
                f"[original_role=system][message_index={index}]\n{content}",
            )
            continue

        if source_role == "user":
            add_text_message(
                "user",
                f"[message_index={index}]\n{content}" if content else f"[message_index={index}]",
            )
            continue

        if source_role != "assistant":
            add_text_message(
                "user",
                f"[original_role={source_role or 'unknown'}][message_index={index}]\n{content}",
            )
            continue

        parts: list[dict[str, Any]] = []
        if content:
            parts.append({"type": "text", "text": f"[message_index={index}]\n{content}"})

        for call_offset, tool_call in enumerate(source_message.get("tool_calls") or [], start=1):
            if not isinstance(tool_call, dict):
                continue
            tool_name = _tool_call_name(tool_call)
            if not tool_name:
                continue
            raw_tool_id = str(tool_call.get("id") or "")
            tool_id = _tool_call_id(tool_call, len(parts) + call_offset)
            tool_input = _tool_call_input(tool_call)
            tool_execution, tool_execution_index = _next_tool_execution(
                tools_used,
                tool_execution_index,
                tool_name,
            )
            result_message = tool_results.get(raw_tool_id)
            tool_output = _tool_output_from_sources(result_message, tool_execution)
            parts.append(
                {
                    "type": "tool",
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "tool_uri": f"viking://session/{session_id}/tools/{tool_id}",
                    "tool_input": tool_input,
                    "tool_output": tool_output,
                    "tool_status": _tool_status(tool_execution, tool_output),
                    "duration_ms": (
                        _float_or_none(tool_execution.get("duration"))
                        if isinstance(tool_execution, dict)
                        else None
                    ),
                    "prompt_tokens": (
                        tool_execution.get("input_token")
                        if isinstance(tool_execution, dict)
                        else None
                    ),
                    "completion_tokens": (
                        tool_execution.get("output_token")
                        if isinstance(tool_execution, dict)
                        else None
                    ),
                }
            )

        if parts:
            converted.append(
                {
                    "role": "assistant",
                    "parts": parts,
                    "created_at": created_at(),
                    "peer_id": peer_id,
                }
            )

    consumed_tool_ids = {
        str(tool_call.get("id"))
        for message in source_messages
        if isinstance(message, dict)
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict) and tool_call.get("id")
    }
    for tool_call_id, tool_message in sorted(tool_results.items()):
        if tool_call_id in consumed_tool_ids:
            continue
        add_text_message(
            "user",
            (
                f"[original_role=tool][tool_call_id={tool_call_id}]"
                f"[name={tool_message.get('name') or ''}]\n"
                f"{_content_to_text(tool_message.get('content'))}"
            ),
        )

    final_content = _content_to_text(payload.get("final_content")).strip()
    if final_content:
        add_text_message("assistant", "[final_content]\n" + final_content)

    summary_payload = {
        "task_id": task_id,
        "trial_id": trial_id,
        "iteration": payload.get("iteration"),
        "token_usage": payload.get("token_usage") or {},
        "reasoning_content_present": bool(payload.get("reasoning_content")),
        "tools_used_count": len(tools_used),
        "tools_used": [
            {
                "index": idx,
                "tool_name": item.get("tool_name"),
                "args": _safe_json_loads(item.get("args")),
                "duration": item.get("duration"),
                "execute_success": item.get("execute_success"),
                "input_token": item.get("input_token"),
                "output_token": item.get("output_token"),
                "result_chars": len(_content_to_text(item.get("result"))),
            }
            for idx, item in enumerate(tools_used, start=1)
            if isinstance(item, dict)
        ],
    }
    add_text_message(
        "assistant",
        "[trajectory_structured_summary]\n" + _json_dumps(summary_payload),
    )
    return converted


async def _wait_openviking_task(
    client: Any,
    task_id: str,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_task: dict[str, Any] | None = None
    while time.time() < deadline:
        task = await client.get_task(task_id)
        if isinstance(task, dict):
            last_task = task
            status = task.get("status")
            if status in {"completed", "failed", "cancelled", "unknown"}:
                return task
        await asyncio.sleep(2)
    raise TimeoutError(f"OpenViking memory task {task_id} timed out after {timeout}s: {last_task}")


async def _commit_trajectory_memory(
    *,
    config_path: Path,
    trajectory_path: Path,
    task_id: str,
    trial_id: str,
    skill_config: str,
    session_id: str,
    wait_task: bool,
    task_timeout: int,
) -> dict[str, Any]:
    os.environ["OPENVIKING_CONFIG_FILE"] = str(config_path)
    _require_vikingbot_runtime()
    from vikingbot.openviking_mount.ov_server import VikingClient

    peer_id = _trajectory_memory_peer_id(task_id)
    payload = _load_json_file(trajectory_path)
    messages = _trajectory_to_openviking_messages(
        payload,
        task_id=task_id,
        trial_id=trial_id,
        skill_config=skill_config,
        session_id=session_id,
        peer_id=peer_id,
    )
    memory_policy = _trajectory_memory_policy()
    client = await VikingClient.create(workspace_id="skilllearnbench-memory-import")
    try:
        if client.mode == "remote" and client.api_key_type != "user":
            raise RuntimeError(
                "SkillLearnBench trajectory memory import requires bot.ov_server.api_key_type='user' "
                "and a User API key in ov.conf."
            )
        session_user_id = client.session_owner_user_id()
        await client.ensure_session(
            session_id, user_id=session_user_id, memory_policy=memory_policy
        )
        session_client = await client._session_client_for_user(session_user_id)
        total_added = 0
        message_count = 0
        for start in range(0, len(messages), OPENVIKING_MESSAGE_BATCH_SIZE):
            batch = messages[start : start + OPENVIKING_MESSAGE_BATCH_SIZE]
            append_result = await session_client.batch_add_messages(session_id, batch)
            total_added += int(append_result.get("added", 0) or 0)
            message_count = int(append_result.get("message_count", message_count) or 0)
        commit_result = await client.commit_session(
            session_id,
            user_id=session_user_id,
            memory_policy=memory_policy,
        )
        task_result = None
        task_id_value = commit_result.get("task_id") if isinstance(commit_result, dict) else None
        if wait_task and task_id_value:
            task_result = await _wait_openviking_task(
                session_client, str(task_id_value), task_timeout
            )
        return {
            "success": True,
            "session_id": session_id,
            "peer_id": peer_id,
            "message_count": message_count,
            "added": total_added,
            "memory_policy": memory_policy,
            "commit": commit_result,
            "task": task_result,
        }
    finally:
        await client.close()


def _build_search_resolution_query(
    *,
    task_id: str,
    instruction: str,
    hidden_verifier: bool,
) -> str:
    verifier_text = (
        "The verifier tests are hidden from the agent during solving."
        if hidden_verifier
        else "The verifier tests may be inspected by the agent during solving."
    )
    return (
        "Resolve reusable OpenViking memory and skill guidance for this SkillLearnBench "
        f"task instance.\n\nTask id: {task_id}\n{verifier_text}\n\n"
        "The agent will solve the task inside a Docker container with only shell and "
        "file tools. Return concise, executable guidance that may improve the agent's "
        "trajectory. Prefer memories from the same task instance peer and do not invent "
        "requirements beyond the current instruction.\n\n"
        "Instruction:\n"
        f"{instruction}"
    )


def _build_search_resolution_session_context(
    *,
    task_id: str,
    peer_id: str,
    container_workdir: str,
    hidden_verifier: bool,
) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": (
                "SkillLearnBench execution context:\n"
                f"- task_id: {task_id}\n"
                f"- openviking_peer_id: {peer_id}\n"
                f"- container_workdir: {container_workdir}\n"
                f"- hidden_verifier: {hidden_verifier}\n"
                "- available_agent_tools: exec, read_file, write_file, edit_file, list_dir\n"
                "- retrieval_scope: current User API key; current task instance peer memory"
            ),
        }
    ]


def _search_resolution_limits(args: argparse.Namespace) -> dict[str, int]:
    return {
        "user_memory": args.resolution_user_memory_limit,
        "experiences": args.resolution_experiences_limit,
        "tools_memory": args.resolution_tools_memory_limit,
        "skills": args.resolution_skills_limit,
        "skills_memory": args.resolution_skills_memory_limit,
        "trajectory_grounding": args.resolution_trajectory_grounding_limit,
        "pack_max_tokens": args.resolution_pack_max_tokens,
    }


def _search_resolution_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "return_markdown": True,
        "return_structured": True,
        "allow_trajectory_grounding": args.resolution_allow_trajectory_grounding,
        "skill_content_mode": args.resolution_skill_content_mode,
    }


def _format_search_resolution_context(
    resolution_result: dict[str, Any],
    *,
    peer_id: str,
    limit: int = SEARCH_RESOLUTION_CONTEXT_LIMIT,
) -> str:
    pack = ""
    if isinstance(resolution_result, dict):
        pack = str(resolution_result.get("pack_markdown") or "").strip()
    if not pack:
        pack = _json_dumps(resolution_result)
    selected_context = (
        resolution_result.get("selected_context") if isinstance(resolution_result, dict) else {}
    )
    selected_counts = {
        key: len(value)
        for key, value in (selected_context or {}).items()
        if isinstance(value, list)
    }
    header = {
        "source": "OpenViking /api/v1/search/resolution",
        "resolution_id": resolution_result.get("resolution_id")
        if isinstance(resolution_result, dict)
        else None,
        "peer_id": peer_id,
        "selected_counts": selected_counts,
    }
    text = (
        "Read-only auxiliary task-solving context follows. Treat it as prior "
        "memory/skill guidance for this benchmark instance, not as a verifier answer. "
        "Use it only when it is consistent with the current task files and instruction. "
        "This context does not add tools. Do not call openviking_memory_commit, "
        "openviking_search, openviking_* tools, memory tools, or any tool outside "
        "the explicitly allowed Docker file/shell tools. The benchmark runner records "
        "memory after you finish; your job is only to solve the task inside the container.\n\n"
        "Metadata:\n"
        f"{_json_dumps(header)}\n\n"
        f"{pack}\n\n"
        "Reminder: the context above is read-only. Do not call or invent OpenViking "
        "or memory tools; finish the task using only the allowed Docker tools."
    )
    return _truncate_text(text, limit)


def _search_resolution_summary(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    selected_context = result.get("selected_context") if isinstance(result, dict) else {}
    selected_counts = {
        key: len(value)
        for key, value in (selected_context or {}).items()
        if isinstance(value, list)
    }
    return {
        "success": payload.get("success"),
        "peer_id": payload.get("peer_id"),
        "agent_space": payload.get("agent_space"),
        "resolution_id": result.get("resolution_id") if isinstance(result, dict) else None,
        "pack_markdown_chars": len(str(result.get("pack_markdown") or ""))
        if isinstance(result, dict)
        else 0,
        "selected_counts": selected_counts,
        "query_chars": len(str(payload.get("query") or "")),
    }


def _strip_vikingbot_default_tool_profile(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    conflicting_markers = (
        "# vikingbot",
        "OpenViking Knowledge Base",
        "openviking_memory_commit",
        "openviking_search",
    )
    for message in messages:
        if message.get("role") == "system":
            content = str(message.get("content") or "")
            if any(marker in content for marker in conflicting_markers):
                continue
        cleaned.append(message)
    return cleaned


async def _fetch_search_resolution_context(
    *,
    config_path: Path,
    task_id: str,
    instruction: str,
    peer_id: str,
    container_workdir: str,
    hidden_verifier: bool,
    agent_space: str,
    include_debug: bool,
    limits: dict[str, int],
    options: dict[str, Any],
) -> dict[str, Any]:
    os.environ["OPENVIKING_CONFIG_FILE"] = str(config_path)
    _require_vikingbot_runtime()
    from vikingbot.openviking_mount.ov_server import VikingClient

    client = await VikingClient.create(workspace_id="skilllearnbench-resolution")
    try:
        if client.mode == "remote" and client.api_key_type != "user":
            raise RuntimeError(
                "SkillLearnBench search-resolution injection requires "
                "bot.ov_server.api_key_type='user' and a User API key in ov.conf."
            )
        query = _build_search_resolution_query(
            task_id=task_id,
            instruction=instruction,
            hidden_verifier=hidden_verifier,
        )
        session_context = _build_search_resolution_session_context(
            task_id=task_id,
            peer_id=peer_id,
            container_workdir=container_workdir,
            hidden_verifier=hidden_verifier,
        )
        result = await client.client.search_resolution(
            query=query,
            agent_space=agent_space,
            peer_ids=[peer_id],
            session_context=session_context,
            include_debug=include_debug,
            limits=limits,
            options=options,
        )
        context = _format_search_resolution_context(result, peer_id=peer_id)
        return {
            "success": True,
            "peer_id": peer_id,
            "agent_space": agent_space,
            "query": query,
            "session_context": session_context,
            "limits": limits,
            "options": options,
            "result": result,
            "context": context,
        }
    finally:
        await client.close()


def _run(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 1800,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
        env=env,
    )


def _run_streaming(
    cmd: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    """Run a long command while mirroring output to logs and the parent process."""
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_f,
        stderr_path.open("w", encoding="utf-8") as stderr_f,
    ):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def pump(stream: Any, sink: Any, mirror: Any, chunks: list[str]) -> None:
            for line in stream:
                sink.write(line)
                sink.flush()
                chunks.append(line)
                if len(chunks) > 200:
                    del chunks[: len(chunks) - 200]
                print(line, end="", file=mirror, flush=True)

        stdout_thread = threading.Thread(
            target=pump, args=(proc.stdout, stdout_f, sys.stdout, stdout_chunks)
        )
        stderr_thread = threading.Thread(
            target=pump, args=(proc.stderr, stderr_f, sys.stderr, stderr_chunks)
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            raise
        stdout_thread.join()
        stderr_thread.join()

    return subprocess.CompletedProcess(
        cmd,
        returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )


def _run_optional(cmd: list[str], *, timeout: int = 60) -> tuple[bool, str]:
    try:
        proc = _run(cmd, timeout=timeout)
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired as exc:
        return False, f"timeout after {exc.timeout}s"
    return proc.returncode == 0, _result_tail(proc.stdout or "", proc.stderr or "")


def _resolve_required_env_value(name: str) -> tuple[str | None, str | None]:
    value = os.environ.get(name)
    if value:
        return value, "environment"

    if name == "GH_TOKEN" and shutil.which("gh"):
        try:
            proc = _run(["gh", "auth", "token"], timeout=30)
        except Exception:
            return None, None
        token = (proc.stdout or "").strip()
        if proc.returncode == 0 and token:
            return token, "gh auth token"

    return None, None


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower() or "task"


def _timestamp_id() -> str:
    return f"{dt.datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _resolve_skilllearnbench_root(raw: str | None) -> Path:
    if raw:
        root = Path(raw).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"SkillLearnBench root not found: {root}")
        return root
    for candidate in DEFAULT_ROOT_CANDIDATES:
        if candidate and candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(
        "SkillLearnBench root not found. Pass --skilllearnbench-root or set SKILLLEARNBENCH_ROOT."
    )


def _load_task_toml_text(task_path: Path) -> str:
    toml_path = task_path / "task.toml"
    return toml_path.read_text(encoding="utf-8", errors="replace") if toml_path.exists() else ""


def _extract_toml_list(raw: str, key: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(rf"{re.escape(key)}\s*=\s*\[([^\]]*)\]", raw):
        values.extend(re.findall(r"""["']([^"']+)["']""", match.group(1)))
    return values


def _parse_workdir(dockerfile: Path) -> str:
    if not dockerfile.exists():
        return "/root"
    workdir = "/root"
    env: dict[str, str] = {}
    for raw in dockerfile.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("ENV "):
            payload = line[4:].strip()
            if "=" in payload:
                key, val = payload.split("=", 1)
                env[key.strip()] = val.strip()
        elif line.startswith("WORKDIR "):
            value = line[8:].strip()
            for key, val in env.items():
                value = value.replace(f"${{{key}}}", val).replace(f"${key}", val)
            workdir = value
    return workdir


def _copy_skills_to_dest(src: Path, dest_skills_dir: Path) -> bool:
    """Normalize SkillLearnBench skill layouts into workspace/skills."""
    if not src.exists() or not src.is_dir():
        return False
    if dest_skills_dir.exists():
        shutil.rmtree(dest_skills_dir)
    dest_skills_dir.mkdir(parents=True, exist_ok=True)

    child_skill_dirs = [d for d in src.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
    if child_skill_dirs:
        for d in child_skill_dirs:
            shutil.copytree(d, dest_skills_dir / d.name)
        return True

    root_skill = src / "SKILL.md"
    if root_skill.exists():
        target = dest_skills_dir / src.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root_skill, target / "SKILL.md")
        for child in src.iterdir():
            if child.name == "SKILL.md":
                continue
            if child.is_dir():
                shutil.copytree(child, target / child.name, dirs_exist_ok=True)
            elif child.is_file():
                shutil.copy2(child, target / child.name)
        return True

    md_files = sorted(p for p in src.glob("*.md") if p.is_file())
    if md_files:
        for md in md_files:
            target = dest_skills_dir / md.stem.replace("_", "-")
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(md, target / "SKILL.md")
        return True
    return False


def _inject_apt_mirror(
    dockerfile: Path,
    apt_mirror: str | None,
    apt_security_mirror: str | None,
    ubuntu_apt_mirror: str | None,
    ubuntu_apt_security_mirror: str | None,
    ubuntu_ports_apt_mirror: str | None,
    maven_mirror: str | None,
    pip_index_url: str | None,
    pip_extra_index_url: str | None,
    uv_index_url: str | None,
    uv_extra_index_url: str | None,
) -> None:
    if (
        not apt_mirror
        and not apt_security_mirror
        and not ubuntu_apt_mirror
        and not ubuntu_apt_security_mirror
        and not ubuntu_ports_apt_mirror
        and not maven_mirror
        and not pip_index_url
        and not pip_extra_index_url
        and not uv_index_url
        and not uv_extra_index_url
    ):
        return

    env_lines: list[str] = []
    if pip_index_url:
        env_lines.append(f"ENV PIP_INDEX_URL={pip_index_url}")
        env_lines.append(f"ENV UV_INDEX_URL={uv_index_url or pip_index_url}")
        env_lines.append(f"ENV UV_DEFAULT_INDEX={uv_index_url or pip_index_url}")
    elif uv_index_url:
        env_lines.append(f"ENV UV_INDEX_URL={uv_index_url}")
        env_lines.append(f"ENV UV_DEFAULT_INDEX={uv_index_url}")
    if pip_extra_index_url:
        env_lines.append(f"ENV PIP_EXTRA_INDEX_URL={pip_extra_index_url}")
    if uv_extra_index_url:
        env_lines.append(f"ENV UV_EXTRA_INDEX_URL={uv_extra_index_url}")

    debian_mirror = apt_mirror or "http://deb.debian.org/debian"
    security_mirror = apt_security_mirror or apt_mirror or "http://deb.debian.org/debian-security"
    ubuntu_mirror = ubuntu_apt_mirror or "http://archive.ubuntu.com/ubuntu"
    ubuntu_ports_mirror = (
        ubuntu_ports_apt_mirror or ubuntu_apt_mirror or "http://ports.ubuntu.com/ubuntu-ports"
    )
    ubuntu_security_mirror = ubuntu_apt_security_mirror or ubuntu_apt_mirror or ubuntu_mirror
    sed_expr = (
        f"s#http://deb.debian.org/debian#{shlex.quote(debian_mirror)}#g; "
        f"s#http://deb.debian.org/debian-security#{shlex.quote(security_mirror)}#g; "
        f"s#http://ports.ubuntu.com/ubuntu-ports#{shlex.quote(ubuntu_ports_mirror)}#g; "
        f"s#http://archive.ubuntu.com/ubuntu#{shlex.quote(ubuntu_mirror)}#g; "
        f"s#http://security.ubuntu.com/ubuntu#{shlex.quote(ubuntu_security_mirror)}#g"
    )
    maven_script = ""
    if maven_mirror:
        maven_settings_lines = [
            '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 '
            'https://maven.apache.org/xsd/settings-1.0.0.xsd">',
            "  <mirrors>",
            "    <mirror>",
            "      <id>skilllearnbench-mirror</id>",
            "      <mirrorOf>external:*</mirrorOf>",
            f"      <url>{maven_mirror}</url>",
            "    </mirror>",
            "  </mirrors>",
            "</settings>",
        ]
        quoted_lines = " ".join(shlex.quote(line) for line in maven_settings_lines)
        maven_script = (
            "    mkdir -p /root/.m2; \\\n"
            f"    printf '%s\\n' {quoted_lines} > /root/.m2/settings.xml; \\\n"
        )
    pip_script = ""
    if pip_index_url or pip_extra_index_url:
        pip_config_lines = ["[global]"]
        if pip_index_url:
            pip_config_lines.append(f"index-url = {pip_index_url}")
        if pip_extra_index_url:
            pip_config_lines.append(f"extra-index-url = {pip_extra_index_url}")
        quoted_lines = " ".join(shlex.quote(line) for line in pip_config_lines)
        pip_script = (
            f"    mkdir -p /etc; \\\n    printf '%s\\n' {quoted_lines} > /etc/pip.conf; \\\n"
        )
    mirror_script = (
        ("\n".join(env_lines) + "\n" if env_lines else "") + "RUN set -eux; \\\n"
        "    for apt_sources in /etc/apt/sources.list.d/*.sources /etc/apt/sources.list; do \\\n"
        '        [ -f "$apt_sources" ] || continue; \\\n'
        f"        sed -i '{sed_expr}' \"$apt_sources\"; \\\n"
        "    done; \\\n"
        f"{maven_script}"
        f"{pip_script}"
        "    printf '%s\\n' 'Acquire::Retries \"5\";' 'Acquire::http::Timeout \"60\";' "
        "'Acquire::https::Timeout \"60\";' > /etc/apt/apt.conf.d/80-skilllearnbench-retries\n"
    )

    lines = dockerfile.read_text(encoding="utf-8", errors="replace").splitlines()
    insert_at = 1 if lines and lines[0].lstrip().upper().startswith("FROM ") else 0
    lines.insert(insert_at, mirror_script.rstrip())
    dockerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prepare_build_env(
    env_dir: Path,
    skill_source: Path | None,
    *,
    apt_mirror: str | None = None,
    apt_security_mirror: str | None = None,
    ubuntu_apt_mirror: str | None = None,
    ubuntu_apt_security_mirror: str | None = None,
    ubuntu_ports_apt_mirror: str | None = None,
    maven_mirror: str | None = None,
    pip_index_url: str | None = None,
    pip_extra_index_url: str | None = None,
    uv_index_url: str | None = None,
    uv_extra_index_url: str | None = None,
) -> Path:
    tmp_root = Path(tempfile.mkdtemp(prefix="ov_slb_build_"))
    build_env = tmp_root / "environment"
    shutil.copytree(env_dir, build_env, ignore=shutil.ignore_patterns("skills"))
    _inject_apt_mirror(
        build_env / "Dockerfile",
        apt_mirror,
        apt_security_mirror,
        ubuntu_apt_mirror,
        ubuntu_apt_security_mirror,
        ubuntu_ports_apt_mirror,
        maven_mirror,
        pip_index_url,
        pip_extra_index_url,
        uv_index_url,
        uv_extra_index_url,
    )
    target_skills = build_env / "skills"
    target_skills.mkdir(parents=True, exist_ok=True)
    if skill_source is not None:
        ok = _copy_skills_to_dest(skill_source, target_skills)
        if not ok:
            raise RuntimeError(f"skill source has no usable SKILL.md content: {skill_source}")
    return build_env


def _run_preflight(
    *,
    plan: dict[str, Any],
    config_path: Path,
    required_env: list[str],
    require_task_environment: bool = True,
) -> tuple[bool, dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    def add_check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            errors.append(f"{name}: {detail}")

    if require_task_environment:
        docker_bin = shutil.which("docker")
        add_check("docker_cli", docker_bin is not None, docker_bin or "docker command not found")
        if docker_bin:
            ok, detail = _run_optional(["docker", "--version"], timeout=30)
            add_check("docker_version", ok, detail)
            ok, detail = _run_optional(
                ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30
            )
            add_check("docker_daemon", ok, detail or "docker daemon is not reachable")
    else:
        checks.append(
            {
                "name": "docker_cli",
                "ok": True,
                "detail": "skipped for --commit-existing-trajectory-memory",
            }
        )

    runtime_ok, runtime_detail = _check_vikingbot_runtime(config_path)
    add_check("vikingbot_runtime", runtime_ok, runtime_detail)

    add_check("config_file", config_path.exists(), str(config_path))
    config_ok, config_detail = _check_openviking_llm_config(config_path)
    add_check("config_llm", config_ok, config_detail)
    if plan.get("commit_trajectory_memory") or plan.get("inject_search_resolution"):
        memory_config_ok, memory_config_detail = _check_openviking_memory_user_key_config(
            config_path
        )
        add_check("config_openviking_user_key", memory_config_ok, memory_config_detail)

    missing_required_env: list[str] = []
    inferred_required_env: list[str] = []
    if require_task_environment:
        for name in required_env:
            value, source = _resolve_required_env_value(name)
            if value:
                if source != "environment":
                    inferred_required_env.append(f"${name} from {source}")
            else:
                missing_required_env.append(name)
        if missing_required_env:
            warnings.append(
                "Task-required env vars are missing: "
                + ", ".join(f"${name}" for name in missing_required_env)
            )
        if inferred_required_env:
            warnings.append(
                "Task-required env vars will be inferred: " + ", ".join(inferred_required_env)
            )

    payload = {
        **plan,
        "config_path": str(config_path),
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "ok": not errors,
    }
    return not errors, payload


async def _docker_run(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(_run, cmd, input_text=input_text, timeout=timeout)


def _raw_tool_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    raw = kwargs.get("raw")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_tool_param(
    kwargs: dict[str, Any],
    name: str,
    value: Any | None,
) -> str | None:
    if value is None:
        value = _raw_tool_params(kwargs).get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class DockerExecTool(Tool):
    def __init__(self, container: str, workdir: str, timeout: int):
        self.container = container
        self.workdir = workdir
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command inside the SkillLearnBench task container. "
            "Use this for inspecting files, running scripts, installing task-local "
            "helpers, and producing the required output artifacts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run in the task container.",
                },
                "working_dir": {
                    "type": "string",
                    "description": f"Optional working directory inside the container. Defaults to {self.workdir}.",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        command: str | None = None,
        working_dir: str | None = None,
        **kwargs: Any,
    ) -> str:
        command = _text_tool_param(kwargs, "command", command)
        working_dir = _text_tool_param(kwargs, "working_dir", working_dir)
        if command is None:
            return "Error: missing required parameter: command"
        cwd = working_dir or self.workdir
        timed_command = f"timeout -k 5s {self.timeout}s sh -lc {shlex.quote(command)}"
        proc = await _docker_run(
            ["docker", "exec", "-w", cwd, self.container, "sh", "-lc", timed_command],
            timeout=self.timeout + 10,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        text = out
        if err.strip():
            text += ("\nSTDERR:\n" if text else "STDERR:\n") + err
        if proc.returncode:
            text += f"\nExit code: {proc.returncode}"
        return text[:TEXT_FILE_LIMIT] + (
            f"\n... (truncated, {len(text) - TEXT_FILE_LIMIT} more chars)"
            if len(text) > TEXT_FILE_LIMIT
            else ""
        )


class DockerReadFileTool(Tool):
    def __init__(self, container: str):
        self.container = container

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read a text file from inside the SkillLearnBench task container."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or container-relative file path.",
                }
            },
            "required": ["path"],
        }

    async def execute(
        self, tool_context: ToolContext, path: str | None = None, **kwargs: Any
    ) -> str:
        path = _text_tool_param(kwargs, "path", path)
        if path is None:
            return "Error: missing required parameter: path"
        script = (
            "from pathlib import Path; import sys\n"
            "p=Path(sys.argv[1])\n"
            "print(p.read_text(encoding='utf-8', errors='replace'), end='')\n"
        )
        proc = await _docker_run(["docker", "exec", self.container, "python3", "-c", script, path])
        if proc.returncode:
            return (proc.stderr or proc.stdout or f"Error reading {path}").strip()
        text = proc.stdout or ""
        return text[:TEXT_FILE_LIMIT] + (
            f"\n... (truncated, {len(text) - TEXT_FILE_LIMIT} more chars)"
            if len(text) > TEXT_FILE_LIMIT
            else ""
        )


class DockerWriteFileTool(Tool):
    def __init__(self, container: str):
        self.container = container

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write a text file inside the SkillLearnBench task container, creating parent directories."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or container-relative file path.",
                },
                "content": {"type": "string", "description": "Text content to write."},
            },
            "required": ["path", "content"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        path: str | None = None,
        content: str | None = None,
        **kwargs: Any,
    ) -> str:
        path = _text_tool_param(kwargs, "path", path)
        content = _text_tool_param(kwargs, "content", content)
        if path is None:
            return "Error: missing required parameter: path"
        if content is None:
            return "Error: missing required parameter: content"
        script = (
            "from pathlib import Path; import sys\n"
            "p=Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text(sys.stdin.read(), encoding='utf-8')\n"
        )
        proc = await _docker_run(
            ["docker", "exec", "-i", self.container, "python3", "-c", script, path],
            input_text=content,
        )
        if proc.returncode:
            return (proc.stderr or proc.stdout or f"Error writing {path}").strip()
        return f"Successfully wrote {len(content)} bytes to {path}"


class DockerEditFileTool(Tool):
    def __init__(self, container: str):
        self.reader = DockerReadFileTool(container)
        self.writer = DockerWriteFileTool(container)

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a container file by replacing an exact text span."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        path: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        **kwargs: Any,
    ) -> str:
        path = _text_tool_param(kwargs, "path", path)
        old_text = _text_tool_param(kwargs, "old_text", old_text)
        new_text = _text_tool_param(kwargs, "new_text", new_text)
        if path is None:
            return "Error: missing required parameter: path"
        if old_text is None:
            return "Error: missing required parameter: old_text"
        if new_text is None:
            return "Error: missing required parameter: new_text"
        content = await self.reader.execute(tool_context, path)
        if content.startswith("Error") or content.startswith("STDERR"):
            return content
        if old_text not in content:
            return "Error: old_text not found in file."
        if content.count(old_text) > 1:
            return "Error: old_text appears multiple times; provide a more specific span."
        return await self.writer.execute(tool_context, path, content.replace(old_text, new_text, 1))


class DockerListDirTool(Tool):
    def __init__(self, container: str):
        self.container = container

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List a directory inside the SkillLearnBench task container."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

    async def execute(
        self, tool_context: ToolContext, path: str | None = None, **kwargs: Any
    ) -> str:
        path = _text_tool_param(kwargs, "path", path)
        if path is None:
            return "Error: missing required parameter: path"
        proc = await _docker_run(["docker", "exec", self.container, "ls", "-la", path])
        text = (proc.stdout or "") + (("\nSTDERR:\n" + proc.stderr) if proc.stderr.strip() else "")
        if proc.returncode:
            text += f"\nExit code: {proc.returncode}"
        return text.strip() or "(empty)"


class DockerSandboxShim:
    """Minimal sandbox manager interface for AgentLoop hooks/context."""

    def __init__(self, workspace: Path, container_workdir: str):
        self.workspace = workspace
        self.container_workdir = container_workdir

    def to_workspace_id(self, session_key: Any) -> str:
        return "skilllearnbench"

    def get_workspace_path(self, session_key: Any) -> Path:
        return self.workspace

    async def get_sandbox_cwd(self, session_key: Any) -> str:
        return self.container_workdir

    async def get_sandbox(self, session_key: Any) -> Any:
        raise RuntimeError(
            "SkillLearnBench runner uses Docker-backed tools, not VikingBot sandbox backends."
        )

    async def cleanup_session(self, session_key: Any) -> None:
        return None

    async def cleanup_all(self) -> None:
        return None


def _build_agent(
    config_path: Path,
    workspace: Path,
    max_iterations: int,
    container_workdir: str,
) -> AgentLoop:
    os.environ["OPENVIKING_CONFIG_FILE"] = str(config_path)
    _require_vikingbot_runtime()
    config = ensure_config(config_path)
    _init_bot_data(config)
    # Keep benchmark trials isolated: OpenViking hooks can inject memory/experience
    # outside the explicit Docker-backed tool set.
    config.hooks = []
    bus = MessageBus()
    session_manager = SessionManager(config.bot_data_path)
    provider = _make_provider(config)
    sandbox_manager = DockerSandboxShim(workspace, container_workdir)
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model=config.agents.model,
        max_iterations=max_iterations,
        memory_window=config.agents.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        exa_api_key=None,
        gen_image_model=config.agents.gen_image_model,
        exec_config=config.tools.exec,
        cron_service=None,
        session_manager=session_manager,
        sandbox_manager=sandbox_manager,
        config=config,
        eval=True,
        mcp_servers=None,
    )


async def _run_vikingbot(
    *,
    config_path: Path,
    workspace: Path,
    instruction: str,
    container_name: str,
    container_workdir: str,
    max_iterations: int,
    tool_timeout: int,
    trajectory_path: Path,
    hidden_verifier: bool = False,
    search_resolution_context: str | None = None,
) -> dict[str, Any]:
    agent = _build_agent(
        config_path,
        workspace,
        max_iterations=max_iterations,
        container_workdir=container_workdir,
    )
    for tool_name in list(agent.tools.tool_names):
        agent.tools.unregister(tool_name)
    docker_tools = [
        DockerExecTool(container_name, container_workdir, timeout=tool_timeout),
        DockerReadFileTool(container_name),
        DockerWriteFileTool(container_name),
        DockerEditFileTool(container_name),
        DockerListDirTool(container_name),
    ]
    for tool in docker_tools:
        agent.tools.register(tool)
    allowed_tool_names = ", ".join(tool.name for tool in docker_tools)

    if hidden_verifier:
        verifier_note = """The verifier tests are hidden and unavailable during solving.
Do not try to inspect, create, or run `/tests` or verifier files. Complete the
task from the user instruction and files available in the container. After you
finish, the benchmark harness will run the hidden verifier."""
    else:
        verifier_note = """The verifier tests are mounted read-only at `/tests`; after you finish,
`bash /tests/test.sh` will be run in the same container. Inspect `/tests` when
useful and satisfy exact output paths, schemas, names, and values asserted there.
Before finishing, run `bash /tests/test.sh` when practical and fix any failures."""

    system_note = f"""You are solving a SkillLearnBench task.

All file and shell tools operate inside Docker container `{container_name}`.
The default container working directory is `{container_workdir}`.
Only these tools are available: {allowed_tool_names}. Do not call web_search,
web_fetch, openviking_search, openviking_* tools, or any tool not in that list.
{verifier_note}
Use the task's requested absolute paths exactly. Create or modify files in the
container, then finish with a concise summary. Do not ask the user for more input.
"""
    if search_resolution_context and search_resolution_context.strip():
        system_note += (
            "\n\n"
            "<read_only_resolution_hints>\n"
            f"{search_resolution_context.strip()}\n"
            "</read_only_resolution_hints>\n"
            "\nThe read-only resolution hints above are not tools. Do not call "
            "OpenViking or memory tools; only use the allowed Docker tools listed "
            "earlier in this system message.\n"
        )
    session_key = SessionKey(
        type="cli", channel_id="skilllearnbench", chat_id=_safe_name(container_name)
    )
    messages = await agent.context.build_messages(
        history=[],
        current_message=instruction,
        session_key=session_key,
        ov_tools_enable=False,
        media=None,
        profile_user_list=[],
        memory_peer_ids=None,
        memory_owner_user_ids=None,
    )
    messages = _strip_vikingbot_default_tool_profile(messages)
    messages.insert(0, {"role": "system", "content": system_note})
    final_content, reasoning, tools_used, token_usage, iteration = await agent._run_agent_loop(
        messages=messages,
        session_key=session_key,
        publish_events=False,
        sender_id="skilllearnbench",
        ov_tools_enable=False,
    )
    payload = {
        "final_content": final_content,
        "reasoning_content": reasoning,
        "tools_used": tools_used,
        "token_usage": token_usage,
        "iteration": iteration,
        "messages": messages,
    }
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    trajectory_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    await agent.close_mcp()
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one SkillLearnBench task with VikingBot.")
    parser.add_argument("--skilllearnbench-root", help="Path to cxcscmu/SkillLearnBench checkout.")
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID, help="Task id like task/task-1.")
    parser.add_argument("--skill-source", type=Path, help="Optional task-level skill directory.")
    parser.add_argument("--output-root", type=Path, default=BENCH_DIR / "result")
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to ov.conf. If omitted, use OPENVIKING_CONFIG_FILE or ~/.openviking/ov.conf.",
    )
    parser.add_argument("--trial-id", default=None)
    parser.add_argument(
        "--skill-config",
        default=None,
        help="Output config name; defaults to skill dir name or no_skill.",
    )
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--tool-timeout", type=int, default=300)
    parser.add_argument("--docker-build-timeout", type=int, default=1800)
    parser.add_argument(
        "--apt-mirror", help="Optional Debian apt mirror injected before task Dockerfile installs."
    )
    parser.add_argument(
        "--apt-security-mirror",
        help="Optional Debian security apt mirror. Defaults to --apt-mirror when omitted.",
    )
    parser.add_argument(
        "--ubuntu-apt-mirror",
        help="Optional Ubuntu apt mirror injected before task Dockerfile installs.",
    )
    parser.add_argument(
        "--ubuntu-apt-security-mirror",
        help="Optional Ubuntu security apt mirror. Defaults to --ubuntu-apt-mirror when omitted.",
    )
    parser.add_argument(
        "--ubuntu-ports-apt-mirror",
        help=(
            "Optional Ubuntu ports mirror for arm64 sources. Defaults to "
            "--ubuntu-apt-mirror when omitted."
        ),
    )
    parser.add_argument(
        "--maven-mirror", help="Optional Maven mirror URL written to /root/.m2/settings.xml."
    )
    parser.add_argument(
        "--pip-index-url", help="Optional pip index URL written to /etc/pip.conf and PIP_INDEX_URL."
    )
    parser.add_argument(
        "--pip-extra-index-url", help="Optional pip extra index URL written to /etc/pip.conf."
    )
    parser.add_argument(
        "--uv-index-url", help="Optional uv index URL exported as UV_INDEX_URL/UV_DEFAULT_INDEX."
    )
    parser.add_argument(
        "--uv-extra-index-url", help="Optional uv extra index URL exported as UV_EXTRA_INDEX_URL."
    )
    parser.add_argument("--verifier-timeout", type=int, default=1800)
    parser.add_argument(
        "--hidden-verifier",
        action="store_true",
        help="Do not expose /tests to the agent; copy tests in only for verifier execution.",
    )
    parser.add_argument(
        "--inject-search-resolution",
        action="store_true",
        help=(
            "Call OpenViking /api/v1/search/resolution before the agent runs "
            "and inject the returned Query Resolution Pack into the system context."
        ),
    )
    parser.add_argument(
        "--resolution-agent-space",
        default="default",
        help="agent_space passed to OpenViking search/resolution.",
    )
    parser.add_argument(
        "--resolution-include-debug",
        action="store_true",
        help="Save search/resolution debug payload, including retrieval queries and raw candidates.",
    )
    parser.add_argument("--resolution-user-memory-limit", type=int, default=8)
    parser.add_argument("--resolution-experiences-limit", type=int, default=5)
    parser.add_argument("--resolution-tools-memory-limit", type=int, default=5)
    parser.add_argument("--resolution-skills-limit", type=int, default=5)
    parser.add_argument("--resolution-skills-memory-limit", type=int, default=5)
    parser.add_argument("--resolution-trajectory-grounding-limit", type=int, default=2)
    parser.add_argument("--resolution-pack-max-tokens", type=int, default=6000)
    parser.add_argument(
        "--resolution-skill-content-mode",
        choices=["auto", "full", "summary", "link_only"],
        default="auto",
    )
    parser.add_argument(
        "--no-resolution-trajectory-grounding",
        dest="resolution_allow_trajectory_grounding",
        action="store_false",
        help="Disable trajectory grounding inside search/resolution.",
    )
    parser.set_defaults(resolution_allow_trajectory_grounding=True)
    parser.add_argument(
        "--allow-resolution-failure",
        action="store_true",
        help="Continue with no injected context if search/resolution fails.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate paths and print planned actions."
    )
    parser.add_argument(
        "--preflight", action="store_true", help="Check Docker, VikingBot runtime, and env vars."
    )
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--remove-image", action="store_true")
    parser.add_argument(
        "--commit-existing-trajectory-memory",
        action="store_true",
        help="Import an existing trial's agent/vikingbot-trajectory.json and exit without running Docker.",
    )
    parser.add_argument(
        "--commit-trajectory-memory",
        action="store_true",
        help=(
            "Import every generated agent/vikingbot-trajectory.json as an "
            "OpenViking session with self + task instance peer memory enabled."
        ),
    )
    parser.add_argument(
        "--commit-trajectory-memory-on-fail",
        action="store_true",
        help="Deprecated compatibility flag. Trajectories are committed on fail by default when --commit-trajectory-memory is enabled.",
    )
    parser.add_argument(
        "--memory-session-id",
        help="Optional OpenViking session id for trajectory memory import. Defaults to a task/trial-derived id.",
    )
    parser.add_argument(
        "--wait-memory-task",
        action="store_true",
        help="Wait for the OpenViking post-commit memory extraction task to finish.",
    )
    parser.add_argument(
        "--memory-task-timeout",
        type=int,
        default=1800,
        help="Timeout in seconds when --wait-memory-task is enabled.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    slb_root = _resolve_skilllearnbench_root(args.skilllearnbench_root)
    task_path = slb_root / "tasks" / args.task_id
    env_dir = task_path / "environment"
    dockerfile = env_dir / "Dockerfile"
    instruction_path = task_path / "instruction.md"
    tests_dir = task_path / "tests"
    if not task_path.exists():
        raise FileNotFoundError(f"task not found: {task_path}")
    if not dockerfile.exists():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")
    if not instruction_path.exists():
        raise FileNotFoundError(f"instruction.md not found: {instruction_path}")
    if not tests_dir.exists():
        raise FileNotFoundError(f"tests dir not found: {tests_dir}")

    skill_source = args.skill_source.resolve() if args.skill_source else None
    if skill_source is not None and not skill_source.exists():
        raise FileNotFoundError(f"skill source not found: {skill_source}")

    skill_config = args.skill_config or (
        skill_source.name
        if skill_source
        else "no_skill_resolution"
        if args.inject_search_resolution
        else "no_skill"
    )
    trial_id = args.trial_id or _timestamp_id()
    task_name, subtask_name = Path(args.task_id).parts[-2:]
    trial_path = args.output_root.resolve() / skill_config / task_name / subtask_name / trial_id
    agent_dir = trial_path / "agent"
    verifier_dir = trial_path / "verifier"
    host_workspace = trial_path / "vikingbot_workspace"
    container_workdir = _parse_workdir(dockerfile)
    image_tag = f"ov-skilllearnbench-{_safe_name(args.task_id)}:{trial_id.lower()}"
    container_name = f"ov_slb_{_safe_name(args.task_id)}_{_safe_name(trial_id)}"
    required_env = _extract_toml_list(_load_task_toml_text(task_path), "required_env")

    plan = {
        "skilllearnbench_root": str(slb_root),
        "task_id": args.task_id,
        "task_path": str(task_path),
        "skill_source": str(skill_source) if skill_source else None,
        "skill_config": skill_config,
        "trial_path": str(trial_path),
        "image_tag": image_tag,
        "container_name": container_name,
        "container_workdir": container_workdir,
        "required_env": required_env,
        "hidden_verifier": args.hidden_verifier,
        "inject_search_resolution": args.inject_search_resolution,
        "resolution_agent_space": args.resolution_agent_space,
        "commit_trajectory_memory": args.commit_trajectory_memory,
        "commit_existing_trajectory_memory": args.commit_existing_trajectory_memory,
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0

    agent_dir.mkdir(parents=True, exist_ok=True)
    verifier_dir.mkdir(parents=True, exist_ok=True)
    host_workspace.mkdir(parents=True, exist_ok=True)
    (host_workspace / "skills").mkdir(parents=True, exist_ok=True)
    if skill_source is not None:
        _copy_skills_to_dest(skill_source, host_workspace / "skills")

    if args.config:
        config_path = args.config.expanduser().resolve()
    else:
        config_path = _default_openviking_config_path()

    preflight_ok, preflight_payload = _run_preflight(
        plan=plan,
        config_path=config_path,
        required_env=required_env,
        require_task_environment=not args.commit_existing_trajectory_memory,
    )
    (trial_path / "preflight.json").write_text(
        json.dumps(preflight_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.preflight:
        print(json.dumps(preflight_payload, ensure_ascii=False, indent=2))
        return 0 if preflight_ok else 2
    if not preflight_ok:
        print(json.dumps(preflight_payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    if args.commit_existing_trajectory_memory:
        if not args.commit_trajectory_memory:
            raise ValueError(
                "--commit-existing-trajectory-memory requires --commit-trajectory-memory"
            )
        trajectory_path = agent_dir / "vikingbot-trajectory.json"
        if not trajectory_path.exists():
            raise FileNotFoundError(f"existing trajectory not found: {trajectory_path}")
        memory_session_id = _trajectory_memory_session_id(
            skill_config=skill_config,
            task_id=args.task_id,
            trial_id=trial_id,
            explicit_session_id=args.memory_session_id,
        )
        print(
            f"[memory] Importing existing trajectory into OpenViking session {memory_session_id} ...",
            flush=True,
        )
        memory_commit = None
        memory_commit_error = None
        try:
            memory_commit = asyncio.run(
                _commit_trajectory_memory(
                    config_path=config_path,
                    trajectory_path=trajectory_path,
                    task_id=args.task_id,
                    trial_id=trial_id,
                    skill_config=skill_config,
                    session_id=memory_session_id,
                    wait_task=args.wait_memory_task,
                    task_timeout=args.memory_task_timeout,
                )
            )
            print(f"[memory] committed trajectory session={memory_session_id}", flush=True)
        except Exception as exc:
            memory_commit_error = repr(exc)
            (agent_dir / "openviking-memory-commit-error.txt").write_text(
                memory_commit_error,
                encoding="utf-8",
            )
            print(f"[warn] OpenViking memory commit failed: {memory_commit_error}", file=sys.stderr)

        result_path = trial_path / "result.json"
        result = _load_json_file(result_path)
        result.update(
            {
                **plan,
                "config_path": str(config_path),
                "memory_commit": memory_commit,
                "memory_commit_error": memory_commit_error,
            }
        )
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Trial: {trial_path}")
        return 0 if memory_commit_error is None else 2

    build_env = _prepare_build_env(
        env_dir,
        skill_source,
        apt_mirror=args.apt_mirror,
        apt_security_mirror=args.apt_security_mirror,
        ubuntu_apt_mirror=args.ubuntu_apt_mirror,
        ubuntu_apt_security_mirror=args.ubuntu_apt_security_mirror,
        ubuntu_ports_apt_mirror=args.ubuntu_ports_apt_mirror,
        maven_mirror=args.maven_mirror,
        pip_index_url=args.pip_index_url,
        pip_extra_index_url=args.pip_extra_index_url,
        uv_index_url=args.uv_index_url,
        uv_extra_index_url=args.uv_extra_index_url,
    )
    build_root = build_env.parent
    reward = 0
    passed = False
    agent_payload: dict[str, Any] | None = None
    agent_error: str | None = None
    search_resolution_payload: dict[str, Any] | None = None
    search_resolution_error: str | None = None
    search_resolution_context: str | None = None
    memory_commit: dict[str, Any] | None = None
    memory_commit_error: str | None = None
    verifier_stdout = ""
    verifier_stderr = ""
    try:
        print(f"[1/5] Building {image_tag} ...", flush=True)
        build = _run_streaming(
            ["docker", "build", "--progress=plain", "-t", image_tag, str(build_env)],
            stdout_path=trial_path / "docker-build.stdout.txt",
            stderr_path=trial_path / "docker-build.stderr.txt",
            timeout=args.docker_build_timeout,
        )
        if build.returncode:
            raise RuntimeError(f"docker build failed: {(build.stderr or build.stdout)[-2000:]}")

        print(f"[2/5] Starting {container_name} ...", flush=True)
        _run(["docker", "rm", "-f", container_name], timeout=60)
        env_args: list[str] = []
        run_env = os.environ.copy()
        for var in required_env:
            val, source = _resolve_required_env_value(var)
            if val:
                run_env[var] = val
                env_args.extend(["-e", var])
                if source != "environment":
                    print(f"[info] required env ${var} sourced from {source}.", flush=True)
            else:
                print(f"[warn] required env ${var} is not set; task may fail.", file=sys.stderr)
        run_cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
        ]
        if not args.hidden_verifier:
            run_cmd.extend(["-v", f"{tests_dir}:/tests:ro"])
        run_cmd.extend(
            [
                "-v",
                f"{trial_path}:/logs",
                *env_args,
                image_tag,
                "sleep",
                "3600",
            ]
        )
        started = _run(run_cmd, timeout=120, env=run_env)
        if started.returncode:
            raise RuntimeError(f"docker run failed: {started.stderr or started.stdout}")

        print("[3/5] Running VikingBot agent ...", flush=True)
        instruction = instruction_path.read_text(encoding="utf-8").strip()
        if args.inject_search_resolution:
            peer_id = _trajectory_memory_peer_id(args.task_id)
            print(
                f"[resolution] Calling OpenViking search/resolution for peer {peer_id} ...",
                flush=True,
            )
            try:
                search_resolution_payload = asyncio.run(
                    _fetch_search_resolution_context(
                        config_path=config_path,
                        task_id=args.task_id,
                        instruction=instruction,
                        peer_id=peer_id,
                        container_workdir=container_workdir,
                        hidden_verifier=args.hidden_verifier,
                        agent_space=args.resolution_agent_space,
                        include_debug=args.resolution_include_debug,
                        limits=_search_resolution_limits(args),
                        options=_search_resolution_options(args),
                    )
                )
                search_resolution_context = str(search_resolution_payload.get("context") or "")
                (agent_dir / "openviking-search-resolution.json").write_text(
                    json.dumps(search_resolution_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (agent_dir / "openviking-search-resolution.md").write_text(
                    search_resolution_context,
                    encoding="utf-8",
                )
                summary = _search_resolution_summary(search_resolution_payload) or {}
                print(
                    "[resolution] "
                    f"resolution_id={summary.get('resolution_id')} "
                    f"pack_chars={summary.get('pack_markdown_chars')}",
                    flush=True,
                )
            except Exception as exc:
                search_resolution_error = repr(exc)
                (agent_dir / "openviking-search-resolution-error.txt").write_text(
                    search_resolution_error,
                    encoding="utf-8",
                )
                print(
                    f"[warn] OpenViking search/resolution failed: {search_resolution_error}",
                    file=sys.stderr,
                )
                if not args.allow_resolution_failure:
                    agent_error = f"search_resolution_failed: {search_resolution_error}"

        try:
            if agent_error is None:
                agent_payload = asyncio.run(
                    _run_vikingbot(
                        config_path=config_path,
                        workspace=host_workspace,
                        instruction=instruction,
                        container_name=container_name,
                        container_workdir=container_workdir,
                        max_iterations=args.max_iterations,
                        tool_timeout=args.tool_timeout,
                        trajectory_path=agent_dir / "vikingbot-trajectory.json",
                        hidden_verifier=args.hidden_verifier,
                        search_resolution_context=search_resolution_context,
                    )
                )
                (agent_dir / "vikingbot-stdout.txt").write_text(
                    str(agent_payload.get("final_content") or ""), encoding="utf-8"
                )
        except Exception as exc:  # verifier still runs to mirror SkillLearnBench behavior
            agent_error = repr(exc)
            (agent_dir / "vikingbot-error.txt").write_text(agent_error, encoding="utf-8")
            print(f"[warn] VikingBot failed: {agent_error}", file=sys.stderr)

        print("[4/5] Running verifier ...", flush=True)
        if args.hidden_verifier:
            _run(["docker", "exec", container_name, "rm", "-rf", "/tests"], timeout=60)
            _run(["docker", "exec", container_name, "mkdir", "-p", "/tests"], timeout=60)
            copied = _run(
                ["docker", "cp", f"{tests_dir}/.", f"{container_name}:/tests/"], timeout=300
            )
            if copied.returncode:
                raise RuntimeError(
                    f"copy hidden verifier tests failed: {copied.stderr or copied.stdout}"
                )
        verifier = _run(
            ["docker", "exec", container_name, "bash", "/tests/test.sh"],
            timeout=args.verifier_timeout,
        )
        verifier_stdout = verifier.stdout or ""
        verifier_stderr = verifier.stderr or ""
        (verifier_dir / "stdout.txt").write_text(verifier_stdout, encoding="utf-8")
        (verifier_dir / "stderr.txt").write_text(verifier_stderr, encoding="utf-8")

        reward_file = verifier_dir / "reward.txt"
        if reward_file.exists():
            try:
                reward = int(reward_file.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                reward = 0
        passed = reward == 1

        should_commit_memory = bool(args.commit_trajectory_memory and agent_payload is not None)
        if should_commit_memory:
            memory_session_id = _trajectory_memory_session_id(
                skill_config=skill_config,
                task_id=args.task_id,
                trial_id=trial_id,
                explicit_session_id=args.memory_session_id,
            )
            print(
                f"[memory] Importing trajectory into OpenViking session {memory_session_id} ...",
                flush=True,
            )
            try:
                memory_commit = asyncio.run(
                    _commit_trajectory_memory(
                        config_path=config_path,
                        trajectory_path=agent_dir / "vikingbot-trajectory.json",
                        task_id=args.task_id,
                        trial_id=trial_id,
                        skill_config=skill_config,
                        session_id=memory_session_id,
                        wait_task=args.wait_memory_task,
                        task_timeout=args.memory_task_timeout,
                    )
                )
                print(
                    f"[memory] committed trajectory session={memory_session_id}",
                    flush=True,
                )
            except Exception as exc:
                memory_commit_error = repr(exc)
                (agent_dir / "openviking-memory-commit-error.txt").write_text(
                    memory_commit_error,
                    encoding="utf-8",
                )
                print(
                    f"[warn] OpenViking memory commit failed: {memory_commit_error}",
                    file=sys.stderr,
                )

        print("[5/5] Writing result ...", flush=True)
        result = {
            **plan,
            "config_path": str(config_path),
            "passed": passed,
            "reward": reward,
            "agent_error": agent_error,
            "agent_final_content": (agent_payload or {}).get("final_content"),
            "token_usage": (agent_payload or {}).get("token_usage", {}),
            "iteration": (agent_payload or {}).get("iteration"),
            "tools_used": (agent_payload or {}).get("tools_used", []),
            "search_resolution": _search_resolution_summary(search_resolution_payload),
            "search_resolution_error": search_resolution_error,
            "memory_commit": memory_commit,
            "memory_commit_error": memory_commit_error,
            "verifier_exit": verifier.returncode,
            "verifier_stdout_tail": verifier_stdout[-2000:],
            "verifier_stderr_tail": verifier_stderr[-2000:],
        }
        (trial_path / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("PASS" if passed else "FAIL")
        print(f"Trial: {trial_path}")
        if should_commit_memory and memory_commit_error:
            return 2
        return 0 if passed else 1
    finally:
        if not args.keep_container:
            _run(["docker", "rm", "-f", container_name], timeout=60)
        if args.remove_image:
            _run(["docker", "rmi", "-f", image_tag], timeout=120)
        shutil.rmtree(build_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
