#!/bin/bash
# Daily VM 1 backup: full Hermes archive (keep 3) + small critical-state archive (keep 7).
set -euo pipefail
export HERMES_HOME=/data/hermes PATH="$HOME/.local/bin:$PATH"
umask 077
hermes backup -o /data/backups/hermes/ -k 3 >/dev/null
D=$(date -u +%Y%m%d-%H%M); T=$(mktemp -d)
for db in state.db kanban.db mcp/task_history.db; do
  [ -f /data/hermes/$db ] && mkdir -p "$T/$(dirname $db)" && python3 -c "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d)" /data/hermes/$db "$T/$db"
done
mkdir -p "$T/pa" && python3 -c "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d)" /data/pa/pa.db "$T/pa/pa.db"
cp /data/pa/token /data/pa/worker.json "$T/pa/" 2>/dev/null || true
tar -czf /data/backups/critical/vm1-critical-$D.tgz -C /data/hermes config.yaml .env SOUL.md memories skills/research mcp/dsh_mcp.py mcp/pa_mcp.py mcp/dsh_token -C /data/pa service/app.py -C "$T" .
rm -rf "$T"
ls -1t /data/backups/critical/vm1-critical-*.tgz | tail -n +8 | xargs -r rm -f
echo "ok $D"
