"""Tests for the configuration loader."""

from pathlib import Path

import pytest
import yaml

from cortiva.core.config import build_fabric, load_config


def _write_config(tmp_path: Path, config: dict) -> Path:
    """Helper: write a config dict to a YAML file and return its path."""
    p = tmp_path / "cortiva.yaml"
    p.write_text(yaml.dump(config, default_flow_style=False))
    return p


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path: Path) -> None:
        cfg = {
            "fabric": {"name": "test-org", "heartbeat_interval": 10},
            "memory": {"adapter": "inmemory", "config": {}},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": "./agents"},
        }
        path = _write_config(tmp_path, cfg)
        result = load_config(path)
        assert result["fabric"]["name"] == "test-org"
        assert result["memory"]["adapter"] == "inmemory"

    def test_applies_defaults_for_missing_sections(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"fabric": {"name": "minimal"}})
        result = load_config(path)
        # Should fill in defaults
        assert result["memory"]["adapter"] == "inmemory"
        assert result["consciousness"]["provider"] == "anthropic"
        assert result["agents"]["directory"] == "./agents"
        assert result["fabric"]["heartbeat_interval"] == 30

    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Config not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_raises_on_invalid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "cortiva.yaml"
        path.write_text("just a string, not a mapping")
        with pytest.raises(ValueError, match="expected a YAML mapping"):
            load_config(path)


class TestBuildFabric:
    def test_builds_with_inmemory_defaults(self, tmp_path: Path) -> None:
        cfg = {
            "fabric": {"name": "test", "heartbeat_interval": 5},
            "memory": {"adapter": "inmemory", "config": {}},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }
        fabric = build_fabric(cfg)
        assert fabric.heartbeat_interval == 5.0
        from cortiva.adapters.memory.inmemory import InMemoryAdapter

        assert isinstance(fabric.memory, InMemoryAdapter)

    def test_unknown_memory_adapter_raises(self, tmp_path: Path) -> None:
        cfg = {
            "memory": {"adapter": "nosuchdb"},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }
        with pytest.raises(ValueError, match="Unknown memory adapter"):
            build_fabric(cfg)

    def test_unknown_consciousness_adapter_raises(self, tmp_path: Path) -> None:
        cfg = {
            "memory": {"adapter": "inmemory", "config": {}},
            "consciousness": {"provider": "nosuchllm"},
            "agents": {"directory": str(tmp_path / "agents")},
        }
        with pytest.raises(ValueError, match="Unknown consciousness adapter"):
            build_fabric(cfg)

    def test_channel_is_optional(self, tmp_path: Path) -> None:
        cfg = {
            "memory": {"adapter": "inmemory", "config": {}},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }
        fabric = build_fabric(cfg)
        assert fabric.channel is None

    def test_daily_limit_from_config(self, tmp_path: Path) -> None:
        cfg = {
            "memory": {"adapter": "inmemory", "config": {}},
            "consciousness": {
                "provider": "anthropic",
                "budget": {"daily_limit": 500},
            },
            "agents": {"directory": str(tmp_path / "agents")},
        }
        fabric = build_fabric(cfg)
        assert fabric.daily_consciousness_limit == 500

    def test_terminal_adapter_from_config(self, tmp_path: Path) -> None:
        cfg = {
            "memory": {"adapter": "inmemory", "config": {}},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
            "terminal": {"adapter": "claude-code", "config": {}},
        }
        fabric = build_fabric(cfg)
        from cortiva.adapters.terminal.claude_code import ClaudeCodeAdapter

        assert isinstance(fabric.terminal, ClaudeCodeAdapter)

    def test_terminal_adapter_optional(self, tmp_path: Path) -> None:
        cfg = {
            "memory": {"adapter": "inmemory", "config": {}},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }
        fabric = build_fabric(cfg)
        assert fabric.terminal is None
