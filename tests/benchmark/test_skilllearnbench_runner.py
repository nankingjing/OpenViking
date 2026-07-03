import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmark"
    / "skillLearnBench"
    / "scripts"
    / "run_vikingbot_task.py"
)
TRAJ_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmark"
    / "skillLearnBench"
    / "scripts"
    / "run_vikingbot_trajectory_eval.py"
)


def load_runner():
    spec = importlib.util.spec_from_file_location("skilllearnbench_runner_under_test", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_traj_runner():
    spec = importlib.util.spec_from_file_location(
        "skilllearnbench_traj_runner_under_test", TRAJ_RUNNER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_task(root: Path, *, workdir: str = "/app", task_toml: str = "") -> str:
    task_id = "demo-task/demo-task-1"
    task_path = root / "tasks" / "demo-task" / "demo-task-1"
    env_dir = task_path / "environment"
    tests_dir = task_path / "tests"
    env_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (env_dir / "Dockerfile").write_text(
        f"FROM ubuntu:24.04\nRUN apt-get update\nWORKDIR {workdir}\nCOPY skills /root/.agents/skills\n",
        encoding="utf-8",
    )
    (task_path / "instruction.md").write_text("Create the requested artifact.", encoding="utf-8")
    (task_path / "task.toml").write_text(task_toml, encoding="utf-8")
    (tests_dir / "test.sh").write_text(
        "#!/bin/bash\nmkdir -p /logs/verifier\necho 1 > /logs/verifier/reward.txt\n",
        encoding="utf-8",
    )
    return task_id


def test_dry_run_resolves_task_metadata(tmp_path, monkeypatch, capsys):
    runner = load_runner()
    slb_root = tmp_path / "SkillLearnBench"
    task_id = make_task(slb_root, workdir="/workspace")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_vikingbot_task.py",
            "--skilllearnbench-root",
            str(slb_root),
            "--task-id",
            task_id,
            "--output-root",
            str(tmp_path / "out"),
            "--trial-id",
            "trial-a",
            "--dry-run",
        ],
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == task_id
    assert payload["container_workdir"] == "/workspace"
    assert payload["trial_path"].endswith("/no_skill/demo-task/demo-task-1/trial-a")
    assert payload["required_env"] == []
    assert not (tmp_path / "out").exists()


def test_preflight_reports_missing_docker_and_default_config(tmp_path, monkeypatch, capsys):
    runner = load_runner()
    slb_root = tmp_path / "SkillLearnBench"
    task_id = make_task(slb_root)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runner, "_check_vikingbot_runtime", lambda _config_path: (True, "ok"))
    missing_config = tmp_path / "missing.ov.conf"
    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(missing_config))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_vikingbot_task.py",
            "--skilllearnbench-root",
            str(slb_root),
            "--task-id",
            task_id,
            "--output-root",
            str(tmp_path / "out"),
            "--trial-id",
            "trial-b",
            "--preflight",
        ],
    )

    assert runner.main() == 2
    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["docker_cli"]["ok"] is False
    assert checks["vikingbot_runtime"]["ok"] is True
    assert checks["config_file"]["ok"] is False
    assert checks["config_file"]["detail"] == str(missing_config.resolve())
    assert payload["ok"] is False
    preflight_path = Path(payload["trial_path"]) / "preflight.json"
    assert json.loads(preflight_path.read_text(encoding="utf-8"))["ok"] is False


def test_preflight_success_with_fake_docker_and_default_config(tmp_path, monkeypatch, capsys):
    runner = load_runner()
    slb_root = tmp_path / "SkillLearnBench"
    task_id = make_task(slb_root, task_toml='required_env = ["GH_TOKEN"]\n')
    config_path = tmp_path / "home" / ".openviking" / "ov.conf"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"bot": {"agents": {"model": "openai/test", "api_key": "test-key"}}}', encoding="utf-8"
    )
    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(config_path))
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: f"/fake/bin/{name}" if name == "docker" else None,
    )
    monkeypatch.setattr(runner, "_run_optional", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(runner, "_check_vikingbot_runtime", lambda _config_path: (True, "ok"))
    monkeypatch.delenv("GH_TOKEN", raising=False)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_vikingbot_task.py",
            "--skilllearnbench-root",
            str(slb_root),
            "--task-id",
            task_id,
            "--output-root",
            str(tmp_path / "out"),
            "--trial-id",
            "trial-c",
            "--preflight",
        ],
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["config_path"] == str(config_path.resolve())
    assert payload["required_env"] == ["GH_TOKEN"]
    assert payload["warnings"] == ["Task-required env vars are missing: $GH_TOKEN"]
    assert all(check["ok"] for check in payload["checks"])


