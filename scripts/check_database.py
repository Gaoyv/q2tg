#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database)
    try:
        print("quick_check=" + connection.execute("PRAGMA quick_check").fetchone()[0])
        counts = [
            connection.execute(f'SELECT COUNT(1) FROM "{table}"').fetchone()[0]
            for table in ("chatassoc", "msglog", "slavechatinfo")
        ]
        print(f"chatassoc={counts[0]} msglog={counts[1]} slavechatinfo={counts[2]}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()

