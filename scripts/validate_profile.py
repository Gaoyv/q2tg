#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", nargs="?", type=Path, default=Path("deploy/profile"))
    args = parser.parse_args()
    profile = args.profile.resolve()

    core = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    telegram = yaml.safe_load((profile / "blueset.telegram" / "config.yaml").read_text(encoding="utf-8"))
    qq = yaml.safe_load((profile / "milkice.qq" / "config.yaml").read_text(encoding="utf-8"))
    assert core["master_channel"] == "blueset.telegram"
    assert "milkice.qq" in core["slave_channels"]
    assert telegram.get("token") and telegram.get("admins")
    assert qq["NapCat"]["endpoint"].startswith(("ws://", "wss://"))
    assert len(str(qq["NapCat"]["access_token"])) >= 32

    db = sqlite3.connect(profile / "blueset.telegram" / "tgdata.db")
    try:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"chatassoc", "msglog", "slavechatinfo"} <= tables
        counts = {
            table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("chatassoc", "msglog", "slavechatinfo")
        }
    finally:
        db.close()
    print(f"Profile valid: {profile}; rows={counts}")


if __name__ == "__main__":
    main()

