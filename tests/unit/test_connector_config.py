# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Unit tests for Connector configuration validation."""

import pytest

from openviking_cli.utils.config.open_viking_config import ConnectorConfig, OpenVikingConfig


def test_connector_config_defaults_to_disabled_tos_only():
    config = ConnectorConfig()

    assert config.enable is False
    assert config.connector == ""
    assert config.tracker == ""
    assert config.timeout_seconds == 3600
    assert config.poll_interval_ms == 5000
    assert config.allowed_add_types == ["tos"]


def test_openviking_config_loads_connector_section_from_dict():
    config = OpenVikingConfig.from_dict(
        {
            "connector": {
                "enable": True,
                "connector": "https://connector.example/doc/add",
                "tracker": "https://connector.example/task/info",
                "main_account_id": "main-account",
                "timeout_seconds": 120,
                "poll_interval_ms": 250,
                "allowed_add_types": ["tos", "web"],
            }
        }
    )

    assert config.connector.enable is True
    assert config.connector.connector == "https://connector.example/doc/add"
    assert config.connector.tracker == "https://connector.example/task/info"
    assert config.connector.main_account_id == "main-account"
    assert config.connector.timeout_seconds == 120
    assert config.connector.poll_interval_ms == 250
    assert config.connector.allowed_add_types == ["tos", "web"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {
                "enable": True,
                "connector": "",
                "tracker": "https://connector.example/task/info",
            },
            "connector.connector is required",
        ),
        (
            {
                "enable": True,
                "connector": "https://connector.example/doc/add",
                "tracker": "",
            },
            "connector.tracker is required",
        ),
        (
            {
                "enable": True,
                "connector": "connector.example/doc/add",
                "tracker": "https://connector.example/task/info",
            },
            "connector.connector must be a full endpoint URL",
        ),
        (
            {
                "connector": "https://connector.example/doc/add",
                "tracker": "https://connector.example/task/info",
                "timeout_seconds": 0,
            },
            "connector.timeout_seconds must be > 0",
        ),
        (
            {
                "connector": "https://connector.example/doc/add",
                "tracker": "https://connector.example/task/info",
                "poll_interval_ms": 0,
            },
            "connector.poll_interval_ms must be > 0",
        ),
    ],
)
def test_connector_config_rejects_invalid_shapes(kwargs, match):
    with pytest.raises(ValueError, match=match):
        ConnectorConfig(**kwargs)
