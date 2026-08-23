from pathlib import Path

import pytest

from efb_qq_napcat.config import NapCatConfig, load_config


def test_new_config() -> None:
    config = NapCatConfig.from_mapping(
        {"NapCat": {"endpoint": "ws://napcat:3001/", "access_token": "secret", "api_timeout": 12}}
    )
    assert config.endpoint == "ws://napcat:3001"
    assert config.access_token == "secret"
    assert config.api_timeout == 12
    assert config.send_timeout == 180


def test_legacy_gocqhttp_config_preserves_token() -> None:
    config = NapCatConfig.from_mapping(
        {"Client": "GoCQHttp", "GoCQHttp": {"api_root": "http://127.0.0.1:5700", "access_token": "old"}}
    )
    assert config.endpoint == "ws://127.0.0.1:3001"
    assert config.access_token == "old"


def test_load_missing_file_uses_safe_local_default(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.yaml").endpoint == "ws://127.0.0.1:3001"


def test_rejects_http_endpoint() -> None:
    with pytest.raises(ValueError):
        NapCatConfig.from_mapping({"NapCat": {"endpoint": "http://napcat:3000"}})
