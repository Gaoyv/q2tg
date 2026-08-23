from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class NapCatConfig:
    endpoint: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    api_timeout: float = 60.0
    connect_timeout: float = 20.0
    reconnect_delay: float = 5.0
    max_media_bytes: int = 50 * 1024 * 1024
    download_timeout: float = 60.0
    send_timeout: float = 180.0
    qrcode_path: str = "/napcat-cache/qrcode.png"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "NapCatConfig":
        data: Mapping[str, Any] = raw or {}
        # Accept the old EQS/GoCQHttp profile so an existing installation can be
        # migrated without exposing or re-entering its OneBot access token.
        if "NapCat" in data:
            section = data.get("NapCat") or {}
        elif "GoCQHttp" in data:
            old = data.get("GoCQHttp") or {}
            section = {
                "endpoint": "ws://127.0.0.1:3001",
                "access_token": old.get("access_token", ""),
                "api_timeout": old.get("api_timeout", 60),
            }
        else:
            section = data

        endpoint = str(section.get("endpoint", cls.endpoint)).rstrip("/")
        if not endpoint.startswith(("ws://", "wss://")):
            raise ValueError("NapCat.endpoint must use ws:// or wss://")

        return cls(
            endpoint=endpoint,
            access_token=str(section.get("access_token", "") or ""),
            api_timeout=float(section.get("api_timeout", cls.api_timeout)),
            connect_timeout=float(section.get("connect_timeout", cls.connect_timeout)),
            reconnect_delay=float(section.get("reconnect_delay", cls.reconnect_delay)),
            max_media_bytes=int(section.get("max_media_bytes", cls.max_media_bytes)),
            download_timeout=float(section.get("download_timeout", cls.download_timeout)),
            send_timeout=float(section.get("send_timeout", cls.send_timeout)),
            qrcode_path=str(section.get("qrcode_path", cls.qrcode_path)),
        )


def load_config(path: Path) -> NapCatConfig:
    if not path.exists():
        return NapCatConfig()
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("QQ channel config must be a YAML mapping")
    return NapCatConfig.from_mapping(raw)
