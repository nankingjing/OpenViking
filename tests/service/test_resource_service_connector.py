# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Unit tests for ResourceService Connector routing."""

from types import SimpleNamespace

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service import resource_service as resource_service_module
from openviking.service.resource_service import ResourceService
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.session.user_id import UserIdentifier


class RecordingResourceProcessor:
    def __init__(self):
        self.calls = []

    async def process_resource(self, **kwargs):
        self.calls.append(kwargs)
        return {"root_uri": kwargs.get("to") or "viking://resources/standard"}


class RecordingTaskTracker:
    def __init__(self):
        self.created = []
        self.started = []
        self.stages = []
        self.completed = []
        self.failed = []

    async def create(self, task_type, **kwargs):
        self.created.append({"task_type": task_type, **kwargs})
        return SimpleNamespace(task_id=f"task-{len(self.created)}")

    async def start(self, task_id, **kwargs):
        self.started.append({"task_id": task_id, **kwargs})

    async def update_stage(self, task_id, stage, **kwargs):
        self.stages.append({"task_id": task_id, "stage": stage, **kwargs})

    async def complete(self, task_id, result, **kwargs):
        self.completed.append({"task_id": task_id, "result": result, **kwargs})

    async def fail(self, task_id, error, **kwargs):
        self.failed.append({"task_id": task_id, "error": error, **kwargs})


class FakeBackgroundTask:
    def add_done_callback(self, callback):
        self.callback = callback


class RecordingConnectorClient:
    instances = []

    def __init__(self, doc_add_url, task_info_url, account_id=""):
        self.doc_add_url = doc_add_url
        self.task_info_url = task_info_url
        self.account_id = account_id
        self.submit_calls = []
        RecordingConnectorClient.instances.append(self)

    async def submit_doc_add(self, **kwargs):
        self.submit_calls.append(kwargs)
        return {"TaskKey": "connector-task-1"}


@pytest.fixture
def request_context():
    return RequestContext(
        user=UserIdentifier("acct", "alice"),
        role=Role.USER,
        api_key="sk-test",
    )


@pytest.fixture
def resource_processor():
    return RecordingResourceProcessor()


@pytest.fixture
def resource_service(resource_processor):
    return ResourceService(
        vikingdb=object(),
        viking_fs=object(),
        resource_processor=resource_processor,
        skill_processor=object(),
    )


def install_connector_config(
    monkeypatch,
    *,
    enable=True,
    allowed_add_types=None,
    poll_interval_ms=1,
    timeout_seconds=1,
):
    import openviking_cli.utils.config.open_viking_config as config_module

    connector = SimpleNamespace(
        enable=enable,
        connector="https://connector.example/api/knowledge/doc/add",
        tracker="https://connector.example/api/task/info",
        main_account_id="main-account",
        timeout_seconds=timeout_seconds,
        poll_interval_ms=poll_interval_ms,
        allowed_add_types=allowed_add_types or ["tos"],
    )
    monkeypatch.setattr(
        config_module,
        "get_openviking_config",
        lambda: SimpleNamespace(connector=connector),
    )
    return connector


def install_task_tracker(monkeypatch):
    tracker = RecordingTaskTracker()
    monkeypatch.setattr(
        "openviking.service.task_tracker.get_task_tracker",
        lambda: tracker,
    )
    return tracker


def install_noop_background_tasks(monkeypatch):
    def fake_create_task(coro):
        if hasattr(coro, "close"):
            coro.close()
        return FakeBackgroundTask()

    monkeypatch.setattr(resource_service_module.asyncio, "create_task", fake_create_task)


@pytest.mark.asyncio
async def test_add_resource_routes_allowed_add_type_to_connector(
    monkeypatch,
    resource_service,
    resource_processor,
    request_context,
):
    install_connector_config(monkeypatch, enable=True, allowed_add_types=["tos"])
    tracker = install_task_tracker(monkeypatch)
    install_noop_background_tasks(monkeypatch)
    RecordingConnectorClient.instances = []
    monkeypatch.setattr(
        resource_service_module,
        "ConnectorClient",
        RecordingConnectorClient,
    )

    result = await resource_service.add_resource(
        path="tos://bucket/input",
        ctx=request_context,
        to="viking://resources/spec",
        reason="Import from Connector",
        args={
            "add_type": "tos",
            "tos_path": "tos://bucket/input",
            "path_prefix": ["docs"],
            "include_child": False,
            "parser": "pdf",
        },
    )

    assert result == {
        "status": "accepted",
        "task_id": "task-1",
        "connector_task_key": "connector-task-1",
        "resource_id": "viking://resources/spec",
    }
    assert resource_processor.calls == []
    connector = RecordingConnectorClient.instances[0]
    assert connector.doc_add_url == "https://connector.example/api/knowledge/doc/add"
    assert connector.task_info_url == "https://connector.example/api/task/info"
    assert connector.account_id == "main-account"
    assert connector.submit_calls == [
        {
            "resource_id": "viking://resources/spec",
            "add_type": "tos",
            "api_key": "sk-test",
            "tos_path": "tos://bucket/input",
            "path_prefix": ["docs"],
            "include_child": False,
            "extra_params": {"parser": "pdf"},
        }
    ]
    assert tracker.created == [
        {
            "task_type": "connector_import",
            "resource_id": "viking://resources/spec",
            "account_id": "acct",
            "user_id": "alice",
        }
    ]


