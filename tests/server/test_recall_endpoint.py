# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0


import httpx

from openviking_cli.retrieve import ContextType, MatchedContext


class _FakeFindResult:
    def __init__(self, memories):
        self.memories = memories


def _memory(uri: str, score: float = 0.9, abstract: str = ""):
    return MatchedContext(
        uri=uri,
        context_type=ContextType.MEMORY,
        level=2,
        score=score,
        abstract=abstract,
        category=uri.split("/memories/", 1)[-1].split("/", 1)[0],
    )


async def test_recall_endpoint_searches_by_type_quota_and_renders(
    client: httpx.AsyncClient,
    service,
    monkeypatch,
):
    calls = []

    async def fake_find(**kwargs):
        calls.append(kwargs)
        target_uri = kwargs["target_uri"]
        if target_uri.endswith("/events"):
            return _FakeFindResult(
                [
                    _memory(
                        "viking://user/test_user/memories/events/launch.md",
                        0.91,
                        "Launch decision",
                    )
                ]
            )
        if target_uri.endswith("/entities"):
            return _FakeFindResult(
                [
                    _memory(
                        "viking://user/test_user/memories/entities/openviking.md",
                        0.82,
                        "OpenViking project",
                    )
                ]
            )
        return _FakeFindResult([])

    async def fake_read(uri, **kwargs):
        if uri.endswith("/launch.md"):
            return "Summary: Ship stdio MCP proxy.\n2026-07-06 ChatLog: details"
        return "OpenViking is the target project."

    monkeypatch.setattr(service.search, "find", fake_find)
    monkeypatch.setattr(service.fs, "read", fake_read)

    resp = await client.post(
        "/api/v1/search/recall",
        json={
            "query": "what should I remember",
            "quotas": {"events": 1, "entities": 1, "preferences": 0, "experiences": 0},
            "max_chars": 200,
            "min_score": 0.1,
            "render": True,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    result = body["result"]
    assert result["stats"]["returned"] == 2
    assert [entry["type"] for entry in result["entries"]] == ["events", "entities"]
    assert '<memory_group type="events"' in result["rendered"]
    assert "<summary>Ship stdio MCP proxy.</summary>" in result["rendered"]
    assert "viking://user/test_user/memories/entities/openviking.md" in result["rendered"]
    assert [call["target_uri"].rsplit("/", 1)[-1] for call in calls] == ["events", "entities"]


async def test_recall_endpoint_rejects_unknown_fields(client: httpx.AsyncClient):
    resp = await client.post(
        "/api/v1/search/recall",
        json={"query": "hello", "unexpected": "value"},
    )

    assert resp.status_code == 400


async def test_recall_endpoint_filters_profile_and_duplicates(
    client: httpx.AsyncClient,
    service,
    monkeypatch,
):
    async def fake_find(**kwargs):
        del kwargs
        duplicate = _memory("viking://user/test_user/memories/events/dup.md", 0.8, "same")
        profile = _memory("viking://user/test_user/memories/profile.md", 0.99, "profile")
        return _FakeFindResult([profile, duplicate, duplicate])

    async def fake_read(uri, **kwargs):
        del kwargs
        if uri.endswith("profile.md"):
            return "profile"
        return "duplicate content"

    monkeypatch.setattr(service.search, "find", fake_find)
    monkeypatch.setattr(service.fs, "read", fake_read)

    resp = await client.post(
        "/api/v1/search/recall",
        json={"query": "hello", "quotas": {"events": 3, "entities": 0, "preferences": 0}},
    )

    assert resp.status_code == 200
    entries = resp.json()["result"]["entries"]
    assert [entry["uri"] for entry in entries] == ["viking://user/test_user/memories/events/dup.md"]
