# 10. Setup commands, in order

Every terminal command for a fresh install, in the order to run them. Each block says where it runs:
**laptop** (Windows PowerShell), **VM 1** (the assistant) or **VM 2** (the coding worker). Steps marked
**(you)** need a person: a browser sign-in, a token to paste, or an approval. Docs 2–7 explain why each step
exists; this page is the short version.

Assumed: two Debian 13 VMs with `sudo` and a persistent `/data` disk, Python 3.11+ and Node 22+ on them, and
Python 3.11+, Node 20+ and Git on the laptop. If a VM lacks `uv`, install it with
`curl -LsSf https://astral.sh/uv/install.sh | sh`.

## 0. Tailscale on all three machines

**VM 1 and VM 2:**

```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update && sudo apt-get install -y tailscale nftables git python3-pytest zstd
sudo tailscale up          # (you) open the printed link and sign in; don't press Ctrl+C before that
tailscale ip -4            # note this VM's 100.x.y.z address
```

**Laptop:**

```powershell
winget install Tailscale.Tailscale     # (you) sign in from the tray icon, same account
tailscale ip -4
```

Check: `tailscale status` on any machine lists all three.

## 1. Fill in your values (each machine)

Clone the repo on all three machines and write your values into every config file once.

**VM 1 and VM 2:**

```bash
cd ~ && git clone https://github.com/2005legend/ATLAS.git && cd ATLAS
cat > my.env <<'EOF'
VM1_TAILSCALE_IP=100.x.x.x
VM2_TAILSCALE_IP=100.x.x.x
LAPTOP_TAILSCALE_IP=100.x.x.x
YOUR_SUBDOMAIN=fill-in-after-step-6
YOUR_D1_DATABASE_ID=fill-in-after-step-6
EOF
nano my.env                                                   # (you) put in your three IPs now
# replaces each placeholder in the repo files; values still "fill-in…" are skipped, so you can run it again later
fill() { set -a; . ./my.env; set +a
  for v in VM1_TAILSCALE_IP VM2_TAILSCALE_IP LAPTOP_TAILSCALE_IP YOUR_SUBDOMAIN YOUR_D1_DATABASE_ID; do
    val="${!v}"; case "$val" in ""|fill-in*) continue;; esac
    grep -rlI --exclude-dir=.git --exclude=my.env "$v" . | xargs -r sed -i "s/$v/$val/g"
  done; }
fill && echo "filled"
```

**Laptop** (Git Bash, so the same `fill` function works): run the same commands in `C:\Users\<you>\ATLAS`.
After step 6, put the subdomain and D1 database ID into `my.env` on the laptop and VM 1 and run `fill` again.

## 2. Laptop: fallback model and firewall

**Laptop** (PowerShell; the last two lines in an Administrator PowerShell):

```powershell
ollama pull qwen3.5:4b
"FROM qwen3.5:4b`nPARAMETER num_ctx 65536" | Set-Content -Encoding ascii qwen-64k.Modelfile
ollama create qwen3.5-4b-64k -f qwen-64k.Modelfile
[Environment]::SetEnvironmentVariable('OLLAMA_HOST','0.0.0.0:11434','User')
New-NetFirewallRule -DisplayName "Ollama via Tailscale only" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Any
```

Quit Ollama fully (tray icon, then check Task Manager for leftover `ollama` processes) and start it again.

Check, from VM 1: `curl -s http://LAPTOP_TAILSCALE_IP:11434/v1/models` lists your models.

## 3. VM 2: firewall, Ollama, Docker

**VM 2**, in `~/ATLAS/vm2`:

```bash
cd ~/ATLAS/vm2
sudo cp nftables.conf /etc/nftables.conf
sudo nft -c -f /etc/nftables.conf && sudo nft -f /etc/nftables.conf && sudo systemctl enable nftables

curl -fsSL https://ollama.com/install.sh | sh
sudo mkdir -p /data/ollama/models && sudo chown -R ollama:ollama /data/ollama
sudo mkdir -p /etc/systemd/system/ollama.service.d && sudo cp ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
ollama pull qwen3.5:4b
printf 'FROM qwen3.5:4b\nPARAMETER num_ctx 65536\n' > /tmp/m && ollama create qwen3.5-4b-64k -f /tmp/m

command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh     # install Docker if missing
sudo cp docker-daemon.json /etc/docker/daemon.json
sudo mkdir -p /etc/systemd/system/docker.service.d && sudo cp docker-wait-tailscale.conf /etc/systemd/system/docker.service.d/
sudo systemctl daemon-reload && sudo systemctl restart docker
```

Check: `curl -s 127.0.0.1:11434/api/version`.

## 4. VM 2: freellmapi

