# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Client for the external Connector service (knowledge-base doc/add pipeline)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from openviking_cli.utils import get_logger

logger = get_logger(__name__)


class ConnectorClient:
    """Wraps the Connector service's doc/add and task/info APIs."""

    def __init__(self, doc_add_url: str, task_info_url: str, account_id: str = "") -> None:
        self._doc_add_url = doc_add_url
        self._task_info_url = task_info_url
        self._headers = {"V-Account-Id": account_id} if account_id else {}

    async def submit_doc_add(
        self,
        resource_id: str,
        add_type: str,
        api_key: str,
        *,
        tos_path: Optional[str] = None,
        path_prefix: Optional[List[str]] = None,
        include_child: bool = True,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Submit a document import job via the configured doc/add endpoint.

        Returns the Connector response dict (contains task key / id on success).
        """
        payload: Dict[str, Any] = {
            "resource_id": resource_id,
            "add_type": add_type,
            "backend": "ov",
            "api_key": api_key,
            "include_child": include_child,
        }
        if tos_path is not None:
            payload["tos_path"] = tos_path
        if path_prefix is not None:
            payload["path_prefix"] = path_prefix
        if extra_params:
            payload.update(extra_params)

        async with httpx.AsyncClient(timeout=30.0) as client:
            rsp = await client.post(self._doc_add_url, json=payload, headers=self._headers)
        rsp.raise_for_status()
        return rsp.json()

    async def get_task_info(self, task_key: str) -> Dict[str, Any]:
        """Query task status via the configured task/info endpoint.

        Connector task statuses: pending / running / succeeded / failed / cancelled.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            rsp = await client.post(
                self._task_info_url,
                json={"TaskKey": task_key},
                headers=self._headers,
            )
        rsp.raise_for_status()
        return rsp.json()
