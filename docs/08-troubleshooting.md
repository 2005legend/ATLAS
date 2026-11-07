# 8. Troubleshooting: every problem we hit

Each of these cost real time in this build.

| Symptom | Cause | Fix |
|---|---|---|
| `tailscale up` then "Logged out" | Ctrl+C before finishing the browser login | Run it again and leave it until you've signed in |
| VM can't reach laptop Ollama, request times out | An old Ollama process from before `OLLAMA_HOST` was set still holds the port, bound to 127.0.0.1 | Kill every `ollama` process, start Ollama again; `netstat -ano | findstr :11434` should show `0.0.0.0` |
| Hermes rejects a model | Hermes requires at least 64,000 tokens of context; Ollama's VRAM-based default is 4,096 and some models cap at 32K | Make a Modelfile copy with `PARAMETER num_ctx 65536` and set `model.ollama_num_ctx` |
| Telegram token "saved" but empty | A hidden `read -s` prompt in a browser terminal ignored the paste | Use the visible `read -rp` version from step 3 |
| Telegram token doesn't work | The trailing "." from BotFather's message got copied | Remove it; check with Telegram's `getMe` |
| Bot answers, but tools seem missing | `agent.disabled_toolsets` disables web, memory, delegation, clarify…; the CLI platform had only 4 toolsets | Step 3: remove them from the list, set `cli: [hermes-telegram]` |
| Assistant tools ignored, notes saved to the wrong place | `tool_search` hides MCP tools and free models search badly | `tools.tool_search.enabled off` |
| Habit "5 times a week" saved as daily | An optional parameter with a default; the model omitted it | Make the frequency a required parameter, parsed in code |
| Making a parameter required broke saving entirely | The model produced an invalid tool call | Prefer a separate, obviously named tool (`idea_add`) over a required enum |
| The model writes its own tests, they fail, it loops on correct code | Weak model, no stop rule | SOUL "stop and ask" rule plus `max_turns`; grade with hidden tests, not the model's |
| Bot never shows buttons when stuck | `clarify` was in the disabled toolsets | Remove it from `agent.disabled_toolsets` |
| `gpt-oss-120b` works in a tiny test, fails in Hermes with 413 | Groq's free tier caps tokens per request below Hermes's ~10–20K prompt | Test candidate models with a realistic prompt size |
| freellmapi `auto:smart` sometimes lands on 3B/8B models | Free tiers run out during the day | A named freellmapi chain with only proven models, or a paid key as one provider |
| Docker build fails resolving `deb.debian.org` | Tailscale MagicDNS (100.100.100.100) isn't reachable from containers | `/etc/docker/daemon.json` with `"dns": ["1.1.1.1","8.8.8.8"]` |
| freellmapi doesn't start after reboot | Docker started before the Tailscale IP existed | `docker-wait-tailscale.conf` drop-in |
| Firewall config wipes Docker's rules | Debian's default `/etc/nftables.conf` begins with `flush ruleset` | Only touch your own table (`table … / delete table … / table … { }`) |
| Messages sent while VM 1 was off are lost | `drop_pending_on_cold_boot` defaults to true | Set it to false |
| "freellmapi is down" | The VM was paused (InstaBox idle pause), not crashed | Any SSH connection wakes it; run `wake-vms.ps1` |
| Windows PowerShell 5.1 mangles remote commands | It strips inner double quotes from arguments passed to `ssh` | Write remote commands without inner double quotes |
| `wake-vms.ps1` says VM 1 can't reach the bridge | VM 2 was still paused when VM 1 checked it | Wake VM 2 first (the script does) |
| Telegram `/stop` doesn't stop a busy turn | The command never reached the gateway | `systemctl --user restart HERMES_GATEWAY_UNIT` on VM 1 |
| A first reminder arrives 2 minutes late | A new Cloudflare cron trigger takes a few minutes to start | Later reminders are on time |
| `mcp` import error `mcp.server.fastmcp` | mcp 2.x renamed FastMCP | Pin `mcp>=1.2,<2` |
| DSH writes files the checker can't read | DSH creates files with mode 600 as `dsh-runner` | Run checks as `dsh-runner` (or through `dsh-ctl read`) |
| `qwen2.5-coder:3b` "finishes" tasks instantly, does nothing | Ollama's build of it doesn't make real tool calls; it prints them as text | Use a model with real tool calling |
