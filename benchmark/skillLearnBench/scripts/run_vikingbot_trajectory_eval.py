#!/usr/bin/env python3
"""Export VikingBot SkillLearnBench trajectories and run official trajectory eval.

SkillLearnBench's trajectory runner expects:

  evaluation_log/<config>/<task>/<subtask>/<trial-id>/agent/trajectory.jsonl
  evaluation_log/<config>/<task>/<subtask>/<trial-id>/verifier/reward.txt

The VikingBot task runner writes a different layout under
benchmark/skillLearnBench/result/<config>/<task>/<subtask>/<trial-id>/.
This bridge converts the saved VikingBot trajectory into a Claude-Code-like
JSONL file that SkillLearnBench's trajectory_io.py can read, then optionally
invokes the upstream trajectory evaluator.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BENCH_DIR = SCRIPT_DIR.parent
REPO_ROOT = BENCH_DIR.parents[1]

DEFAULT_SKILLLEARNBENCH_ROOT = Path("/Users/bytedance/work/space/test/SkillLearnBench")
DEFAULT_OUTPUT_ROOT = BENCH_DIR / "result"
DEFAULT_TRAJECTORY_ROOT = BENCH_DIR / "result" / "trajectory_eval"


def _default_openviking_config_path() -> Path:
    raw = os.environ.get("OPENVIKING_CONFIG_FILE")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".openviking" / "ov.conf").resolve()


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _is_loop_reflection(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = str(message.get("content") or "").strip()
    return content == "Reflect on the results and decide next steps."


def _is_runtime_wrapper(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = str(message.get("content") or "")
    return content.startswith("## Current Time:") and "User's query:" in content


def _compact_text(value: Any, limit: int = 120_000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n\n[... truncated {len(text) - limit} chars ...]\n\n{text[-tail:]}"


def _message_id(prefix: str, index: int) -> str:
    return f"vikingbot-{prefix}-{index}"


def _assistant_jsonl_message(index: int, message: dict[str, Any]) -> dict[str, Any] | None:
    content_items: list[dict[str, Any]] = []
    text = str(message.get("content") or "").strip()
    if text:
        content_items.append({"type": "text", "text": _compact_text(text)})

    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        fn = tool_call.get("function") or {}
        name = fn.get("name") or tool_call.get("name")
        if not name:
            continue
        content_items.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id") or _message_id("tool", index),
                "name": str(name),
                "input": _safe_json_loads(fn.get("arguments") or tool_call.get("arguments") or {}),
            }
        )

    if not content_items:
        return None
    return {
        "type": "assistant",
        "message": {
            "id": _message_id("assistant", index),
            "role": "assistant",
            "content": content_items,
        },
    }


def _user_jsonl_message(index: int, message: dict[str, Any]) -> dict[str, Any] | None:
    if _is_loop_reflection(message) or _is_runtime_wrapper(message):
        return None
    content = str(message.get("content") or "").strip()
    if not content:
        return None
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": _compact_text(content)}],
        },
    }


def _tool_jsonl_message(message: dict[str, Any]) -> dict[str, Any] | None:
    content = message.get("content")
    if content is None:
        return None
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": _compact_text(content),
                }
            ],
        },
    }


def vikingbot_trajectory_to_jsonl(payload: dict[str, Any]) -> str:
    """Convert a saved VikingBot trajectory payload to Claude-Code-like JSONL."""
    lines: list[dict[str, Any]] = []
    messages = payload.get("messages") or []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        converted: dict[str, Any] | None = None
        if role == "assistant":
            converted = _assistant_jsonl_message(index, message)
        elif role == "tool":
            converted = _tool_jsonl_message(message)
        elif role == "user":
            converted = _user_jsonl_message(index, message)
        if converted is not None:
            lines.append(converted)

    final_content = str(payload.get("final_content") or "").strip()
    if final_content:
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "id": _message_id("final", len(lines) + 1),
                    "role": "assistant",
                    "content": [{"type": "text", "text": _compact_text(final_content)}],
                },
            }
        )

    token_usage = payload.get("token_usage") or {}
    input_tokens = int(token_usage.get("prompt_tokens") or 0)
    output_tokens = int(token_usage.get("completion_tokens") or 0)
    if not input_tokens and token_usage.get("total_tokens"):
        input_tokens = int(token_usage.get("total_tokens") or 0)
    lines.append(
        {
            "type": "result",
            "modelUsage": {
                "vikingbot": {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                }
            },
        }
    )

    return "\n".join(json.dumps(item, ensure_ascii=False) for item in lines) + "\n"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _summary_rows(summary_csv: Path) -> list[dict[str, str]]:
    with summary_csv.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _matches_any(task_id: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    task_name, subtask_name = Path(task_id).parts[-2:]
    candidates = {task_id, task_name, subtask_name}
    return any(pattern in candidates for pattern in patterns)


def _reward_from_result(result: dict[str, Any], summary_row: dict[str, str]) -> int:
    if result.get("reward") in (0, 1):
        return int(result["reward"])
    if summary_row.get("reward") in {"0", "1"}:
        return int(summary_row["reward"])
    return 1 if result.get("passed") is True or summary_row.get("passed") == "True" else 0


def export_evaluation_log(args: argparse.Namespace) -> dict[str, Any]:
    summary_csv = args.summary_csv or (args.output_root / "suites" / args.suite_id / "summary.csv")
    rows = _summary_rows(summary_csv)
    trials_root = args.trajectory_root / "evaluation_log"
    exported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in rows:
        task_id = row["task_id"]
        if not _matches_any(task_id, args.task or []):
            continue
        task_name, subtask_name = Path(task_id).parts[-2:]
        result_path = Path(row["result_path"])
        trial_path = Path(row["trial_path"])
        source_trajectory = trial_path / "agent" / "vikingbot-trajectory.json"
        if not source_trajectory.exists():
            skipped.append({"task_id": task_id, "reason": "missing vikingbot trajectory"})
            continue

        trial_id = args.eval_trial_id or trial_path.name
        dest_trial = trials_root / args.skill_config / task_name / subtask_name / trial_id
        agent_dir = dest_trial / "agent"
        verifier_dir = dest_trial / "verifier"
        agent_dir.mkdir(parents=True, exist_ok=True)
        verifier_dir.mkdir(parents=True, exist_ok=True)

        source_payload = _load_json(source_trajectory)
        (agent_dir / "trajectory.jsonl").write_text(
            vikingbot_trajectory_to_jsonl(source_payload),
            encoding="utf-8",
        )

        result = _load_json(result_path)
        reward_text = str(_reward_from_result(result, row))
        source_reward = trial_path / "verifier" / "reward.txt"
        if source_reward.exists():
            reward_text = (
                source_reward.read_text(encoding="utf-8", errors="replace").strip() or reward_text
            )
        (verifier_dir / "reward.txt").write_text(reward_text + "\n", encoding="utf-8")
        (dest_trial / "metadata.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "source_trial_path": str(trial_path),
                    "source_result_path": str(result_path),
                    "source_trajectory_path": str(source_trajectory),
                    "source_returncode": row.get("returncode"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        exported.append(
            {
                "task_id": task_id,
                "trial_id": trial_id,
                "trajectory": str(agent_dir / "trajectory.jsonl"),
                "reward": int(reward_text) if reward_text in {"0", "1"} else reward_text,
            }
        )

    manifest = {
        "suite_id": args.suite_id,
        "skill_config": args.skill_config,
        "summary_csv": str(summary_csv),
        "trials_root": str(trials_root),
        "exported": exported,
        "skipped": skipped,
    }
    args.trajectory_root.mkdir(parents=True, exist_ok=True)
    (args.trajectory_root / "export_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _load_ov_config(config_path: Path) -> Any:
    for import_root in (REPO_ROOT, REPO_ROOT / "bot"):
        if str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))
    from vikingbot.config.loader import ensure_config

    return ensure_config(config_path)


def _model_for_openai_client(model: str) -> str:
    return re.sub(r"^(openai|volcengine|azure|openai-compatible)/", "", model)


def _build_eval_env(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    env = os.environ.copy()
    model = args.model
    config_path = args.config.resolve()
    if config_path.exists():
        config = _load_ov_config(config_path)
        agent_config = config.agents
        if not model:
            model = str(agent_config.model)
        provider = str(agent_config.provider or "").lower()
        api_key = str(agent_config.api_key or "")
        api_base = str(agent_config.api_base or "")
        if provider == "anthropic":
            if api_key:
                env["ANTHROPIC_API_KEY"] = api_key
        else:
            if api_key:
                env["OPENAI_API_KEY"] = api_key
            if api_base:
                env["OPENAI_BASE_URL"] = api_base

    if not model:
        model = "gpt-5-mini"
    return env, _model_for_openai_client(model)


def _parent_tasks(exported: list[dict[str, Any]]) -> list[str]:
    return sorted({Path(item["task_id"]).parts[-2] for item in exported})


_CSV_METRIC_KEYS = (
    "pass",
    "num_steps",
    "num_tool_calls",
    "num_skill_calls",
    "input_tokens",
    "output_tokens",
    "num_skills_invoked",
    "num_skills_total",
    "skill_invocation_ratio",
    "execution_order",
    "trajectory_key_point_recall",
    "completeness",
)

_KNOWN_LLMS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
]


def _parse_skill_config(config: str) -> tuple[str, str]:
    if config in ("human_authored", "no_skill"):
        return config, ""
    for llm in _KNOWN_LLMS:
        if config.endswith(llm):
            return llm, config[: len(config) - len(llm)].rstrip("-")
    return config, ""


def _extract_csv_metric(row: dict[str, Any], key: str) -> Any:
    if key == "pass":
        v = row.get("reward")
        return "" if v is None else int(v)
    if key == "execution_order":
        return row.get("execution_order", {}).get("score", "")
    if key == "skill_invocation_ratio":
        return row.get("skill_invocation", {}).get("skill_invocation_ratio", "")
    if key == "trajectory_key_point_recall":
        return row.get("key_points", {}).get("trajectory_key_point_recall", "")
    if key == "completeness":
        return row.get("completeness", {}).get("score", "")
    if key == "num_steps":
        return row.get("efficiency", {}).get("num_steps", "")
    if key == "num_tool_calls":
        return row.get("efficiency", {}).get("num_tools", "")
    if key == "num_skill_calls":
        return (
            row.get("skill_invocation", {}).get("counts", {}).get("skill_calls_among_available", "")
        )
    if key == "input_tokens":
        return row.get("efficiency", {}).get("input_tokens", "")
    if key == "output_tokens":
        return row.get("efficiency", {}).get("output_tokens", "")
    if key == "num_skills_invoked":
        return row.get("skill_invocation", {}).get("counts", {}).get("invoked_among_available", "")
    if key == "num_skills_total":
        return row.get("skill_invocation", {}).get("counts", {}).get("available", "")
    return ""


def _float_or_none(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _task_subtasks(trials_root: Path, config: str, task_name: str) -> list[str]:
    task_dir = trials_root / config / task_name
    if not task_dir.exists():
        return []
    return sorted(path.name for path in task_dir.iterdir() if path.is_dir())


def _safe_tag(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")


def _subtask_report_path(
    results_root: Path, config: str, task_name: str, subtask_name: str
) -> Path:
    return results_root / config / task_name / subtask_name / "trajectory_evaluation.json"


def write_task_csv_from_reports(results_root: Path, config: str, task_name: str) -> Path | None:
    task_dir = results_root / config / task_name
    if not task_dir.exists():
        return None

    rows: list[list[Any]] = []
    for report_path in sorted(task_dir.glob("*/trajectory_evaluation.json")):
        summary = _load_json(report_path)
        subtask_name = report_path.parent.name
        for row in summary.get("by_run", []):
            llm, method = _parse_skill_config(row.get("skill_config", config))
            rows.append(
                [
                    llm,
                    method,
                    task_name,
                    subtask_name,
                    row.get("trial_id", ""),
                    *[_extract_csv_metric(row, key) for key in _CSV_METRIC_KEYS],
                ]
            )

    if not rows:
        return None

    out_path = task_dir / f"{task_name}-trajectory-results.csv"
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["LLM", "method", "Task", "Query", "run_id", *_CSV_METRIC_KEYS])
        writer.writerows(rows)
    return out_path


def write_global_trajectory_summary(trajectory_root: Path, config: str) -> Path | None:
    reports_root = trajectory_root / "evaluation_reports" / config
    csv_paths = sorted(reports_root.glob("*/*-trajectory-results.csv"))
    if not csv_paths:
        return None

    rows: list[dict[str, str]] = []
    by_task: dict[str, list[dict[str, str]]] = {}
    for csv_path in csv_paths:
        with csv_path.open(encoding="utf-8", newline="") as f:
            task_rows = list(csv.DictReader(f))
        rows.extend(task_rows)
        if task_rows:
            by_task.setdefault(task_rows[0]["Task"], []).extend(task_rows)

    metrics: dict[str, dict[str, Any]] = {}
    for key in _CSV_METRIC_KEYS:
        vals = [v for v in (_float_or_none(row.get(key)) for row in rows) if v is not None]
        metrics[key] = {"count": len(vals), "mean": _mean(vals)}

    task_metrics: dict[str, dict[str, Any]] = {}
    for task_name, task_rows in sorted(by_task.items()):
        task_metrics[task_name] = {
            "rows": len(task_rows),
            "metrics": {
                key: {
                    "count": len(vals),
                    "mean": _mean(vals),
                }
                for key in _CSV_METRIC_KEYS
                for vals in [
                    [
                        v
                        for v in (_float_or_none(row.get(key)) for row in task_rows)
                        if v is not None
                    ]
                ]
            },
        }

    summary_path = trajectory_root / "trajectory_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "skill_config": config,
                "csv_count": len(csv_paths),
                "row_count": len(rows),
                "metrics": metrics,
                "by_task": task_metrics,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return summary_path


def _prepare_single_subtask_trials_root(
    source_trials_root: Path,
    temp_trials_root: Path,
    config: str,
    task_name: str,
    subtask_name: str,
) -> None:
    if temp_trials_root.exists():
        shutil.rmtree(temp_trials_root)
    dest_parent = temp_trials_root / config / task_name
    dest_parent.mkdir(parents=True, exist_ok=True)
    source_subtask = source_trials_root / config / task_name / subtask_name
    dest_subtask = dest_parent / subtask_name
    os.symlink(source_subtask, dest_subtask, target_is_directory=True)


def _run_official_for_subtask(
    args: argparse.Namespace,
    *,
    runner: Path,
    env: dict[str, str],
    model: str,
    trials_root: Path,
    results_root: Path,
    task_name: str,
    subtask_name: str,
) -> dict[str, Any]:
    temp_root = args.trajectory_root / ".tmp_trials" / _safe_tag(f"{task_name}__{subtask_name}")
    _prepare_single_subtask_trials_root(
        trials_root, temp_root, args.skill_config, task_name, subtask_name
    )
    cmd = [
        sys.executable,
        str(runner),
        "--task-id",
        task_name,
        "--config",
        args.skill_config,
        "--trials-root",
        str(temp_root),
        "--results-root",
        str(results_root),
        "--model",
        model,
    ]
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(args.skilllearnbench_root),
            text=True,
            capture_output=True,
            env=env,
            timeout=args.subtask_timeout,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        stderr += f"\n[TIMEOUT] subtask exceeded {args.subtask_timeout}s\n"
        returncode = 124
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    log_dir = args.trajectory_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    tag = _safe_tag(f"{task_name}__{subtask_name}")
    (log_dir / f"{tag}.stdout.txt").write_text(stdout, encoding="utf-8")
    (log_dir / f"{tag}.stderr.txt").write_text(stderr, encoding="utf-8")
    if stdout:
        print(stdout[-2500:], end="" if stdout.endswith("\n") else "\n", flush=True)
    if stderr:
        print(
            stderr[-2500:], end="" if stderr.endswith("\n") else "\n", file=sys.stderr, flush=True
        )
    return {
        "task": task_name,
        "subtask": subtask_name,
        "returncode": returncode,
        "timed_out": timed_out,
    }


def run_official_trajectory_eval(
    args: argparse.Namespace, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    env, model = _build_eval_env(args)
    runner = args.skilllearnbench_root / "evaluation" / "trajectory" / "run_trajectory_eval.py"
    trials_root = Path(manifest["trials_root"])
    results_root = args.trajectory_root / "evaluation_reports"
    tasks = args.parent_task or _parent_tasks(manifest["exported"])
    reports: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []

    for index, task_name in enumerate(tasks, start=1):
        subtasks = _task_subtasks(trials_root, args.skill_config, task_name)
        if not subtasks:
            print(f"[{index}/{len(tasks)}] trajectory eval {task_name} (no subtasks)", flush=True)
            reports.append({"task": task_name, "returncode": 1, "reason": "no subtasks"})
            if not args.keep_going:
                raise SystemExit(1)
            continue

        print(
            f"[{index}/{len(tasks)}] trajectory eval {task_name} ({len(subtasks)} subtask(s))",
            flush=True,
        )
        for sub_index, subtask_name in enumerate(subtasks, start=1):
            report_path = _subtask_report_path(
                results_root, args.skill_config, task_name, subtask_name
            )
            if args.skip_existing_reports and report_path.exists():
                print(f"  [{sub_index}/{len(subtasks)}] {subtask_name} (skip existing)", flush=True)
                reports.append(
                    {
                        "task": task_name,
                        "subtask": subtask_name,
                        "returncode": 0,
                        "skipped_existing": True,
                    }
                )
                continue

            jobs.append(
                {
                    "task_name": task_name,
                    "subtask_name": subtask_name,
                    "sub_index": sub_index,
                    "subtask_count": len(subtasks),
                }
            )

    if args.subtask_concurrency <= 1:
        for job in jobs:
            task_name = job["task_name"]
            subtask_name = job["subtask_name"]
            print(f"  [{job['sub_index']}/{job['subtask_count']}] {subtask_name}", flush=True)
            report = _run_official_for_subtask(
                args,
                runner=runner,
                env=env,
                model=model,
                trials_root=trials_root,
                results_root=results_root,
                task_name=task_name,
                subtask_name=subtask_name,
            )
            reports.append(report)
            if report["returncode"] != 0 and not args.keep_going:
                raise SystemExit(report["returncode"])
    else:
        max_workers = max(1, args.subtask_concurrency)
        print(f"Running {len(jobs)} subtask eval job(s) with concurrency={max_workers}", flush=True)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_job = {}
            for job in jobs:
                task_name = job["task_name"]
                subtask_name = job["subtask_name"]
                print(f"  [submit] {task_name}/{subtask_name}", flush=True)
                future = executor.submit(
                    _run_official_for_subtask,
                    args,
                    runner=runner,
                    env=env,
                    model=model,
                    trials_root=trials_root,
                    results_root=results_root,
                    task_name=task_name,
                    subtask_name=subtask_name,
                )
                future_to_job[future] = job

            for future in as_completed(future_to_job):
                job = future_to_job[future]
                try:
                    report = future.result()
                except Exception as exc:
                    report = {
                        "task": job["task_name"],
                        "subtask": job["subtask_name"],
                        "returncode": 1,
                        "exception": str(exc),
                    }
                    print(
                        f"  [failed] {job['task_name']}/{job['subtask_name']}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                reports.append(report)
                status = "ok" if report["returncode"] == 0 else f"rc={report['returncode']}"
                print(f"  [done] {job['task_name']}/{job['subtask_name']} ({status})", flush=True)
                if report["returncode"] != 0 and not args.keep_going:
                    raise SystemExit(report["returncode"])

    for task_name in tasks:
        csv_path = write_task_csv_from_reports(results_root, args.skill_config, task_name)
        if csv_path:
            print(f"  CSV refreshed -> {csv_path}", flush=True)

    (args.trajectory_root / "run_manifest.json").write_text(
        json.dumps({"model": model, "reports": reports}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    summary_path = write_global_trajectory_summary(args.trajectory_root, args.skill_config)
    if summary_path:
        print(f"Global summary -> {summary_path}", flush=True)
    return reports


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export and evaluate VikingBot SkillLearnBench trajectories."
    )
    parser.add_argument("--skilllearnbench-root", type=Path, default=DEFAULT_SKILLLEARNBENCH_ROOT)
    parser.add_argument("--suite-id", default="vikingbot-hidden-strict-20260617")
    parser.add_argument("--summary-csv", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--trajectory-root", type=Path, default=DEFAULT_TRAJECTORY_ROOT)
    parser.add_argument("--skill-config", default="no_skill")
    parser.add_argument("--eval-trial-id")
    parser.add_argument("--config", type=Path, default=_default_openviking_config_path())
    parser.add_argument(
        "--model", help="LLM judge model. Defaults to ov.conf agents.model, then gpt-5-mini."
    )
    parser.add_argument(
        "--task", action="append", help="Filter exported task/subtask. Can be repeated."
    )
    parser.add_argument(
        "--parent-task", action="append", help="Only run trajectory eval for these parent tasks."
    )
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument(
        "--keep-going", action="store_true", help="Continue if one parent task eval fails."
    )
    parser.add_argument(
        "--parent-timeout",
        type=int,
        default=900,
        help="Seconds before one parent task eval is timed out.",
    )
    parser.add_argument(
        "--subtask-timeout",
        type=int,
        default=300,
        help="Seconds before one subtask eval is timed out.",
    )
    parser.add_argument(
        "--subtask-concurrency",
        type=int,
        default=1,
        help="Number of subtask/instance trajectory eval jobs to run concurrently.",
    )
    parser.add_argument(
        "--skip-existing-reports",
        action="store_true",
        help="Skip subtasks with an existing trajectory_evaluation.json.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    args.skilllearnbench_root = args.skilllearnbench_root.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    args.trajectory_root = args.trajectory_root.expanduser().resolve()
    if args.summary_csv:
        args.summary_csv = args.summary_csv.expanduser().resolve()
    args.config = args.config.expanduser().resolve()

    manifest = export_evaluation_log(args)
    print(
        f"Exported {len(manifest['exported'])} trajectory run(s), "
        f"skipped {len(manifest['skipped'])}.",
        flush=True,
    )
    print(f"Trials root: {manifest['trials_root']}", flush=True)

    if args.export_only:
        return 0

    reports = run_official_trajectory_eval(args, manifest)
    failed = [item for item in reports if item["returncode"] != 0]
    print(
        f"Trajectory eval complete: {len(reports) - len(failed)}/{len(reports)} subtask run(s) succeeded."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