def test_prepare_build_env_normalizes_flat_markdown_skills(tmp_path):
    runner = load_runner()
    slb_root = tmp_path / "SkillLearnBench"
    task_id = make_task(slb_root)
    env_dir = slb_root / "tasks" / task_id / "environment"
    skill_src = tmp_path / "skills"
    skill_src.mkdir()
    (skill_src / "poster_helper.md").write_text("Use this poster skill.", encoding="utf-8")

    build_env = runner._prepare_build_env(env_dir, skill_src)
    try:
        skill_file = build_env / "skills" / "poster-helper" / "SKILL.md"
        assert skill_file.read_text(encoding="utf-8") == "Use this poster skill."
        assert (build_env / "Dockerfile").exists()
    finally:
        runner.shutil.rmtree(build_env.parent, ignore_errors=True)


def test_run_vikingbot_uses_current_context_api(tmp_path, monkeypatch):
    runner = load_runner()
    from vikingbot.config.schema import SessionKey

    captured = {}

    class FakeTools:
        def __init__(self):
            self.tool_names = ["exec", "read_file", "openviking_search"]
            self.unregistered = []
            self.registered = []

        def unregister(self, name):
            self.unregistered.append(name)

        def register(self, tool):
            self.registered.append(tool.name)

    class FakeContext:
        async def build_messages(self, **kwargs):
            captured["build_messages"] = kwargs
            assert "memory_users" not in kwargs
            return [
                {"role": "system", "content": "# vikingbot\nopenviking_memory_commit"},
                {"role": "system", "content": "base"},
            ]

    class FakeAgent:
        def __init__(self):
            self.tools = FakeTools()
            self.context = FakeContext()

        async def _run_agent_loop(self, **kwargs):
            captured["run_loop"] = kwargs
            return "done", "reasoning", [{"tool_name": "exec"}], {"total_tokens": 3}, 1

        async def close_mcp(self):
            captured["closed"] = True

    fake_agent = FakeAgent()
    monkeypatch.setattr(runner, "_build_agent", lambda *args, **kwargs: fake_agent)
    monkeypatch.setattr(runner, "SessionKey", SessionKey)

    trajectory_path = tmp_path / "trajectory.json"
    payload = asyncio.run(
        runner._run_vikingbot(
            config_path=tmp_path / "ov.conf",
            workspace=tmp_path / "workspace",
            instruction="Solve the task.",
            container_name="task-container",
            container_workdir="/app",
            max_iterations=2,
            tool_timeout=10,
            trajectory_path=trajectory_path,
        )
    )

    build_kwargs = captured["build_messages"]
    assert build_kwargs["ov_tools_enable"] is False
    assert build_kwargs["memory_peer_ids"] is None
    assert build_kwargs["memory_owner_user_ids"] is None
    assert fake_agent.tools.unregistered == ["exec", "read_file", "openviking_search"]
    assert fake_agent.tools.registered == [
        "exec",
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
    ]
    run_messages = captured["run_loop"]["messages"]
    assert run_messages[0]["role"] == "system"
    assert (
        "Only these tools are available: exec, read_file, write_file, edit_file, list_dir"
        in run_messages[0]["content"]
    )
    assert "/tests/test.sh" in run_messages[0]["content"]
    assert "Do not call web_search" in run_messages[0]["content"]
    assert run_messages[1]["content"] == "base"
    assert all(
        "openviking_memory_commit" not in message.get("content", "") for message in run_messages
    )
    assert captured["run_loop"]["ov_tools_enable"] is False
    assert captured["closed"] is True
    assert payload["final_content"] == "done"
    assert json.loads(trajectory_path.read_text(encoding="utf-8"))["iteration"] == 1