@pytest.mark.asyncio
async def test_add_resource_falls_back_when_add_type_is_not_allowed(
    monkeypatch,
    resource_service,
    resource_processor,
    request_context,
):
    install_connector_config(monkeypatch, enable=True, allowed_add_types=["tos"])
    install_task_tracker(monkeypatch)
    install_noop_background_tasks(monkeypatch)
    monkeypatch.setattr(
        resource_service,
        "_should_use_understanding_api",
        lambda _path: False,
    )

    result = await resource_service.add_resource(
        path="https://example.com/doc",
        ctx=request_context,
        to="viking://resources/fallback",
        args={"add_type": "web"},
    )

    assert result["root_uri"] == "viking://resources/fallback"
    assert resource_processor.calls[0]["path"] == "https://example.com/doc"
    assert resource_processor.calls[0]["add_type"] == "web"


@pytest.mark.asyncio
async def test_add_resource_falls_back_when_connector_is_disabled(
    monkeypatch,
    resource_service,
    resource_processor,
    request_context,
):
    install_connector_config(monkeypatch, enable=False, allowed_add_types=["tos"])
    install_task_tracker(monkeypatch)
    install_noop_background_tasks(monkeypatch)
    monkeypatch.setattr(
        resource_service,
        "_should_use_understanding_api",
        lambda _path: False,
    )

    result = await resource_service.add_resource(
        path="tos://bucket/input",
        ctx=request_context,
        to="viking://resources/disabled",
        args={"add_type": "tos"},
    )

    assert result["root_uri"] == "viking://resources/disabled"
    assert len(resource_processor.calls) == 1


@pytest.mark.asyncio
async def test_connector_route_requires_request_api_key(
    monkeypatch,
    resource_service,
):
    install_connector_config(monkeypatch, enable=True, allowed_add_types=["tos"])
    ctx = RequestContext(
        user=UserIdentifier("acct", "alice"),
        role=Role.USER,
        api_key=None,
    )

    with pytest.raises(InvalidArgumentError, match="API key"):
        await resource_service.add_resource(
            path="tos://bucket/input",
            ctx=ctx,
            to="viking://resources/spec",
            args={"add_type": "tos"},
        )


@pytest.mark.asyncio
async def test_monitor_connector_task_completes_succeeded_status(monkeypatch, request_context):
    tracker = install_task_tracker(monkeypatch)
    service = ResourceService()
    client = SimpleNamespace(get_task_info=lambda _task_key: {"Status": "succeeded"})

    async def get_task_info(task_key):
        return {"Status": "succeeded", "TaskKey": task_key}

    client.get_task_info = get_task_info

    await service._monitor_connector_task(
        client=client,
        connector_task_key="connector-task-1",
        ov_task_id="task-1",
        resource_id="viking://resources/spec",
        poll_interval_ms=1,
        timeout_seconds=1,
        ctx=request_context,
    )

    assert tracker.started == [
        {"task_id": "task-1", "account_id": "acct", "user_id": "alice"}
    ]
    assert tracker.stages[-1]["stage"] == "connector:succeeded"
    assert tracker.completed == [
        {
            "task_id": "task-1",
            "result": {
                "connector_status": "succeeded",
                "connector_task_key": "connector-task-1",
            },
            "account_id": "acct",
            "user_id": "alice",
        }
    ]
    assert tracker.failed == []


@pytest.mark.asyncio
async def test_monitor_connector_task_fails_terminal_error_status(monkeypatch, request_context):
    tracker = install_task_tracker(monkeypatch)
    service = ResourceService()

    async def get_task_info(_task_key):
        return {"status": "failed", "error_message": "source unavailable"}

    client = SimpleNamespace(get_task_info=get_task_info)

    await service._monitor_connector_task(
        client=client,
        connector_task_key="connector-task-1",
        ov_task_id="task-1",
        resource_id="viking://resources/spec",
        poll_interval_ms=1,
        timeout_seconds=1,
        ctx=request_context,
    )

    assert tracker.stages[-1]["stage"] == "connector:failed"
    assert tracker.failed == [
        {
            "task_id": "task-1",
            "error": "connector task failed: source unavailable",
            "account_id": "acct",
            "user_id": "alice",
        }
    ]
    assert tracker.completed == []
