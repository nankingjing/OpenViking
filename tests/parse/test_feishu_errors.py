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


def test_maps_feishu_forbidden_to_permission_denied_and_keeps_details():
    response = _fake_response(
        code=1770032,
        msg="forBidden",
        http_status=400,
        error_payload={
            "permission_violations": [
                {"scope": "docx:document:readonly", "url": "https://open.feishu.cn/apps/cli_x/auth"}
            ]
        },
    )
    exc = feishu_api_error_from_response(
        response,
        operation="fetch document blocks",
        resource="doc_token",
    )

    assert isinstance(exc, PermissionDeniedError)
    assert exc.code == "PERMISSION_DENIED"
    assert "code=1770032, msg=forBidden" in exc.message
    assert exc.details["feishu_code"] == 1770032
    assert exc.details["feishu_msg"] == "forBidden"
    assert exc.details["http_status"] == 400
    assert (
        exc.details["feishu_error"]["permission_violations"][0]["scope"] == "docx:document:readonly"
    )


@pytest.mark.parametrize(
    ("http_status", "error_type"),
    [
        (401, UnauthenticatedError),
        (404, NotFoundError),
        (429, ResourceExhaustedError),
        (500, UnavailableError),
        (400, InvalidArgumentError),
    ],
)
def test_maps_http_status_to_openviking_error(http_status, error_type):
    response = _fake_response(code=123, msg="failed", http_status=http_status)

    exc = feishu_api_error_from_response(response, operation="resolve wiki node")

    assert isinstance(exc, error_type)
    assert exc.details["feishu_code"] == 123
    assert exc.details["http_status"] == http_status


def test_raise_from_lark_response_raises_typed_error():
    response = _fake_response(code=1770001, msg="invalid param", http_status=400)
    with pytest.raises(InvalidArgumentError) as exc_info:
        raise_from_lark_response(response, operation="resolve wiki node", resource="wiki_token")
    assert exc_info.value.details["feishu_code"] == 1770001
