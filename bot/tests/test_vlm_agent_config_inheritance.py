# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for resolving bot provider config from top-level VLM settings."""

import json
import sys
import contextlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vikingbot.config import loader as loader_module  # noqa: E402
from vikingbot.config.schema import Config  # noqa: E402


def _config_with_vlm(bot_data, vlm_data):
    config = Config.model_validate(bot_data)
    config.set_vlm_config_data(vlm_data)
    config.set_agent_config_data(bot_data.get("agents"))
    return config


def test_bot_provider_config_uses_vlm_settings_when_agents_unset():
    config = _config_with_vlm(
        {},
        {
            "model": "qwen-plus",
            "provider": "dashscope",
            "api_key": "sk-test",
            "forward_api_key": False,
            "api_base": "https://dashscope.example/v1",
            "temperature": 0.1,
            "thinking": False,
            "timeout": 77.0,
            "max_tokens": 8192,
            "max_retries": 5,
            "extra_headers": {"X-Test": "1"},
            "extra_request_body": {"seed": 7},
            "api_version": "2026-01-01",
            "stream": True,
        },
    )

    provider_config = config.get_bot_vlm_config()

    assert provider_config["model"] == "qwen-plus"
    assert provider_config["provider"] == "dashscope"
    assert provider_config["api_key"] == "sk-test"
    assert provider_config["forward_api_key"] is False
    assert provider_config["api_base"] == "https://dashscope.example/v1"
    assert provider_config["temperature"] == 0.1
    assert provider_config["thinking"] is False
    assert provider_config["timeout"] == 77.0
    assert provider_config["max_tokens"] == 8192
    assert provider_config["max_retries"] == 5
    assert provider_config["extra_headers"] == {"X-Test": "1"}
    assert provider_config["extra_request_body"] == {"seed": 7}
    assert provider_config["api_version"] == "2026-01-01"
    assert provider_config["stream"] is True
    assert config.agents.model == "openai/doubao-seed-2-0-pro-260215"
    assert config.agents.provider == ""


def test_bot_provider_config_agent_settings_override_vlm_settings():
    config = _config_with_vlm(
        {
            "agents": {
                "model": "bot-model",
                "temperature": 0.4,
                "max_tokens": 2048,
                "extra_request_body": {"bot": True},
            }
        },
        {
            "model": "vlm-model",
            "temperature": 0.1,
            "max_tokens": 8192,
            "extra_request_body": {"vlm": True},
            "provider": "dashscope",
            "api_key": "sk-test",
        },
    )

    provider_config = config.get_bot_vlm_config()

    assert provider_config["model"] == "bot-model"
    assert provider_config["temperature"] == 0.4
    assert provider_config["max_tokens"] == 2048
    assert provider_config["extra_request_body"] == {"bot": True}
    assert provider_config["provider"] == "dashscope"


def test_bot_provider_config_ignores_default_agent_provider_values():
    vlm_data = {
        "model": "qwen-plus",
        "provider": "dashscope",
        "api_key": "sk-test",
        "temperature": 0.1,
        "thinking": False,
        "max_tokens": 8192,
    }
    bot_data = {
        "agents": {
            "model": "openai/doubao-seed-2-0-pro-260215",
            "provider": "",
            "api_key": "",
            "api_base": "",
            "temperature": 0.7,
            "thinking": True,
            "extra_headers": {},
        }
    }

    config = _config_with_vlm(bot_data, vlm_data)
    provider_config = config.get_bot_vlm_config()

    assert provider_config["model"] == "qwen-plus"
    assert provider_config["provider"] == "dashscope"
    assert provider_config["temperature"] == 0.1
    assert provider_config["thinking"] is False
    assert provider_config["max_tokens"] == 8192


def test_bot_provider_config_allows_explicit_default_value_overrides():
    config = _config_with_vlm(
        {"agents": {"temperature": 0.7, "thinking": True}},
        {
            "model": "qwen-plus",
            "provider": "dashscope",
            "api_key": "sk-test",
            "temperature": 0.1,
            "thinking": False,
        },
    )

    provider_config = config.get_bot_vlm_config()

    assert provider_config["temperature"] == 0.7
    assert provider_config["thinking"] is True


def test_bot_provider_config_expands_nested_vlm_provider_settings():
    config = _config_with_vlm(
        {},
        {
            "model": "qwen-plus",
            "provider": "dashscope",
            "providers": {
                "dashscope": {
                    "api_key": "sk-from-provider",
                    "api_base": "https://dashscope.example/v1",
                }
            },
        },
    )

    provider_config = config.get_bot_vlm_config()

    assert provider_config["provider"] == "dashscope"
    assert provider_config["api_key"] == "sk-from-provider"
    assert provider_config["api_base"] == "https://dashscope.example/v1"


def test_bot_provider_config_resolves_default_provider():
    config = _config_with_vlm(
        {},
        {
            "model": "qwen-plus",
            "default_provider": "dashscope",
            "providers": {
                "dashscope": {
                    "api_key": "sk-from-default-provider",
                }
            },
        },
    )

    provider_config = config.get_bot_vlm_config()

    assert provider_config["provider"] == "dashscope"
    assert provider_config["api_key"] == "sk-from-default-provider"


