# 2. Private network and laptop Ollama

## Tailscale on all three machines

On each VM (Debian 13 "trixie"):

```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update && sudo apt-get install -y tailscale nftables
sudo tailscale up
```

`tailscale up` prints a login link and waits. Open it in your browser and sign in with the same account on
every machine. Don't press Ctrl+C before you have signed in, or the VM stays logged out.

On Windows: `winget install Tailscale.Tailscale`, then sign in from the tray icon.

**Check:** `tailscale status` on any machine lists all three. `tailscale ip -4` gives each machine's
`100.x.y.z` address; write them down as `VM1_TAILSCALE_IP`, `VM2_TAILSCALE_IP`, `LAPTOP_TAILSCALE_IP`.

## Laptop Ollama (optional fallback model)

Hermes needs a model with at least a 64K context. The 4B model that worked on a 4 GB laptop GPU:

```powershell
ollama pull qwen3.5:4b
@"
FROM qwen3.5:4b
PARAMETER num_ctx 65536
"@ | Set-Content -Encoding ascii qwen-64k.Modelfile
ollama create qwen3.5-4b-64k -f qwen-64k.Modelfile
```

Let VM 1 reach it over Tailscale only (run in an Administrator PowerShell):

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_HOST','0.0.0.0:11434','User')
New-NetFirewallRule -DisplayName "Ollama via Tailscale only" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Any
```

Quit Ollama completely (tray icon, and check no `ollama` process is left in Task Manager), then start it again.
If Windows asks to allow Ollama through the firewall, click Cancel; the rule above already covers Tailscale.
Ollama's Startup shortcut starts it at login with this setting.

**Check (from VM 1):** `curl -s http://LAPTOP_TAILSCALE_IP:11434/v1/models` lists your models.