def test_build_agent_disables_openviking_hooks(tmp_path, monkeypatch):
    runner = load_runner()
    config = SimpleNamespace(
        bot_data_path=tmp_path / "bot_data",
        hooks=["vikingbot.hooks.builtins.openviking_hooks.hooks"],
        agents=SimpleNamespace(model="openai/test", memory_window=5, gen_image_model=None),
        tools=SimpleNamespace(
            web=SimpleNamespace(search=SimpleNamespace(api_key="")),
            exec=SimpleNamespace(timeout=30),
        ),
    )
    captured = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(runner, "_require_vikingbot_runtime", lambda: None)
    monkeypatch.setattr(runner, "ensure_config", lambda _path: config)
    monkeypatch.setattr(runner, "_init_bot_data", lambda _config: None)
    monkeypatch.setattr(runner, "MessageBus", lambda: "bus")
    monkeypatch.setattr(runner, "SessionManager", lambda path: ("session", path))
    monkeypatch.setattr(runner, "_make_provider", lambda _config: "provider")
    monkeypatch.setattr(runner, "AgentLoop", FakeAgentLoop)

    runner._build_agent(
        config_path=tmp_path / "ov.conf",
        workspace=tmp_path / "workspace",
        max_iterations=7,
        container_workdir="/app",
    )

    assert config.hooks == []
    assert captured["config"] is config
    assert captured["mcp_servers"] is None
    assert captured["max_iterations"] == 7


def test_agent_loop_skips_write_experience_when_ov_tools_disabled(monkeypatch):
    load_runner()
    import vikingbot.agent.loop as loop_module
    from vikingbot.agent.loop import AgentLoop
    from vikingbot.config.schema import SessionKey

    class FakeResponse:
        usage = None
        reasoning_content = None
        content = None
        has_tool_calls = True
        tool_calls = [
            SimpleNamespace(
                id="call-1",
                name="write_file",
                arguments={"path": "/tmp/a", "content": "x"},
                tokens=0,
            )
        ]

    class FakeTools:
        def get_definitions(self, **kwargs):
            return [{"type": "function", "function": {"name": "write_file"}}]

        async def execute(self, *args, **kwargs):
            return "ok"

    class FakeContext:
        def add_assistant_message(self, messages, content, tool_call_dicts, reasoning_content=None):
            return [
                *messages,
                {"role": "assistant", "content": content or "", "tool_calls": tool_call_dicts},
            ]

        def add_tool_result(self, messages, tool_call_id, tool_name, result):
            return [
                *messages,
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": result,
                },
            ]

    async def fake_chat_with_stream_events(**kwargs):
        return FakeResponse(), "", None

    def fail_load_config():
        raise AssertionError(
            "write-experience config should not be loaded when ov_tools_enable=False"
        )

    monkeypatch.setattr(loop_module, "load_config", fail_load_config)

    loop = AgentLoop.__new__(AgentLoop)
    loop.max_iterations = 1
    loop.bus = None
    loop.tools = FakeTools()
    loop.context = FakeContext()
    loop.sandbox_manager = SimpleNamespace(to_workspace_id=lambda _session_key: "workspace")
    loop._chat_with_stream_events = fake_chat_with_stream_events

    final_content, _reasoning, tools_used, _token_usage, iteration = asyncio.run(
        loop._run_agent_loop(
            messages=[{"role": "user", "content": "write a file"}],
            session_key=SessionKey(type="cli", channel_id="test", chat_id="test"),
            publish_events=False,
            ov_tools_enable=False,
        )
    )

    assert iteration == 1
    assert tools_used[0]["tool_name"] == "write_file"
    assert final_content == "Reached 1 iterations without completion."


def test_full_run_writes_result_with_fake_docker_and_agent(tmp_path, monkeypatch):
    runner = load_runner()
    slb_root = tmp_path / "SkillLearnBench"
    task_id = make_task(slb_root)
    output_root = tmp_path / "out"
    trial_path = output_root / "no_skill" / "demo-task" / "demo-task-1" / "trial-d"
    run_calls = []

    def fake_preflight(*, plan, config_path, required_env):
        return True, {
            **plan,
            "config_path": str(config_path),
            "checks": [{"name": "fake", "ok": True, "detail": "ok"}],
            "warnings": [],
            "errors": [],
            "ok": True,
        }

    async def fake_run_vikingbot(**kwargs):
        payload = {
            "final_content": "done",
            "reasoning_content": "",
            "tools_used": ["write_file"],
            "token_usage": {"total_tokens": 12},
            "iteration": 1,
            "messages": [],
        }
        kwargs["trajectory_path"].write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        if cmd[:3] == ["docker", "exec", "ov_slb_demo-task-demo-task-1_trial-d"]:
            verifier_dir = trial_path / "verifier"
            verifier_dir.mkdir(parents=True, exist_ok=True)
            (verifier_dir / "reward.txt").write_text("1", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(runner, "_run_preflight", fake_preflight)
    monkeypatch.setattr(runner, "_run_vikingbot", fake_run_vikingbot)
    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_run_streaming", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_vikingbot_task.py",
            "--skilllearnbench-root",
            str(slb_root),
            "--task-id",
            task_id,
            "--output-root",
            str(output_root),
            "--trial-id",
            "trial-d",
        ],
    )

    assert runner.main() == 0
    result = json.loads((trial_path / "result.json").read_text(encoding="utf-8"))
    assert result["passed"] is True
    assert result["reward"] == 1
    assert result["agent_final_content"] == "done"
    assert result["tools_used"] == ["write_file"]
    assert (trial_path / "agent" / "vikingbot-trajectory.json").exists()
    assert any(call[:2] == ["docker", "build"] for call in run_calls)
    assert any(call[:2] == ["docker", "run"] for call in run_calls)