def test_bot_provider_config_uses_vlm_defaults_without_explicit_provider():
    config = _config_with_vlm(
        {},
        {
            "model": "gpt-4o-mini",
            "api_key": "sk-openai",
        },
    )

    provider_config = config.get_bot_vlm_config()
    vlm_instance = config.get_bot_vlm_instance()

    assert provider_config["model"] == "gpt-4o-mini"
    assert provider_config["api_key"] == "sk-openai"
    assert "provider" not in provider_config
    assert vlm_instance.__class__.__name__ == "OpenAIVLM"


def test_bot_provider_config_does_not_inherit_implicit_vlm_behavior_defaults():
    config = _config_with_vlm(
        {},
        {
            "model": "qwen-plus",
            "provider": "dashscope",
            "api_key": "sk-test",
        },
    )

    provider_config = config.get_bot_vlm_config()
    vlm_instance = config.get_bot_vlm_instance()

    assert "temperature" not in provider_config
    assert "thinking" not in provider_config
    assert "timeout" not in provider_config
    assert "max_retries" not in provider_config
    assert "stream" not in provider_config
    assert vlm_instance.temperature == 0.7
    assert vlm_instance.thinking is True


def test_make_provider_uses_vlm_defaults_without_explicit_provider(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "prompt_toolkit",
        SimpleNamespace(PromptSession=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "prompt_toolkit.formatted_text",
        SimpleNamespace(HTML=lambda value: value),
    )
    monkeypatch.setitem(
        sys.modules,
        "prompt_toolkit.history",
        SimpleNamespace(FileHistory=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "prompt_toolkit.patch_stdout",
        SimpleNamespace(patch_stdout=lambda: contextlib.nullcontext()),
    )

    from vikingbot.cli.commands import _make_provider

    config = _config_with_vlm(
        {},
        {
            "model": "gpt-4o-mini",
            "api_key": "sk-openai",
        },
    )

    provider = _make_provider(config)

    assert provider.__class__.__name__ == "VLMProviderAdapter"
    assert provider._vlm.__class__.__name__ == "OpenAIVLM"
    assert provider._vlm.model == "gpt-4o-mini"
    assert provider._vlm.api_key == "sk-openai"


def test_direct_config_does_not_read_global_vlm(monkeypatch):
    def fail_get_openviking_config():
        raise AssertionError("global OpenViking config should not be read")

    monkeypatch.setattr(
        "openviking_cli.utils.config.get_openviking_config",
        fail_get_openviking_config,
    )

    config = Config.model_validate({"agents": {"model": "bot-model"}})

    assert config.get_bot_vlm_config() == {"model": "bot-model"}
    assert config.get_bot_vlm_instance() is None


def test_bot_vlm_instance_preserves_multi_credential_failover(monkeypatch):
    captured = []

    class FakeVLM:
        def __init__(self, config):
            self.config = config
            self.model = config["model"]
            self.provider = config["provider"]
            self.thinking = config["thinking"]

    def fake_create(config):
        captured.append(config)
        return FakeVLM(config)

    monkeypatch.setattr("openviking.models.vlm.VLMFactory.create", fake_create)
    config = _config_with_vlm(
        {},
        {
            "model": "qwen-plus",
            "provider": "dashscope",
            "thinking": True,
            "credentials": [
                {"id": "primary", "provider": "dashscope", "api_key": "sk-primary"},
                {"id": "backup", "provider": "dashscope", "api_key": "sk-backup"},
            ],
        },
    )

    vlm_instance = config.get_bot_vlm_instance()

    assert vlm_instance.__class__.__name__ == "MultiCredentialVLM"
    assert vlm_instance.thinking is True
    assert [item["api_key"] for item in captured] == ["sk-primary", "sk-backup"]


def test_legacy_vlm_merge_hook_does_not_create_agents_section():
    bot_data = {}

    loader_module._merge_vlm_model_config(bot_data, {"model": "qwen-plus"})

    assert "agents" not in bot_data


def test_load_config_keeps_existing_ov_conf_vlm_as_provider_source(tmp_path, monkeypatch):
    config_path = tmp_path / "ov.conf"
    config_path.write_text(
        json.dumps(
            {
                "vlm": {
                    "model": "qwen-plus",
                    "provider": "dashscope",
                    "apiKey": "sk-test",
                    "temperature": 0.1,
                    "thinking": False,
                    "maxTokens": 8192,
                },
                "bot": {
                    "agents": {
                        "model": "openai/doubao-seed-2-0-pro-260215",
                        "provider": "",
                        "apiKey": "",
                        "apiBase": "",
                        "temperature": 0.7,
                        "thinking": True,
                        "extraHeaders": {},
                    }
                },
            }
        )
    )
    monkeypatch.setattr(loader_module, "CONFIG_PATH", config_path)

    config = loader_module.load_config()
    provider_config = config.get_bot_vlm_config()

    assert provider_config["model"] == "qwen-plus"
    assert provider_config["provider"] == "dashscope"
    assert provider_config["api_key"] == "sk-test"
    assert provider_config["temperature"] == 0.1
    assert provider_config["thinking"] is False
    assert provider_config["max_tokens"] == 8192
