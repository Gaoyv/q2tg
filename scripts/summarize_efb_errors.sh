#!/bin/sh
# Keep diagnostics on the VPS and emit exception/permission lines only. This
# avoids copying Telegram tokens or chat text that may appear in general logs.
docker logs --tail 300 q2tg-efb 2>&1 \
  | grep -E 'Traceback|Error|Exception|Permission denied|No such file|ModuleNotFound|ImportError|TypeError|ValueError|AssertionError|OperationalError|DatabaseError|peewee' \
  | tail -n 80