def test_vikingbot_trajectory_converts_to_claude_jsonl():
    runner = load_traj_runner()
    payload = {
        "final_content": "done",
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        "messages": [
            {"role": "system", "content": "ignore"},
            {"role": "user", "content": "Solve it."},
            {"role": "user", "content": "Reflect on the results and decide next steps."},
            {
                "role": "assistant",
                "content": "I will inspect.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "list_dir", "arguments": '{"path":"/root"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "name": "list_dir", "content": "file.txt"},
        ],
    }

    lines = [
        json.loads(line) for line in runner.vikingbot_trajectory_to_jsonl(payload).splitlines()
    ]

    assert lines[0]["type"] == "user"
    assert lines[0]["message"]["content"][0]["text"] == "Solve it."
    assert lines[1]["message"]["content"][1]["type"] == "tool_use"
    assert lines[1]["message"]["content"][1]["name"] == "list_dir"
    assert lines[2]["message"]["content"][0]["type"] == "tool_result"
    assert lines[-2]["message"]["content"][0]["text"] == "done"
    assert lines[-1]["type"] == "result"
    assert lines[-1]["modelUsage"]["vikingbot"]["inputTokens"] == 10
    assert all("Reflect on" not in json.dumps(item) for item in lines)


def test_export_evaluation_log_writes_trajectory_and_reward(tmp_path):
    runner = load_traj_runner()
    output_root = tmp_path / "result"
    trial_path = output_root / "no_skill" / "demo-task" / "demo-task-1" / "trial-a"
    agent_dir = trial_path / "agent"
    verifier_dir = trial_path / "verifier"
    agent_dir.mkdir(parents=True)
    verifier_dir.mkdir()
    (agent_dir / "vikingbot-trajectory.json").write_text(
        json.dumps(
            {
                "final_content": "done",
                "token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "messages": [{"role": "user", "content": "Do task."}],
            }
        ),
        encoding="utf-8",
    )
    (verifier_dir / "reward.txt").write_text("1\n", encoding="utf-8")
    result_path = trial_path / "result.json"
    result_path.write_text(json.dumps({"reward": 1, "passed": True}), encoding="utf-8")
    suite_dir = output_root / "suites" / "suite-a"
    suite_dir.mkdir(parents=True)
    summary_csv = suite_dir / "summary.csv"
    summary_csv.write_text(
        "task_id,returncode,passed,reward,iteration,total_tokens,agent_error,verifier_exit,result_path,trial_path\n"
        f"demo-task/demo-task-1,0,True,1,1,3,,0,{result_path},{trial_path}\n",
        encoding="utf-8",
    )

    args = runner._build_parser().parse_args(
        [
            "--suite-id",
            "suite-a",
            "--output-root",
            str(output_root),
            "--trajectory-root",
            str(tmp_path / "trajectory_eval"),
            "--export-only",
        ]
    )
    args.output_root = args.output_root.resolve()
    args.trajectory_root = args.trajectory_root.resolve()
    manifest = runner.export_evaluation_log(args)

    exported = manifest["exported"][0]
    trajectory = Path(exported["trajectory"])
    assert trajectory.exists()
    assert '"type": "result"' in trajectory.read_text(encoding="utf-8")
    assert (trajectory.parents[1] / "verifier" / "reward.txt").read_text(encoding="utf-8") == "1\n"
