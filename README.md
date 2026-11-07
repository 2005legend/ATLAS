# ATLAS

A personal AI assistant you run yourself for ₹0 a month. You talk to it on Telegram. It keeps your tasks,
reminders, notes, habits and expenses in a real database, sends reminders on time even while its servers
sleep, and hands coding work to a sandboxed coding agent on a second machine.

It is built from free parts:

| Part | What it does | Where it runs |
|---|---|---|
| [Hermes Agent](https://hermes-agent.nousresearch.com) | The assistant: chat, planning, tools, memory, Telegram | Cloud VM 1 |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH) | Coding agent; each task runs in its own systemd sandbox | Cloud VM 2 |
| [freellmapi](https://github.com/tashfeenahmed/freellmapi) | One OpenAI-compatible endpoint over several free model tiers | Cloud VM 2 |
| Personal-assistant service (this repo) | Tasks, inbox, habits, expenses, agenda (FastAPI + SQLite) | Cloud VM 1 |
| Reminder worker (this repo) | Sends reminders and the morning briefing on time | Cloudflare Workers (free plan) |
| Ollama | Local fallback models | Laptop and VM 2 |
| Tailscale | Private network between all three machines | Everywhere |
| Dashboard (this repo) | One page with today's tasks, habits, spending and system health | Your laptop |

The two VMs in this build are free [InstaBox](https://instabox.sh) sandboxes (4 vCPU, 8 GB RAM each). Any small
Linux VM works; InstaBox's limits are why some parts are designed the way they are (see below).

## What it does day to day

You send messages like these:

```
Add finish the project report by Sunday, high priority
Tomorrow I need to call my guide and do an hour of DSA. Add both.
Remind me at 8 PM to study German
Remind me every Sunday 6pm to review my week
Save this idea: an agent that detects reasoning failures in small LMs, for my SLM research
Track German 5 times a week
I did DSA today
Spent ₹120 on lunch
What's on today?
Use DSH to write a prime checker with pytest tests and run them
```

The assistant only says "added" or "set" after the database has saved it, and it repeats the exact date it
stored. Times like "tomorrow 10am" or "by Sunday" are parsed in code, in your time zone, not guessed by the
model.

At 08:00 every morning a briefing arrives with overdue tasks, today's tasks, the next seven days and today's
habits. It comes from the Cloudflare worker, so it arrives even when both VMs are asleep.

## Architecture

```
 You ── Telegram ──► Hermes (VM 1) ── MCP tools ──► personal-assistant service (VM 1, SQLite)
                        │   │                               │ pushes reminders + task list
                        │   │                               ▼
                        │   │                       Cloudflare worker (cron, every minute) ──► Telegram
                        │   │
                        │   └── delegation bridge (HTTP + token, Tailscale) ──► DSH in a systemd sandbox (VM 2)
                        │
                        └── models: freellmapi on VM 2 → laptop Ollama → VM 2 Ollama (fallbacks)

 Laptop: Tailscale, Ollama, dashboard (127.0.0.1:8765), wake-and-backup script
```

## Measured results

These are from this build, not claims:

- Coding through DSH with free models: 3 of 3 benchmark tasks passed (4–13 s each). The same tasks on a 4B
  model on the VM's CPU passed 1 of 3, and the other two hit the 30-minute limit.
- A key-value store with nested transactions, graded by 28 hidden tests: 28/28 when Hermes built it, 26/28
  from DSH.
- A 12-task benchmark of Hermes itself (reasoning, coding, debugging, research, tool use, memory), 2–3 runs
  per setting: 6.3/12 with most tools off, 8.0/12 after enabling the right toolsets. Single runs swing by ±2.
- A Telegram reminder created at 12:37 for "in 3 minutes" arrived at 12:40.

## Honest limits

- **Free hosting sleeps.** InstaBox pauses a VM after 30 idle minutes and stops it after 8 hours of runtime.
  The assistant only answers while VM 1 is awake. Reminders and the briefing don't depend on that, because
  Cloudflare sends them. `laptop/wake-vms.ps1` wakes both VMs and checks every service.
- **Free models vary.** Quality changes during the day as free tiers hit their limits. The small models
  sometimes guess instead of calling a tool. Novelty-level research was not reliable with them.
- **Your messages go to free model providers**, which may log them. Don't send passwords or secrets.
- Snooze works by replying in chat; there are no snooze buttons on reminder messages.

## Setup

Follow the docs in order. Each step ends with a command that tells you whether it worked.

1. [Overview and what you need](docs/01-overview.md)
2. [Private network (Tailscale) and laptop Ollama](docs/02-network.md)
3. [VM 1: Hermes, Telegram, memory and tools](docs/03-vm1-hermes.md)
4. [VM 2: Ollama, freellmapi, DSH in a sandbox, the bridge](docs/04-vm2-worker.md)
5. [The personal assistant, reminders and dashboard](docs/05-assistant.md)
6. [Security model and what was tested](docs/06-security.md)
7. [Backups and reboots](docs/07-backups.md)
8. [Troubleshooting: every problem we hit](docs/08-troubleshooting.md)
9. [Command reference: what to say to the bot, slash commands, admin commands, API](docs/09-commands.md)

Files you will copy are in `vm1/`, `vm2/`, `assistant/` and `laptop/`. They contain placeholders such as
`VM1_TAILSCALE_IP`, `VM2_SSH_TARGET` and `YOUR_D1_DATABASE_ID`; the docs say where each value comes from.

## License

MIT. Hermes Agent, DeepSeek Harness, freellmapi, Ollama and Tailscale are separate projects with their own
licenses; this repo only contains configuration and the small services written for it.
