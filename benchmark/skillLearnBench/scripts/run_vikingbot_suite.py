#!/usr/bin/env python3
"""Run a SkillLearnBench suite with the current Python/VikingBot runtime."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BENCH_DIR = SCRIPT_DIR.parent
TASK_RUNNER = SCRIPT_DIR / "run_vikingbot_task.py"
DEFAULT_SKILLLEARNBENCH_ROOT = Path("/Users/bytedance/work/space/test/SkillLearnBench")


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower() or "task"


def _timestamp_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d%H%M%S")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_load_error": repr(exc)}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _task_id(instance_dir: Path, tasks_root: Path) -> str:
    return str(instance_dir.relative_to(tasks_root))


def _discover_tasks(root: Path) -> list[str]:
    tasks_root = root / "tasks"
    task_ids: list[str] = []
    for instance_dir in sorted(tasks_root.glob("*/*")):
        if not instance_dir.is_dir():
            continue
        if not (instance_dir / "instruction.md").exists():
            continue
        if not (instance_dir / "environment" / "Dockerfile").exists():
            continue
        if not (instance_dir / "tests").is_dir():
            continue
        task_ids.append(_task_id(instance_dir, tasks_root))
    return task_ids


def _matches_any(task_id: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    task_name, instance_name = Path(task_id).parts[-2:]
    candidates = {task_id, task_name, instance_name}
    return any(pattern in candidates for pattern in patterns)


def _filter_tasks(task_ids: list[str], args: argparse.Namespace) -> list[str]:
    selected = [task_id for task_id in task_ids if _matches_any(task_id, args.task or [])]
    if args.exclude_task:
        selected = [task_id for task_id in selected if not _matches_any(task_id, args.exclude_task)]
    if args.start_after:
        if args.start_after in selected:
            selected = selected[selected.index(args.start_after) + 1 :]
        else:
            selected = [task_id for task_id in selected if task_id > args.start_after]
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def _result_path(output_root: Path, skill_config: str, task_id: str, trial_id: str) -> Path:
    task_name, instance_name = Path(task_id).parts[-2:]
    return output_root / skill_config / task_name / instance_name / trial_id / "result.json"


def _build_task_command(
    args: argparse.Namespace, task_id: str, trial_id: str, skill_config: str
) -> list[str]:
    cmd = [
        sys.executable,
        str(TASK_RUNNER),
        "--skilllearnbench-root",
        str(args.skilllearnbench_root),
        "--task-id",
        task_id,
        "--output-root",
        str(args.output_root),
        "--trial-id",
        trial_id,
        "--skill-config",
        skill_config,
        "--max-iterations",
        str(args.max_iterations),
        "--tool-timeout",
        str(args.tool_timeout),
        "--docker-build-timeout",
        str(args.docker_build_timeout),
        "--verifier-timeout",
        str(args.verifier_timeout),
    ]
    if args.apt_mirror:
        cmd.extend(["--apt-mirror", args.apt_mirror])
    if args.apt_security_mirror:
        cmd.extend(["--apt-security-mirror", args.apt_security_mirror])
    if args.ubuntu_apt_mirror:
        cmd.extend(["--ubuntu-apt-mirror", args.ubuntu_apt_mirror])
    if args.ubuntu_apt_security_mirror:
        cmd.extend(["--ubuntu-apt-security-mirror", args.ubuntu_apt_security_mirror])
    if args.ubuntu_ports_apt_mirror:
        cmd.extend(["--ubuntu-ports-apt-mirror", args.ubuntu_ports_apt_mirror])
    if args.maven_mirror:
        cmd.extend(["--maven-mirror", args.maven_mirror])
    if args.pip_index_url:
        cmd.extend(["--pip-index-url", args.pip_index_url])
    if args.pip_extra_index_url:
        cmd.extend(["--pip-extra-index-url", args.pip_extra_index_url])
    if args.uv_index_url:
        cmd.extend(["--uv-index-url", args.uv_index_url])
    if args.uv_extra_index_url:
        cmd.extend(["--uv-extra-index-url", args.uv_extra_index_url])
    if args.hidden_verifier:
        cmd.append("--hidden-verifier")
    if args.inject_search_resolution:
        cmd.append("--inject-search-resolution")
    if args.resolution_agent_space:
        cmd.extend(["--resolution-agent-space", args.resolution_agent_space])
    if args.resolution_include_debug:
        cmd.append("--resolution-include-debug")
    cmd.extend(["--resolution-user-memory-limit", str(args.resolution_user_memory_limit)])
    cmd.extend(["--resolution-experiences-limit", str(args.resolution_experiences_limit)])
    cmd.extend(["--resolution-tools-memory-limit", str(args.resolution_tools_memory_limit)])
    cmd.extend(["--resolution-skills-limit", str(args.resolution_skills_limit)])
    cmd.extend(["--resolution-skills-memory-limit", str(args.resolution_skills_memory_limit)])
    cmd.extend(
        ["--resolution-trajectory-grounding-limit", str(args.resolution_trajectory_grounding_limit)]
    )
    cmd.extend(["--resolution-pack-max-tokens", str(args.resolution_pack_max_tokens)])
    cmd.extend(["--resolution-skill-content-mode", args.resolution_skill_content_mode])
    if not args.resolution_allow_trajectory_grounding:
        cmd.append("--no-resolution-trajectory-grounding")
    if args.allow_resolution_failure:
        cmd.append("--allow-resolution-failure")
    if args.config:
        cmd.extend(["--config", str(args.config)])
    if args.skill_source:
        cmd.extend(["--skill-source", str(args.skill_source)])
    elif args.skill_source_root:
        task_name = Path(task_id).parts[-2]
        candidate = args.skill_source_root / task_name
        if candidate.exists():
            cmd.extend(["--skill-source", str(candidate)])
    if args.keep_container:
        cmd.append("--keep-container")
    if args.remove_image:
        cmd.append("--remove-image")
    if args.commit_existing_trajectory_memory:
        cmd.append("--commit-existing-trajectory-memory")
    if args.commit_trajectory_memory:
        cmd.append("--commit-trajectory-memory")
    if args.commit_trajectory_memory_on_fail:
        cmd.append("--commit-trajectory-memory-on-fail")
    if args.wait_memory_task:
        cmd.append("--wait-memory-task")
    if args.memory_task_timeout is not None:
        cmd.extend(["--memory-task-timeout", str(args.memory_task_timeout)])
    return cmd


def _write_summary(suite_dir: Path, rows: list[dict[str, Any]]) -> None:
    passed = sum(1 for row in rows if row.get("passed") is True)
    failed = sum(1 for row in rows if row.get("passed") is False)
    errored = sum(
        1 for row in rows if row.get("result_missing") or row.get("returncode") not in (0, 1)
    )
    total_tokens = sum(int((row.get("token_usage") or {}).get("total_tokens") or 0) for row in rows)
    summary = {
        "total": len(rows),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "pass_rate": passed / len(rows) if rows else 0.0,
        "total_tokens": total_tokens,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    (suite_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fieldnames = [
        "task_id",
        "returncode",
        "passed",
        "reward",
        "iteration",
        "total_tokens",
        "agent_error",
        "search_resolution_id",
        "search_resolution_pack_chars",
        "search_resolution_error",
        "memory_commit_session_id",
        "memory_task_id",
        "memory_commit_error",
        "verifier_exit",
        "result_path",
        "trial_path",
    ]
    with (suite_dir / "summary.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            token_usage = row.get("token_usage") or {}
            memory_commit = row.get("memory_commit") or {}
            memory_commit_payload = (
                memory_commit.get("commit") if isinstance(memory_commit, dict) else {}
            )
            search_resolution = row.get("search_resolution") or {}
            writer.writerow(
                {
                    "task_id": row.get("task_id"),
                    "returncode": row.get("returncode"),
                    "passed": row.get("passed"),
                    "reward": row.get("reward"),
                    "iteration": row.get("iteration"),
                    "total_tokens": token_usage.get("total_tokens"),
                    "agent_error": row.get("agent_error"),
                    "search_resolution_id": search_resolution.get("resolution_id")
                    if isinstance(search_resolution, dict)
                    else None,
                    "search_resolution_pack_chars": search_resolution.get("pack_markdown_chars")
                    if isinstance(search_resolution, dict)
                    else None,
                    "search_resolution_error": row.get("search_resolution_error"),
                    "memory_commit_session_id": memory_commit.get("session_id")
                    if isinstance(memory_commit, dict)
                    else None,
                    "memory_task_id": memory_commit_payload.get("task_id")
                    if isinstance(memory_commit_payload, dict)
                    else None,
                    "memory_commit_error": row.get("memory_commit_error"),
                    "verifier_exit": row.get("verifier_exit"),
                    "result_path": row.get("result_path"),
                    "trial_path": row.get("trial_path"),
                }
            )


def _run_one(
    *,
    args: argparse.Namespace,
    suite_dir: Path,
    task_id: str,
    index: int,
    total: int,
    trial_id: str,
    skill_config: str,
) -> dict[str, Any]:
    result_json = _result_path(args.output_root, skill_config, task_id, trial_id)
    if (
        args.skip_existing
        and result_json.exists()
        and not (args.commit_existing_trajectory_memory and args.commit_trajectory_memory)
    ):
        result = _load_json(result_json)
        return {
            "task_id": task_id,
            "returncode": 0 if result.get("passed") else 1,
            "result_path": str(result_json),
            "skipped_existing": True,
            **result,
        }

    cmd = _build_task_command(args, task_id, trial_id, skill_config)
    log_prefix = suite_dir / "logs" / f"{index:03d}-{_safe_name(task_id)}"
    log_prefix.parent.mkdir(parents=True, exist_ok=True)
    print(f"[{index}/{total}] {task_id}", flush=True)
    print("  " + " ".join(cmd), flush=True)

    with (
        (log_prefix.with_suffix(".stdout.txt")).open("w", encoding="utf-8") as stdout_f,
        (log_prefix.with_suffix(".stderr.txt")).open("w", encoding="utf-8") as stderr_f,
    ):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stream_console = getattr(args, "parallel", 1) <= 1

        def pump(stream: Any, sink: Any, chunks: list[str], prefix: str) -> None:
            for line in stream:
                sink.write(line)
                sink.flush()
                chunks.append(line)
                if stream_console:
                    print(prefix + line, end="", flush=True)

        stdout_thread = threading.Thread(
            target=pump, args=(proc.stdout, stdout_f, stdout_chunks, "  ")
        )
        stderr_thread = threading.Thread(
            target=pump, args=(proc.stderr, stderr_f, stderr_chunks, "  STDERR: ")
        )
        stdout_thread.start()
        stderr_thread.start()
        returncode = proc.wait()
        stdout_thread.join()
        stderr_thread.join()

    result = _load_json(result_json)
    row = {
        "task_id": task_id,
        "returncode": returncode,
        "result_path": str(result_json),
        "stdout_tail": "".join(stdout_chunks)[-2000:],
        "stderr_tail": "".join(stderr_chunks)[-2000:],
        **result,
    }
    if not result:
        row["result_missing"] = True
    status = "PASS" if row.get("passed") is True else "FAIL"
    if row.get("result_missing"):
        status = "NO_RESULT"
    print(f"  => {status} returncode={returncode} result={result_json}", flush=True)
    return row


def _write_result_row(suite_dir: Path, row: dict[str, Any]) -> None:
    with (suite_dir / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run many SkillLearnBench tasks with VikingBot.")
    parser.add_argument("--skilllearnbench-root", type=Path, default=DEFAULT_SKILLLEARNBENCH_ROOT)
    parser.add_argument("--output-root", type=Path, default=BENCH_DIR / "result")
    parser.add_argument("--suite-id", default=f"vikingbot-full-{_timestamp_id()}")
    parser.add_argument("--skill-config", default=None)
    parser.add_argument("--skill-source", type=Path)
    parser.add_argument("--skill-source-root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--task",
        action="append",
        help="Task family, instance id, or full task id to include. Repeatable.",
    )
    parser.add_argument(
        "--exclude-task",
        action="append",
        help="Task family, instance id, or full task id to exclude.",
    )
    parser.add_argument("--start-after", help="Resume after this full task id.")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Maximum number of task instances to run concurrently.",
    )
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--tool-timeout", type=int, default=300)
    parser.add_argument("--docker-build-timeout", type=int, default=1800)
    parser.add_argument(
        "--apt-mirror", help="Optional Debian apt mirror injected into task Docker builds."
    )
    parser.add_argument("--apt-security-mirror", help="Optional Debian security apt mirror.")
    parser.add_argument(
        "--ubuntu-apt-mirror", help="Optional Ubuntu apt mirror injected into task Docker builds."
    )
    parser.add_argument("--ubuntu-apt-security-mirror", help="Optional Ubuntu security apt mirror.")
    parser.add_argument(
        "--ubuntu-ports-apt-mirror",
        help="Optional Ubuntu ports apt mirror for arm64 Docker builds.",
    )
    parser.add_argument(
        "--maven-mirror", help="Optional Maven mirror URL written to /root/.m2/settings.xml."
    )
    parser.add_argument(
        "--pip-index-url", help="Optional pip index URL injected into task Docker builds."
    )
    parser.add_argument(
        "--pip-extra-index-url",
        help="Optional pip extra index URL injected into task Docker builds.",
    )
    parser.add_argument(
        "--uv-index-url", help="Optional uv index URL injected into task Docker builds."
    )
    parser.add_argument(
        "--uv-extra-index-url", help="Optional uv extra index URL injected into task Docker builds."
    )
    parser.add_argument("--verifier-timeout", type=int, default=1800)
    parser.add_argument(
        "--hidden-verifier",
        action="store_true",
        help="Do not expose /tests while the agent runs; copy tests only for verifier execution.",
    )
    parser.add_argument(
        "--inject-search-resolution",
        action="store_true",
        help="Inject OpenViking /api/v1/search/resolution output into each task agent context.",
    )
    parser.add_argument("--resolution-agent-space", default="default")
    parser.add_argument("--resolution-include-debug", action="store_true")
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
    )
    parser.set_defaults(resolution_allow_trajectory_grounding=True)
    parser.add_argument("--allow-resolution-failure", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--remove-image", action="store_true")
    parser.add_argument(
        "--commit-existing-trajectory-memory",
        action="store_true",
        help="With --commit-trajectory-memory, import existing trial trajectories without rerunning Docker.",
    )
    parser.add_argument(
        "--commit-trajectory-memory",
        action="store_true",
        help=(
            "Import every generated task trajectory into OpenViking and commit "
            "it with self + task instance peer memory enabled."
        ),
    )
    parser.add_argument(
        "--commit-trajectory-memory-on-fail",
        action="store_true",
        help="Deprecated compatibility flag. Trajectories are committed on fail by default when --commit-trajectory-memory is enabled.",
    )
    parser.add_argument(
        "--wait-memory-task",
        action="store_true",
        help="Wait for each OpenViking memory extraction task to finish.",
    )
    parser.add_argument("--memory-task-timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    args.skilllearnbench_root = args.skilllearnbench_root.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    if args.config:
        args.config = args.config.expanduser().resolve()
    if args.skill_source:
        args.skill_source = args.skill_source.expanduser().resolve()
    if args.skill_source_root:
        args.skill_source_root = args.skill_source_root.expanduser().resolve()

    all_tasks = _discover_tasks(args.skilllearnbench_root)
    selected_tasks = _filter_tasks(all_tasks, args)
    skill_config = args.skill_config or (
        args.skill_source.name
        if args.skill_source
        else args.skill_source_root.name
        if args.skill_source_root
        else "no_skill_resolution"
        if args.inject_search_resolution
        else "no_skill"
    )
    suite_dir = args.output_root / "suites" / args.suite_id
    suite_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "suite_id": args.suite_id,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "python": sys.executable,
        "skilllearnbench_root": str(args.skilllearnbench_root),
        "output_root": str(args.output_root),
        "skill_config": skill_config,
        "task_count": len(selected_tasks),
        "tasks": selected_tasks,
        "args": _jsonable(vars(args)),
    }
    (suite_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (suite_dir / "tasks.txt").write_text("\n".join(selected_tasks) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {k: manifest[k] for k in ("suite_id", "python", "skilllearnbench_root", "task_count")},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Suite dir: {suite_dir}", flush=True)
    if args.dry_run:
        return 0

    rows: list[dict[str, Any]] = []
    if args.parallel <= 1:
        for index, task_id in enumerate(selected_tasks, 1):
            row = _run_one(
                args=args,
                suite_dir=suite_dir,
                task_id=task_id,
                index=index,
                total=len(selected_tasks),
                trial_id=args.suite_id,
                skill_config=skill_config,
            )
            rows.append(row)
            _write_result_row(suite_dir, row)
            _write_summary(suite_dir, rows)
            if args.stop_on_error and (
                row.get("returncode") not in (0, 1) or row.get("result_missing")
            ):
                return int(row.get("returncode") or 3)
    else:
        completed: dict[int, dict[str, Any]] = {}
        print(
            f"Running with parallel={args.parallel}; live task output is written to suite logs.",
            flush=True,
        )
        executor = ThreadPoolExecutor(max_workers=args.parallel)
        futures = {
            executor.submit(
                _run_one,
                args=args,
                suite_dir=suite_dir,
                task_id=task_id,
                index=index,
                total=len(selected_tasks),
                trial_id=args.suite_id,
                skill_config=skill_config,
            ): (index, task_id)
            for index, task_id in enumerate(selected_tasks, 1)
        }
        try:
            for future in as_completed(futures):
                index, task_id = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    result_json = _result_path(
                        args.output_root, skill_config, task_id, args.suite_id
                    )
                    row = {
                        "task_id": task_id,
                        "returncode": 3,
                        "result_path": str(result_json),
                        "result_missing": True,
                        "suite_error": repr(exc),
                    }
                    print(
                        f"  => NO_RESULT returncode=3 result={result_json} error={exc!r}",
                        flush=True,
                    )
                completed[index] = row
                rows = [completed[row_index] for row_index in sorted(completed)]
                _write_result_row(suite_dir, row)
                _write_summary(suite_dir, rows)
                if args.stop_on_error and (
                    row.get("returncode") not in (0, 1) or row.get("result_missing")
                ):
                    for pending in futures:
                        pending.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    return int(row.get("returncode") or 3)
        except KeyboardInterrupt:
            for pending in futures:
                pending.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    _write_summary(suite_dir, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
