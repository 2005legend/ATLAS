# 1. Overview and what you need

## Accounts (all free)

| Account | Used for | Notes |
|---|---|---|
| Two small Linux VMs | VM 1 runs Hermes, VM 2 runs the coding worker | This build used two InstaBox sandboxes, Debian 13, 4 vCPU, 8 GB RAM, 20 GB `/data` disk each |
| Tailscale | Private network between laptop and VMs | One account, all three devices on it |
| Telegram | The chat interface | Create a bot with @BotFather |
| Cloudflare | The reminder worker and its D1 database | Workers free plan: 100,000 requests/day, 5 cron triggers |
| Free model providers | The models behind freellmapi | This build used Groq, Mistral, OpenRouter (free models), Ollama Cloud; one account per provider |

A laptop with Python 3.11+, Node 20+ (for `npx wrangler`) and optionally Ollama.

## Placeholders you will fill in

| Placeholder | Where it comes from |
|---|---|
| `VM1_TAILSCALE_IP`, `VM2_TAILSCALE_IP`, `LAPTOP_TAILSCALE_IP` | `tailscale ip -4` on each machine (step 2) |
| `VM1_SSH_TARGET`, `VM2_SSH_TARGET` | Your VM provider's SSH login, e.g. `user@host` |
| `HERMES_GATEWAY_UNIT` | `systemctl --user list-units 'hermes-gateway*'` on VM 1 (step 3) |
| `YOUR_TELEGRAM_USER_ID` | Shown when you approve the bot pairing (step 3) |
| `YOUR_D1_DATABASE_ID`, `YOUR_SUBDOMAIN` | `npx wrangler d1 create` and your workers.dev subdomain (step 5) |

## Why it is split this way

- **Hermes on its own VM** keeps the assistant, its memory and Telegram in one place that you never let run
  untrusted code.
- **The coding agent on a second VM**, inside a per-task systemd sandbox, means a bad or tricked coding task
  can't read your keys, other tasks, or reach your laptop. The only way in is an HTTP bridge with a token.
- **Reminders on Cloudflare**, because free VMs pause when idle. A paused VM can't send a 10 AM reminder; a
  Cloudflare cron trigger can.
- **The database is the source of truth.** The model never "remembers" a task in chat. It calls a tool, the
  service saves it, and only then does the model confirm, repeating the stored date.

## Cost

₹0 a month as built. The limits that come with free tiers are listed in the README.

**Check:** you have SSH access to both VMs and can run `sudo -n true` on them without a password prompt.
