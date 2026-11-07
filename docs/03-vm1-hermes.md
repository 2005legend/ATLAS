# 3. VM 1: Hermes, Telegram, memory and tools

## Install Hermes

Install Hermes Agent with its official installer (see the Hermes docs). Keep its data on the persistent disk:

```bash
echo 'export HERMES_HOME=/data/hermes' >> ~/.bashrc && export HERMES_HOME=/data/hermes
```

Find the name of the gateway service (it differs per install) and write it down as `HERMES_GATEWAY_UNIT`:

```bash
systemctl --user list-units 'hermes-gateway*' --no-legend
loginctl enable-linger "$USER"   # user services start at boot without a login
```

**Check:** `hermes doctor` ends with "All checks passed".

## A first model

Until VM 2 is ready, point Hermes at the laptop model from step 2. Hermes refuses models under 64K context,
so set the context explicitly:

```bash
hermes config set model.provider custom
hermes config set model.base_url http://LAPTOP_TAILSCALE_IP:11434/v1
hermes config set model.default qwen3.5-4b-64k
hermes config set model.api_key ollama
hermes config set model.context_length 65536
hermes config set model.ollama_num_ctx 65536
```

Step 4 switches the main model to freellmapi, which is much faster.

## Turn on the tools that matter

A fresh install may ship with many toolsets disabled, and with the command-line platform limited to four
tools. Both made this assistant much weaker until we found them. Open `/data/hermes/config.yaml`, find
`agent.disabled_toolsets`, and remove these entries from the list: `web`, `memory`, `session_search`,
`delegation`, `todo`, `kanban`, `cronjob`, `code_execution`, `browser`, `clarify`. Edit the text directly and
keep a backup; leave the other entries alone.

Then give the command-line platform the same tools as Telegram (benchmarks and one-shot runs use it), and set
the guardrails:

```bash
cp /data/hermes/config.yaml /data/hermes/config.yaml.bak
sed -i 's/^  cli: \[.*\]$/  cli: [hermes-telegram]/' /data/hermes/config.yaml

hermes config set memory.memory_enabled true
hermes config set memory.user_profile_enabled true
hermes config set memory.write_approval true          # memories wait for /memory approve
hermes config set agent.max_turns 60                  # wraps up and asks after 60 model calls
hermes config set agent.budget_warning_ratio 0.5      # one check-in halfway
hermes config set agent.clarify_timeout 0             # wait for your answer instead of carrying on alone
hermes config set delegation.max_concurrent_children 2
hermes config set delegation.max_spawn_depth 1
hermes config set delegation.max_iterations 40
hermes config set kanban.max_in_progress 2
hermes config set approvals.destructive_slash_confirm true
hermes config set compression.enabled true
hermes config set compression.threshold 0.35
hermes config set tools.tool_search.enabled off        # see note below
hermes config set platforms.telegram.extra.drop_pending_on_cold_boot false
```

About `tool_search`: with it on, Hermes hides MCP tools (the assistant and DSH tools) behind a search step, and
free models search badly. Turning it off makes every call larger (about 20K tokens) but the assistant tools get
picked reliably.

If your VM image added MCP servers you don't need (InstaBox added `instabox` and `open-computer-use`, which
control the sandbox account and desktop), remove them:

```bash
hermes config unset mcp_servers.instabox
hermes config unset mcp_servers.open-computer-use
```

Install pytest so Hermes can check the code it writes: `sudo apt-get install -y python3-pytest`.

## House rules (SOUL.md)

Append `vm1/SOUL-additions.md` to `/data/hermes/SOUL.md`. It tells Hermes to stop and ask (with buttons) when
it is stuck instead of looping, to delegate when told to, to ask before creating scheduled jobs, to always use
the assistant tools for tasks and notes, and to confirm only what a tool actually saved.

```bash
cat SOUL-additions.md >> /data/hermes/SOUL.md
```

## Telegram

1. Create a bot with @BotFather and copy its token.
2. Save the token on VM 1. This prompt shows what you paste, so you can see it arrived; it clears the screen
   after:

   ```bash
   read -rp 'Bot token: ' T && [ -n "$T" ] && printf 'TELEGRAM_BOT_TOKEN=%s\n' "$T" >> /data/hermes/.env && unset T && clear && echo saved
   ```

   Paste with Ctrl+Shift+V in browser terminals. Copy the token without the trailing full stop that BotFather's
   sentence puts after it.
3. Restart the gateway: `systemctl --user restart HERMES_GATEWAY_UNIT`.
4. Message your bot. It replies with a pairing code. Approve it on VM 1:
   `hermes pairing approve telegram <CODE>`. The output shows your numeric user ID.
5. Lock the bot to you as well: `echo 'TELEGRAM_ALLOWED_USERS=YOUR_TELEGRAM_USER_ID' >> /data/hermes/.env`, then
   restart the gateway again.
6. In the chat, send `/sethome`.

**Check:** `journalctl --user -u HERMES_GATEWAY_UNIT --since -2min | grep 'Connected to Telegram'` prints a
line, and the bot answers you (and only you).

## Firewall and backups

Copy `vm1/nftables.conf` to `/etc/nftables.conf` after replacing `LAPTOP_TAILSCALE_IP`, then
`sudo nft -f /etc/nftables.conf && sudo systemctl enable nftables`. It opens the assistant's port 8100 to your
laptop only. It deliberately doesn't use `flush ruleset`, which would wipe Docker's rules.

Backups are set up in [step 7](07-backups.md).
