# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for Feishu OpenAPI error mapping."""

import json
from types import SimpleNamespace

import pytest

from openviking.utils.feishu_errors import feishu_api_error_from_response, raise_from_lark_response
from openviking_cli.exceptions import (
    InvalidArgumentError,
    NotFoundError,
    PermissionDeniedError,
    ResourceExhaustedError,
    UnauthenticatedError,
    UnavailableError,
)


def _fake_response(
    *,
    code: int,
    msg: str,
    http_status: int = 400,
    error_payload: dict | None = None,
):
    body = {"code": code, "msg": msg}
    if error_payload:
        body["error"] = error_payload
    return SimpleNamespace(
        code=code,
        msg=msg,
        raw=SimpleNamespace(
            status_code=http_status,
            content=json.dumps(body).encode("utf-8"),
        ),
    )


def test_maps_1770032_to_permission_denied_with_tenant_hint():
    response = _fake_response(code=1770032, msg="forBidden", http_status=403)
    exc = feishu_api_error_from_response(
        response,
        operation="fetch document blocks",
        resource="doc_token",
        using_user_token=False,
    )
    assert isinstance(exc, PermissionDeniedError)
    assert exc.code == "PERMISSION_DENIED"
    assert exc.details["feishu_code"] == 1770032
    assert exc.details["http_status"] == 403
    assert "required document scopes" in exc.details["hint"]
    assert "app can access" in exc.details["hint"]


def test_maps_1770032_with_user_token_hint():
    response = _fake_response(code=1770032, msg="forBidden", http_status=403)
    exc = feishu_api_error_from_response(
        response,
        operation="fetch document blocks",
        using_user_token=True,
    )
    assert "Share the document" in exc.details["hint"]


def test_maps_permission_violations_from_error_payload():
    response = _fake_response(
        code=99991672,
        msg="Access denied",
        http_status=403,
        error_payload={
            "permission_violations": [
                {"scope": "docx:document:readonly", "url": "https://open.feishu.cn/apps/cli_x/auth"}
            ]
        },
    )
    exc = feishu_api_error_from_response(response, operation="fetch document blocks")
    assert isinstance(exc, PermissionDeniedError)
    assert exc.details["permission_violations"][0]["scope"] == "docx:document:readonly"
    assert "docx:document:readonly" in exc.details["hint"]


def test_maps_1770002_to_not_found():
    response = _fake_response(code=1770002, msg="not found", http_status=404)
    exc = feishu_api_error_from_response(
        response,
        operation="fetch document blocks",
        resource="missing_doc",
    )
    assert isinstance(exc, NotFoundError)
    assert exc.details["feishu_code"] == 1770002


def test_maps_rate_limit_code():
    response = _fake_response(code=99991400, msg="too many requests", http_status=400)
    exc = feishu_api_error_from_response(response, operation="fetch document blocks")
    assert isinstance(exc, ResourceExhaustedError)


def test_maps_server_error_code():
    response = _fake_response(code=1771001, msg="server internal error", http_status=500)
    exc = feishu_api_error_from_response(response, operation="fetch document blocks")
    assert isinstance(exc, UnavailableError)


def test_maps_unauthenticated_code():
    response = _fake_response(code=99991631, msg="get session fail", http_status=401)
    exc = feishu_api_error_from_response(response, operation="resolve wiki node")
    assert isinstance(exc, UnauthenticatedError)


def test_raise_from_lark_response_raises_typed_error():
    response = _fake_response(code=1770001, msg="invalid param", http_status=400)
    with pytest.raises(InvalidArgumentError) as exc_info:
        raise_from_lark_response(response, operation="resolve wiki node", resource="wiki_token")
    assert exc_info.value.details["feishu_code"] == 1770001
