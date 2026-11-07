# 7. Backups and reboots

## Daily backups

Systemd timers with `Persistent=true`, so a backup missed while the VM was paused runs at the next wake.

- **VM 1**: copy `vm1/hermes-backup.sh` to `~/bin/` and the `.service`/`.timer` to `~/.config/systemd/user/`, then
  `systemctl --user enable --now hermes-backup.timer`. It writes a full `hermes backup` (keeps 3) and a small
  critical archive (config, .env, SOUL.md, memories, MCP code, tokens, SQLite copies of the Hermes and assistant
  databases; keeps 7) to `/data/backups/`.
- **VM 2**: install `vm2/vm2-backup.sh` to `/usr/local/sbin/` (root, 0700) and the units to `/etc/systemd/system/`,
  then `sudo systemctl enable --now vm2-backup.timer`. Root-only archive of the bridge and freellmapi databases,
  `/etc/dsh`, sudoers, firewall, unit files and the freellmapi config.
- **Laptop**: `laptop/wake-vms.ps1` wakes both VMs (VM 2 first, so VM 1's bridge check finds it awake), checks
  every service, and copies the newest archive from each VM plus a D1 export of the reminders to
  `%USERPROFILE%\hermes-backups\`. Keep that folder out of any git repo: the archives contain keys.

SQLite files are copied with SQLite's backup API, not `cp`, so copies taken while services run are consistent.

**Check:** extract the newest archive to a temp folder and run `PRAGMA integrity_check` on the databases inside.

## Reboots

Test it once: `sudo systemctl reboot` on each VM. In this build both came back in about 20 seconds, with
Tailscale, the firewall, Docker (freellmapi on both ports), Ollama, the bridge, the assistant service and the
Hermes gateway all up and Telegram reconnected. What made that work: enabled units, `loginctl enable-linger` for
user services, and Docker waiting for Tailscale.
