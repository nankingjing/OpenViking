# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import pytest

import openviking.pyagfs.async_client as async_client
from openviking.pyagfs import AsyncAGFSClient


class _SyncAGFS:
    def read(self, path, **kwargs):
        return ("read", path, kwargs)

    def write(self, path, data, **kwargs):
        return ("write", path, data, kwargs)

    def rm(self, path, **kwargs):
        return ("rm", path, kwargs)


class _TreeOnlyAGFS:
    def tree_directory(self, path, **kwargs):
        return [
            {"path": f"{path}/a.md", "name": "a.md", "is_dir": False},
            {"path": f"{path}/notes/b.txt", "name": "b.txt", "is_dir": False},
            {"path": f"{path}/notes/c.md", "name": "c.md", "is_dir": False},
        ]


@pytest.mark.asyncio
async def test_async_agfs_client_hides_threadpool(monkeypatch):
    to_thread_calls = []

    async def fake_to_thread(func, *args, **kwargs):
        to_thread_calls.append((func.__name__, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(async_client.asyncio, "to_thread", fake_to_thread)

    sync_agfs = _SyncAGFS()
    agfs = AsyncAGFSClient(sync_agfs)

    assert agfs.sync_client is sync_agfs
    assert await agfs.write("/tasks/1", b"data") == (
        "write",
        "/tasks/1",
        b"data",
        {"ctx": {"account_id": "_system"}},
    )
    assert await agfs.read("/queue/dequeue") == (
        "read",
        "/queue/dequeue",
        {"ctx": {"account_id": "_system"}},
    )
    assert await agfs.rm("/redo/id", recursive=True) == (
        "rm",
        "/redo/id",
        {"recursive": True, "ctx": {"account_id": "_system"}},
    )

    assert to_thread_calls == [
        ("write", ("/tasks/1", b"data"), {"ctx": {"account_id": "_system"}}),
        ("read", ("/queue/dequeue",), {"ctx": {"account_id": "_system"}}),
        (
            "rm",
            ("/redo/id",),
            {"recursive": True, "ctx": {"account_id": "_system"}},
        ),
    ]


@pytest.mark.asyncio
async def test_glob_directory_falls_back_to_tree_directory_when_binding_lacks_glob():
    agfs = AsyncAGFSClient(_TreeOnlyAGFS())

    result = await agfs.glob_directory("/local/acct/resources", "**/*.md", page_size=1)

    assert [entry["name"] for entry in result["entries"]] == ["a.md"]
    assert result["next_token"] == "1"

    next_result = await agfs.glob_directory(
        "/local/acct/resources", "**/*.md", page_size=1, continuation_token="1"
    )
    assert [entry["name"] for entry in next_result["entries"]] == ["c.md"]
    assert next_result["next_token"] is None
