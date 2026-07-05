# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Map Feishu/Lark OpenAPI failures to OpenViking typed exceptions.

Explicit error-code tables currently cover docx/wiki document APIs. Other
Feishu APIs fall back to HTTP status based mapping.
"""

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

# Docx / wiki document APIs — see Feishu docx error tables.
_DOCUMENT_FORBIDDEN_CODES = frozenset({1770032})
_DOCUMENT_NOT_FOUND_CODES = frozenset({1770002})
_DOCUMENT_INVALID_ARGUMENT_CODES = frozenset(
    {
        1770001,
        1770003,
        1770004,
        1770005,
        1770006,
        1770007,
        1770008,
        1770010,
        1770011,
        1770012,
        1770013,
        1770014,
        1770015,
        1770019,
        1770020,
        1770021,
        1770022,
        1770024,
        1770025,
        1770026,
        1770027,
        1770028,
        1770029,
        1770030,
        1770031,
        1770033,
        1770034,
    }
)
_DOCUMENT_UNAVAILABLE_CODES = frozenset({1771001, 1771002, 1771003, 1771004, 1771005, 1771006})
_RATE_LIMIT_CODES = frozenset({99991400})
_UNAUTHENTICATED_CODES = frozenset({99991631, 99991668, 99991669})

_HINT_DOC_FORBIDDEN_TENANT = (
    "Feishu error 1770032 (forbidden): grant the required document scopes and "
    "make sure the app can access this document."
)
_HINT_DOC_FORBIDDEN_USER = (
    "Feishu error 1770032 (forbidden): the user token cannot read this document. "
    "Share the document with that user, or import with a user OAuth token that has access."
)
_HINT_DOC_NOT_FOUND = (
    "Feishu error 1770002 (not found): the document_id or wiki token does not exist "
    "or was deleted."
)


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


def _permission_violation_hint(error_payload: dict[str, Any]) -> Optional[str]:
    violations = error_payload.get("permission_violations")
    if not isinstance(violations, list) or not violations:
        return None
    scopes: list[str] = []
    for item in violations:
        if isinstance(item, dict):
            scope = item.get("scope")
            if scope:
                scopes.append(str(scope))
    if not scopes:
        return None
    joined = ", ".join(scopes)
    return f"Missing Feishu API scopes: {joined}. Open the scope URL from error.details.permission_violations."


def _document_forbidden_hint(*, using_user_token: bool) -> str:
    return _HINT_DOC_FORBIDDEN_USER if using_user_token else _HINT_DOC_FORBIDDEN_TENANT


def _with_message(exc: OpenVikingError, message: str) -> OpenVikingError:
    exc.message = message
    return exc


def build_feishu_error_details(
    *,
    response: Any,
    operation: str,
    resource: Optional[str] = None,
    using_user_token: bool = False,
) -> dict[str, Any]:
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", None)
    http_status = _response_http_status(response)
    error_payload = _extract_error_payload(response)
    hint = _permission_violation_hint(error_payload)

    if hint is None and code in _DOCUMENT_FORBIDDEN_CODES:
        hint = _document_forbidden_hint(using_user_token=using_user_token)
    elif hint is None and code in _DOCUMENT_NOT_FOUND_CODES:
        hint = _HINT_DOC_NOT_FOUND

    details: dict[str, Any] = {
        "operation": operation,
        "feishu_code": code,
        "feishu_msg": msg,
        "http_status": http_status,
    }
    if resource:
        details["resource"] = resource
    if hint:
        details["hint"] = hint
    if error_payload.get("permission_violations"):
        details["permission_violations"] = error_payload["permission_violations"]
    if error_payload.get("troubleshooter"):
        details["troubleshooter"] = error_payload["troubleshooter"]
    if error_payload.get("logid"):
        details["logid"] = error_payload["logid"]
    return details


def feishu_api_error_from_response(
    response: Any,
    *,
    operation: str,
    resource: Optional[str] = None,
    using_user_token: bool = False,
) -> OpenVikingError:
    """Convert a failed lark-oapi response into an OpenVikingError."""
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", None) or "Feishu API request failed"
    http_status = _response_http_status(response)
    details = build_feishu_error_details(
        response=response,
        operation=operation,
        resource=resource,
        using_user_token=using_user_token,
    )
    hint = details.get("hint")
    message = f"Feishu {operation} failed: code={code}, msg={msg}"
    if hint:
        message = f"{message}. {hint}"

    if code in _UNAUTHENTICATED_CODES or http_status == 401:
        exc: OpenVikingError = UnauthenticatedError(message)
    elif code in _DOCUMENT_FORBIDDEN_CODES or http_status == 403:
        exc = PermissionDeniedError(message, resource=resource)
    elif code in _DOCUMENT_NOT_FOUND_CODES or http_status == 404:
        exc = _with_message(
            NotFoundError(resource or "document", resource_type="feishu document"),
            message,
        )
    elif code in _RATE_LIMIT_CODES or http_status == 429:
        exc = ResourceExhaustedError(message, details={"operation": operation})
    elif code in _DOCUMENT_UNAVAILABLE_CODES or http_status in {500, 502, 503, 504}:
        exc = UnavailableError("Feishu API", reason=message)
    elif code in _DOCUMENT_INVALID_ARGUMENT_CODES or http_status == 400:
        exc = InvalidArgumentError(message, details={"operation": operation})
    else:
        exc = InvalidArgumentError(
            message,
            details={"operation": operation, "feishu_code": code},
        )

    if exc.details is not None:
        exc.details.update(details)
    else:
        exc.details = dict(details)
    return exc


def raise_from_lark_response(
    response: Any,
    *,
    operation: str,
    resource: Optional[str] = None,
    using_user_token: bool = False,
) -> NoReturn:
    """Raise a typed OpenVikingError from a failed lark-oapi response."""
    exc = feishu_api_error_from_response(
        response,
        operation=operation,
        resource=resource,
        using_user_token=using_user_token,
    )
    logger.error(
        "[FeishuAPI] %s failed: code=%s msg=%s http=%s",
        operation,
        getattr(response, "code", None),
        getattr(response, "msg", None),
        _response_http_status(response),
    )
    raise exc
