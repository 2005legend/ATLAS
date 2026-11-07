# 4. VM 2: models, freellmapi, the DSH sandbox and the bridge

All commands run on VM 2 unless marked otherwise. Replace the placeholders in the `vm2/` files first.

## Firewall first

`vm2/nftables.conf` lets only VM 1 (and the VM itself) reach Ollama (11434) and the bridge (8000), lets only VM 1
and your laptop reach freellmapi (3001, a Docker port, so it needs the `forward` chain), and blocks the sandbox
user `dsh-runner` from opening connections to any Tailscale address.

```bash
sudo cp nftables.conf /etc/nftables.conf     # after replacing VM1_TAILSCALE_IP / LAPTOP_TAILSCALE_IP
sudo nft -c -f /etc/nftables.conf && sudo nft -f /etc/nftables.conf && sudo systemctl enable nftables
```

## Ollama (offline fallback model)

```bash
sudo apt-get update && sudo apt-get install -y zstd     # the Ollama installer needs it
curl -fsSL https://ollama.com/install.sh | sh
sudo mkdir -p /data/ollama/models && sudo chown -R ollama:ollama /data/ollama
sudo mkdir -p /etc/systemd/system/ollama.service.d && sudo cp ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
ollama pull qwen3.5:4b
printf 'FROM qwen3.5:4b\nPARAMETER num_ctx 65536\n' > /tmp/m && ollama create qwen3.5-4b-64k -f /tmp/m
```

A 4B model on 4 vCPU writes about 2.5 tokens/s. That's too slow for coding tasks (it timed out on 2 of 3) but
fine as a last-resort fallback when every free provider is down.

## freellmapi (the free-model router)

```bash
sudo mkdir -p /data/freellmapi && sudo chown "$USER" /data/freellmapi && cd /data/freellmapi
git clone https://github.com/tashfeenahmed/freellmapi src && cd src
git checkout <latest release tag>                 # pin a version; this build used v0.16.20
(umask 077; printf 'ENCRYPTION_KEY=%s\nPORT=3001\n' "$(openssl rand -hex 32)" > .env)
cp ../../path/to/freellmapi-compose.override.yml compose.override.yml   # edit VM2_TAILSCALE_IP
```

Two VM-specific fixes, both needed in this build:

- **Docker DNS.** After Tailscale is up, the VM's DNS points at `100.100.100.100`, which containers can't reach,
  so builds fail with "Temporary failure resolving". Give Docker the original resolvers and make it start after
  Tailscale (freellmapi publishes on the Tailscale IP):

  ```bash
  sudo cp docker-daemon.json /etc/docker/daemon.json
  sudo mkdir -p /etc/systemd/system/docker.service.d && sudo cp docker-wait-tailscale.conf /etc/systemd/system/docker.service.d/
  sudo systemctl daemon-reload && sudo systemctl restart docker
  ```

- **TLS-inspecting proxy.** InstaBox routes HTTPS through its own proxy, so containers must trust its CA. The
  override file mounts `/usr/local/share/ca-certificates/instabox-proxy.crt` and sets `NODE_EXTRA_CA_CERTS`.
  On another host, delete those two lines.

Build from source (not the `curl | bash` installer) and start it:

```bash
sudo docker compose build && sudo docker compose up -d
sudo docker compose logs | grep 'setup code'
```

Open `http://VM2_TAILSCALE_IP:3001` from your laptop, create the admin account with the setup code, add one API
key per provider (one account per provider; several accounts to stretch free limits breaks most providers'
terms), and create a unified API key (`freellmapi-…`).

**Check:** `curl -s http://127.0.0.1:3001/api/ping` returns `{"status":"ok"…}`.

## DeepSeek Harness in a per-task sandbox

