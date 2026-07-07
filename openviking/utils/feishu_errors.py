# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Map failed Feishu/Lark OpenAPI responses to OpenViking errors."""

from __future__ import annotations

import json
from typing import Any, NoReturn, Optional

from openviking_cli.exceptions import (
    InvalidArgumentError,
    NotFoundError,
    OpenVikingError,
    PermissionDeniedError,
    ResourceExhaustedError,
    UnauthenticatedError,
    UnavailableError,
)
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

_FEISHU_DOCUMENT_FORBIDDEN = 1770032


def _response_http_status(response: Any) -> Optional[int]:
    raw = getattr(response, "raw", None)
    status = getattr(raw, "status_code", None)
    return int(status) if isinstance(status, int) else None


def _extract_error_payload(response: Any) -> dict[str, Any]:
    raw = getattr(response, "raw", None)
    content = getattr(raw, "content", None)
    if not content:
        return {}
    try:
        if isinstance(content, (bytes, bytearray)):
            body = json.loads(content.decode("utf-8"))
        elif isinstance(content, str):
            body = json.loads(content)
        else:
            return {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    error = body.get("error")
    return error if isinstance(error, dict) else {}


def feishu_api_error_from_response(
    response: Any,
    *,
    operation: str,
    resource: Optional[str] = None,
) -> OpenVikingError:
    """Convert a failed lark-oapi response into an OpenVikingError."""
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", None) or "Feishu API request failed"
    http_status = _response_http_status(response)
    details: dict[str, Any] = {
        "operation": operation,
        "feishu_code": code,
        "feishu_msg": msg,
        "http_status": http_status,
    }
    if resource:
        details["resource"] = resource
    if error_payload := _extract_error_payload(response):
        details["feishu_error"] = error_payload

    message = f"Feishu {operation} failed: code={code}, msg={msg}"

    if http_status == 401:
        exc: OpenVikingError = UnauthenticatedError(message)
    elif http_status == 403 or code == _FEISHU_DOCUMENT_FORBIDDEN or "forbidden" in msg.lower():
        exc = PermissionDeniedError(message, resource=resource)
    elif http_status == 404:
        exc = NotFoundError(resource or "document", resource_type="feishu document")
        exc.message = message
        exc.args = (message,)
    elif http_status == 429:
        exc = ResourceExhaustedError(message)
    elif http_status in {500, 502, 503, 504}:
        exc = UnavailableError("Feishu API", reason=message)
    else:
        exc = InvalidArgumentError(message)

    exc.details.update(details)
    return exc


def raise_from_lark_response(
    response: Any,
    *,
    operation: str,
    resource: Optional[str] = None,
) -> NoReturn:
    """Raise a typed OpenVikingError from a failed lark-oapi response."""
    exc = feishu_api_error_from_response(
        response,
        operation=operation,
        resource=resource,
    )
    logger.error(
        "[FeishuAPI] %s failed: code=%s msg=%s http=%s",
        operation,
        getattr(response, "code", None),
        getattr(response, "msg", None),
        _response_http_status(response),
    )
    raise exc