```bash
sudo mkdir -p /data/freellmapi && sudo chown "$USER" /data/freellmapi
git clone https://github.com/tashfeenahmed/freellmapi /data/freellmapi/src && cd /data/freellmapi/src
git checkout "$(git tag --sort=-creatordate | head -1)"
(umask 077; printf 'ENCRYPTION_KEY=%s\nPORT=3001\n' "$(openssl rand -hex 32)" > .env)
cp ~/ATLAS/vm2/freellmapi-compose.override.yml compose.override.yml
nano compose.override.yml       # not on InstaBox? delete the two instabox-proxy.crt lines
sudo docker compose build && sudo docker compose up -d
sudo docker compose logs | grep -i 'setup code'
```

**(you)** On the laptop open `http://VM2_TAILSCALE_IP:3001`, create the admin account with the setup code, add one
API key per provider, and create a unified key (`freellmapi-…`).

Check: `curl -s 127.0.0.1:3001/api/ping` returns `"status":"ok"`.

## 5. VM 2: DSH sandbox and delegation bridge

```bash
cd ~/ATLAS/vm2
sudo npm install -g @deepseek-ai/dsh@latest
sudo useradd --system --home-dir /data/dsh/home --shell /usr/sbin/nologin dsh-runner
sudo useradd --system --home-dir /data/dsh/bridge --shell /usr/sbin/nologin dsh-bridge
sudo mkdir -p /data/dsh/{workspaces,homes,inbox,results,logs,bridge} /etc/dsh
sudo chown root:root /data/dsh/workspaces /data/dsh/homes && sudo chmod 0711 /data/dsh/workspaces /data/dsh/homes
sudo chown -R dsh-bridge:dsh-bridge /data/dsh/{inbox,results,logs,bridge} && sudo chmod 0700 /data/dsh/{inbox,results,logs,bridge}
sudo install -m 0644 cordis.patch.yml /etc/dsh/cordis.patch.yml
sudo install -m 0755 dsh-ctl /usr/local/sbin/dsh-ctl
echo 'dsh-bridge ALL=(root) NOPASSWD: /usr/local/sbin/dsh-ctl' | sudo tee /etc/sudoers.d/dsh-bridge >/dev/null
sudo chmod 0440 /etc/sudoers.d/dsh-bridge && sudo visudo -cf /etc/sudoers.d/dsh-bridge

# (you) paste the freellmapi unified key when asked
read -rp 'freellmapi key: ' K && sed "s/freellmapi-REPLACE_ME/$K/" runner.env.example | sudo install -m 0600 /dev/stdin /etc/dsh/runner.env && unset K && clear

sudo -u dsh-bridge sh -c 'cd /data/dsh/bridge && uv venv -q .venv && uv pip install -q --python .venv/bin/python fastapi "uvicorn[standard]"'
sudo install -o dsh-bridge -m 0644 bridge/app.py /data/dsh/bridge/app.py
sudo -u dsh-bridge sh -c 'umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(32))" > /data/dsh/bridge/token'
sudo cp dsh-bridge.service /etc/systemd/system/
sudo cp vm2-backup.service vm2-backup.timer /etc/systemd/system/ && sudo install -m 0700 vm2-backup.sh /usr/local/sbin/vm2-backup.sh
sudo install -d -m 0700 /data/backups
sudo systemctl daemon-reload && sudo systemctl enable --now dsh-bridge vm2-backup.timer
```

If `sudo -u dsh-bridge … uv` can't find `uv`, use its full path (`which uv`).

Check: `curl -s localhost:8000/health` shows `"bridge":"ok"`, `"freellmapi":"ok"`, `"dsh":true`.

## 6. Laptop: the reminder worker (Cloudflare)

**Laptop** (Git Bash), in `ATLAS/assistant/reminder_worker`:

```bash
npx wrangler login                                   # (you) approve in the browser
npx wrangler d1 create pa-reminders                  # copy database_id into my.env, then run fill again
npx wrangler d1 execute pa-reminders --remote --file=schema.sql
npx wrangler deploy                                  # if the workers.dev subdomain is taken, (you) pick one in the Cloudflare dashboard
npx wrangler secret put TELEGRAM_BOT_TOKEN           # (you) paste the bot token from @BotFather
npx wrangler secret put TELEGRAM_CHAT_ID             # (you) your numeric Telegram ID (you get it in step 7)
python -c "import secrets;print(secrets.token_urlsafe(32))" > sync_secret.txt
npx wrangler secret put SYNC_SECRET < sync_secret.txt
```

Keep `sync_secret.txt` until step 8, then delete it. Check:
`curl -s https://pa-reminders.YOUR_SUBDOMAIN.workers.dev/health` returns `{"ok":true,…}`.

## 7. VM 1: Hermes and Telegram

