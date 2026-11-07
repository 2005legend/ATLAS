#!/bin/bash
# Daily VM 2 critical-state backup (root-only; contains secrets): keep 7.
set -euo pipefail
umask 077
D=$(date -u +%Y%m%d-%H%M); T=$(mktemp -d)
copydb() { python3 -c "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d)" "$1" "$2"; }
mkdir -p "$T/db"; copydb /data/dsh/bridge/tasks.db "$T/db/bridge-tasks.db"; copydb /data/freellmapi/data/freeapi.db "$T/db/freellmapi.db"
[ -f /data/freellmapi/data/.encryption-key ] && cp /data/freellmapi/data/.encryption-key "$T/db/"
tar -czf /data/backups/vm2-critical-$D.tgz -C "$T" db -C / etc/dsh etc/sudoers.d/dsh-bridge etc/nftables.conf etc/docker/daemon.json \
  etc/systemd/system/dsh-bridge.service etc/systemd/system/docker.service.d etc/systemd/system/ollama.service.d usr/local/sbin/dsh-ctl \
  data/dsh/bridge/app.py data/dsh/bridge/token data/freellmapi/src/.env data/freellmapi/src/compose.override.yml
rm -rf "$T"
ls -1t /data/backups/vm2-critical-*.tgz | tail -n +8 | xargs -r rm -f
echo "ok $D"
