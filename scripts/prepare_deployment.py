#!/usr/bin/env python3
"""Create a consistent, secret-bearing runtime profile from the old EFB data."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

import yaml


def sqlite_backup(source: Path, destination: Path) -> None:
    source_db = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        check = source_db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"source Telegram database failed quick_check: {check}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_db = sqlite3.connect(destination)
        try:
            source_db.backup(destination_db)
            destination_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            destination_db.close()
    finally:
        source_db.close()


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("default"))
    parser.add_argument("--deploy-dir", type=Path, default=Path("deploy"))
    args = parser.parse_args()

    source = args.source.resolve()
    deploy = args.deploy_dir.resolve()
    source_db = source / "blueset.telegram" / "tgdata.db"
    required = [source / "config.yaml", source / "blueset.telegram" / "config.yaml", source_db]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("Missing source profile files: " + ", ".join(missing))

    runtime_profile = deploy / "profile"
    telegram_dir = runtime_profile / "blueset.telegram"
    qq_dir = runtime_profile / "milkice.qq"
    telegram_dir.mkdir(parents=True, exist_ok=True)
    qq_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(source / "config.yaml", runtime_profile / "config.yaml")
    shutil.copy2(source / "blueset.telegram" / "config.yaml", telegram_dir / "config.yaml")
    sqlite_backup(source_db, telegram_dir / "tgdata.db")

    token_file = deploy / ".env"
    existing: dict[str, str] = {}
    if token_file.exists():
        for line in token_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                existing[key] = value
    onebot_token = existing.get("ONEBOT_TOKEN") or secrets.token_urlsafe(32)
    webui_token = existing.get("WEBUI_TOKEN") or secrets.token_urlsafe(24)

    channel_config = {
        "NapCat": {
            "endpoint": "ws://napcat:3001",
            "access_token": onebot_token,
            "api_timeout": 60,
            "connect_timeout": 20,
            "reconnect_delay": 5,
            "max_media_bytes": 50 * 1024 * 1024,
            "download_timeout": 60,
        }
    }
    write_private(qq_dir / "config.yaml", yaml.safe_dump(channel_config, allow_unicode=True, sort_keys=False))

    napcat_config = deploy / "napcat" / "config"
    onebot = {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [
                {
                    "enable": True,
                    "name": "efb",
                    "host": "0.0.0.0",
                    "port": 3001,
                    "reportSelfMessage": False,
                    "enableForcePushEvent": True,
                    "messagePostFormat": "array",
                    "token": onebot_token,
                    "debug": False,
                    "heartInterval": 30000,
                }
            ],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": True,
        "imageDownloadProxy": "",
        "timeout": {
            "baseTimeout": 10000,
            "uploadSpeedKBps": 256,
            "downloadSpeedKBps": 256,
            "maxTimeout": 1800000,
        },
    }
    webui = {"host": "0.0.0.0", "port": 6099, "token": webui_token, "loginRate": 3}
    write_private(napcat_config / "onebot11.json", json.dumps(onebot, ensure_ascii=False, indent=2) + "\n")
    write_private(napcat_config / "webui.json", json.dumps(webui, ensure_ascii=False, indent=2) + "\n")
    (deploy / "ntqq").mkdir(parents=True, exist_ok=True)
    write_private(
        token_file,
        f"NAPCAT_UID=1000\nNAPCAT_GID=1000\nONEBOT_TOKEN={onebot_token}\nWEBUI_TOKEN={webui_token}\n",
    )

    check_db = sqlite3.connect(telegram_dir / "tgdata.db")
    try:
        counts = {
            table: check_db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("chatassoc", "msglog", "slavechatinfo")
        }
        check = check_db.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check_db.close()
    print(f"Prepared {runtime_profile}")
    print(f"Database quick_check={check}; rows={counts}")


if __name__ == "__main__":
    main()