**(you)** Install Hermes Agent with its official installer (see
[the Hermes docs](https://hermes-agent.nousresearch.com)). Then:

```bash
echo 'export HERMES_HOME=/data/hermes' >> ~/.bashrc && export HERMES_HOME=/data/hermes
loginctl enable-linger "$USER"
systemctl --user list-units 'hermes-gateway*' --no-legend | grep -q . || hermes gateway install   # creates the service if missing
GW=$(systemctl --user list-units 'hermes-gateway*' --no-legend | awk '{print $1}' | head -1); echo "gateway unit: $GW"
echo "export GW=$GW" >> ~/.bashrc

# (you) paste the bot token; the prompt shows what you pasted, then clears the screen
read -rp 'Bot token: ' T && [ -n "$T" ] && printf 'TELEGRAM_BOT_TOKEN=%s\n' "$T" >> /data/hermes/.env && unset T && clear && echo saved
systemctl --user restart "$GW"
```

**(you)** Message your bot. It replies with a pairing code. Then:

```bash
hermes pairing approve telegram <CODE>                                  # prints your numeric user ID
echo 'TELEGRAM_ALLOWED_USERS=<your numeric ID>' >> /data/hermes/.env
```

Use that numeric ID for `TELEGRAM_CHAT_ID` in step 6. Check: the bot answers you, and
`journalctl --user -u "$GW" --since -2min | grep 'Connected to Telegram'` prints a line.

## 8. VM 1: tools, rules, models

```bash
cd ~/ATLAS
cp /data/hermes/config.yaml /data/hermes/config.yaml.bak

# re-enable the toolsets that matter (removes them from agent.disabled_toolsets)
python3 - <<'PY'
import re
p = "/data/hermes/config.yaml"; enable = {"web", "memory", "session_search", "delegation", "todo", "kanban",
                                          "cronjob", "code_execution", "browser", "clarify"}
out, in_agent, in_list = [], False, False
for line in open(p).read().split("\n"):
    if re.match(r"^[a-z_]+:", line): in_agent, in_list = line.startswith("agent:"), False
    if in_agent and line == "  disabled_toolsets:": in_list = True; out.append(line); continue
    m = re.match(r"^    - '?([a-z_0-9]+)'?\s*$", line) if in_list else None
    if m and m.group(1) in enable: continue
    if in_list and not m: in_list = False
    out.append(line)
open(p, "w").write("\n".join(out)); print("toolsets updated")
PY
sed -i 's/^  cli: \[.*\]$/  cli: [hermes-telegram]/' /data/hermes/config.yaml

for kv in "memory.memory_enabled true" "memory.user_profile_enabled true" "memory.write_approval true" \
          "agent.max_turns 60" "agent.budget_warning_ratio 0.5" "agent.clarify_timeout 0" \
          "delegation.max_concurrent_children 2" "delegation.max_spawn_depth 1" "delegation.max_iterations 40" \
          "kanban.max_in_progress 2" "approvals.destructive_slash_confirm true" \
          "compression.enabled true" "compression.threshold 0.35" "tools.tool_search.enabled off" \
          "platforms.telegram.extra.drop_pending_on_cold_boot false"; do hermes config set $kv >/dev/null; done
hermes config unset mcp_servers.instabox 2>/dev/null; hermes config unset mcp_servers.open-computer-use 2>/dev/null
cat vm1/SOUL-additions.md >> /data/hermes/SOUL.md

# firewall: the assistant's port only for your laptop
sudo cp vm1/nftables.conf /etc/nftables.conf && sudo nft -f /etc/nftables.conf && sudo systemctl enable nftables

# MCP environment and the DSH tools
mkdir -p /data/hermes/mcp && chmod 700 /data/hermes/mcp && cd /data/hermes/mcp
uv venv -q .venv && uv pip install -q --python .venv/bin/python 'mcp>=1.2,<2'
cp ~/ATLAS/vm1/dsh_mcp.py ~/ATLAS/assistant/pa_mcp/pa_mcp.py /data/hermes/mcp/
ssh <VM2 login> 'sudo cat /data/dsh/bridge/token' > /data/hermes/mcp/dsh_token && chmod 600 /data/hermes/mcp/dsh_token
hermes config set mcp_servers.dsh.command /data/hermes/mcp/.venv/bin/python
hermes config set mcp_servers.dsh.args '["/data/hermes/mcp/dsh_mcp.py"]'
hermes config set mcp_servers.dsh.env.DSH_BRIDGE_URL "http://$(grep VM2_TAILSCALE_IP ~/ATLAS/my.env | cut -d= -f2):8000"

# (you) paste the freellmapi unified key; then freellmapi becomes the main model
read -rp 'freellmapi key: ' K && printf 'FREELLMAPI_API_KEY=%s\n' "$K" >> /data/hermes/.env && unset K && clear
. ~/ATLAS/my.env
hermes config set model.provider custom
hermes config set model.base_url "http://$VM2_TAILSCALE_IP:3001/v1"
hermes config set model.default auto:smart
hermes config unset model.api_key 2>/dev/null
hermes config set model.key_env FREELLMAPI_API_KEY
hermes config set model.context_length 65536
hermes config set fallback_providers "[{\"provider\":\"custom\",\"model\":\"qwen3.5-4b-64k\",\"base_url\":\"http://$LAPTOP_TAILSCALE_IP:11434/v1\",\"api_key\":\"ollama\"},{\"provider\":\"custom\",\"model\":\"qwen3.5-4b-64k\",\"base_url\":\"http://$VM2_TAILSCALE_IP:11434/v1\",\"api_key\":\"ollama\"}]"
```

Check: `hermes doctor` passes, `hermes mcp test dsh` finds 8 tools.

## 9. VM 1: the personal-assistant service

```bash
mkdir -p /data/pa/service && chmod 700 /data/pa
cp ~/ATLAS/assistant/pa_service/* /data/pa/service/ && cd /data/pa/service
uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
(umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(32))" > /data/pa/token)
.venv/bin/python -m pytest -q -W ignore::DeprecationWarning                 # expect all tests to pass
MYTZ=Asia/Kolkata                     # (you) your time zone, e.g. Europe/Berlin, America/New_York
mkdir -p ~/.config/systemd/user && cp ~/ATLAS/assistant/pa-service.service ~/.config/systemd/user/
sed -i -e "s|PA_TIMEZONE=.*|PA_TIMEZONE=$MYTZ|" \
       -e "/^Environment=PA_TIMEZONE/a Environment=HERMES_GATEWAY_UNIT=$GW" ~/.config/systemd/user/pa-service.service
systemctl --user daemon-reload && systemctl --user enable --now pa-service

hermes config set mcp_servers.pa.command /data/hermes/mcp/.venv/bin/python
hermes config set mcp_servers.pa.args '["/data/hermes/mcp/pa_mcp.py"]'
systemctl --user restart "$GW"
```

Connect the service to the reminder worker. Copy `sync_secret.txt` from the laptop's `reminder_worker` folder to
VM 1 first (or paste its contents at the prompt):

```bash
. ~/ATLAS/my.env
read -rp 'SYNC_SECRET: ' S && printf '{"url":"https://pa-reminders.%s.workers.dev","secret":"%s"}\n' "$YOUR_SUBDOMAIN" "$S" | (umask 077; cat > /data/pa/worker.json) && unset S && clear
```

Check: `curl -s 127.0.0.1:8100/health` shows the time zone and local time; in Telegram, "Remind me in 3 minutes to
test reminders" arrives on time.

Optional, if your VMs pause when idle: the wake-on-message secrets and the `wake-vm2` hook. The commands are in
`docs/05-assistant.md`, section "Waking sleeping VMs".

## 10. VM 1: backups

```bash
mkdir -p ~/bin /data/backups && chmod 700 /data/backups
cp ~/ATLAS/vm1/hermes-backup.sh ~/bin/ && chmod 700 ~/bin/hermes-backup.sh
cp ~/ATLAS/vm1/hermes-backup.service ~/ATLAS/vm1/hermes-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now hermes-backup.timer
systemctl --user start hermes-backup.service && ls -la /data/backups/critical/
```

## 11. Laptop: dashboard and wake script

**Laptop** (PowerShell), in `ATLAS\laptop`:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.hermes-dashboard" | Out-Null
ssh <VM1 login> "cat /data/pa/token" | Out-File -Encoding ascii -NoNewline "$env:USERPROFILE\.hermes-dashboard\token"
notepad wake-vms.ps1         # (you) set the two SSH login lines at the top ($vm1 / $vm2) and the gateway unit name
.\wake-vms.ps1               # wakes both VMs, checks every service, copies backups
.\dashboard.ps1              # opens http://127.0.0.1:8765
```

If PowerShell blocks scripts: `powershell -ExecutionPolicy Bypass -File .\wake-vms.ps1`.

Delete `assistant\reminder_worker\sync_secret.txt` once step 9 is done.

## 12. Final check

| Where | Command | Expect |
|---|---|---|
| VM 1 | `hermes doctor` | All checks passed |
| VM 1 | `hermes mcp test pa` and `hermes mcp test dsh` | 23 and 8 tools |
| VM 1 | `curl -s -H "Authorization: Bearer $(cat /data/pa/token)" 127.0.0.1:8100/system` | Every component `"ok": true` |
| VM 2 | `curl -s localhost:8000/health` | bridge, freellmapi and Ollama ok |
| Laptop | `.\wake-vms.ps1` | All green |
| Telegram | `Add test task tomorrow 10am` then `What's on tomorrow?` | The task, with the exact time |
| Telegram | `Use DSH to write hello.py that prints hi and run it` | A task id and the output `hi` |

The full list of things to say to the bot is in [the command reference](09-commands.md).