```bash
sudo npm install -g @deepseek-ai/dsh@latest            # needs Node 22+
sudo useradd --system --home-dir /data/dsh/home --shell /usr/sbin/nologin dsh-runner
sudo useradd --system --home-dir /data/dsh/bridge --shell /usr/sbin/nologin dsh-bridge
sudo mkdir -p /data/dsh/{workspaces,homes,inbox,results,logs,bridge} /etc/dsh
sudo chown root:root /data/dsh/workspaces /data/dsh/homes && sudo chmod 0711 /data/dsh/workspaces /data/dsh/homes
sudo chown -R dsh-bridge:dsh-bridge /data/dsh/{inbox,results,logs,bridge} && sudo chmod 0700 /data/dsh/{inbox,results,logs,bridge}
sudo install -m 0644 cordis.patch.yml /etc/dsh/cordis.patch.yml
sudo install -m 0600 runner.env.example /etc/dsh/runner.env      # then put your freellmapi key in it
sudo install -m 0755 dsh-ctl /usr/local/sbin/dsh-ctl
echo 'dsh-bridge ALL=(root) NOPASSWD: /usr/local/sbin/dsh-ctl' | sudo tee /etc/sudoers.d/dsh-bridge && sudo chmod 0440 /etc/sudoers.d/dsh-bridge
sudo visudo -cf /etc/sudoers.d/dsh-bridge
```

`dsh-ctl` is the only thing the bridge may run as root. Each task runs as a transient systemd unit:
`User=dsh-runner`, `TemporaryFileSystem=/data` with only that task's workspace and home bound back,
`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, 2 GB memory, 3 CPUs and a hard time
limit. The freellmapi key comes from the root-only `EnvironmentFile`, never a command line.

## The delegation bridge

```bash
sudo -u dsh-bridge sh -c 'cd /data/dsh/bridge && uv venv -q .venv && uv pip install -q --python .venv/bin/python fastapi "uvicorn[standard]"'
sudo install -o dsh-bridge -m 0644 bridge/app.py /data/dsh/bridge/app.py
sudo -u dsh-bridge sh -c 'umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(32))" > /data/dsh/bridge/token'
sudo cp dsh-bridge.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now dsh-bridge
```

**Check:** `curl -s localhost:8000/health` shows `"bridge":"ok"`, `"freellmapi":"ok"` and `"dsh":true`.

## Connect Hermes (on VM 1)

```bash
mkdir -p /data/hermes/mcp && cd /data/hermes/mcp
uv venv -q .venv && uv pip install -q --python .venv/bin/python 'mcp>=1.2,<2'    # mcp 2.x renamed FastMCP
cp dsh_mcp.py /data/hermes/mcp/
ssh VM2 'sudo cat /data/dsh/bridge/token' > /data/hermes/mcp/dsh_token && chmod 600 /data/hermes/mcp/dsh_token
hermes config set mcp_servers.dsh.command /data/hermes/mcp/.venv/bin/python
hermes config set mcp_servers.dsh.args '["/data/hermes/mcp/dsh_mcp.py"]'
hermes config set mcp_servers.dsh.env.DSH_BRIDGE_URL http://VM2_TAILSCALE_IP:8000
```

Then make freellmapi Hermes's main model, with the laptop and VM 2 models as fallbacks. Save the unified key on
VM 1 first: `read -rp 'freellmapi key: ' K && printf 'FREELLMAPI_API_KEY=%s\n' "$K" >> /data/hermes/.env && unset K`.

```bash
hermes config set model.base_url http://VM2_TAILSCALE_IP:3001/v1
hermes config set model.default auto:smart
hermes config unset model.api_key
hermes config set model.key_env FREELLMAPI_API_KEY
hermes config set fallback_providers '[{"provider":"custom","model":"qwen3.5-4b-64k","base_url":"http://LAPTOP_TAILSCALE_IP:11434/v1","api_key":"ollama"},{"provider":"custom","model":"qwen3.5-4b-64k","base_url":"http://VM2_TAILSCALE_IP:11434/v1","api_key":"ollama"}]'
systemctl --user restart HERMES_GATEWAY_UNIT
```

**Check:** `hermes mcp test dsh` finds 8 tools. In Telegram: "Use DSH to write a prime checker with pytest tests
and run them." It reports a task id and the test output within a minute or so.
