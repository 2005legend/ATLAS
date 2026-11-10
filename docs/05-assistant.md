# 5. The personal assistant, reminders and dashboard

## The service (VM 1)

```bash
sudo mkdir -p /data/pa/service && sudo chown -R "$USER" /data/pa && chmod 700 /data/pa
cp -r assistant/pa_service/* /data/pa/service/ && cd /data/pa/service
uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
(umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(32))" > /data/pa/token)
.venv/bin/python -m pytest -q          # 69 tests
cp ../../path/to/pa-service.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now pa-service
```

The unit sets `PA_TIMEZONE=Asia/Kolkata`; change it to yours. Dates without a time mean 23:59 for task
deadlines and 09:00 for reminders. Past and nonsense times are refused and nothing is saved.

Give Hermes the tools:

```bash
cp assistant/pa_mcp/pa_mcp.py /data/hermes/mcp/
hermes config set mcp_servers.pa.command /data/hermes/mcp/.venv/bin/python
hermes config set mcp_servers.pa.args '["/data/hermes/mcp/pa_mcp.py"]'
systemctl --user restart HERMES_GATEWAY_UNIT
```

**Check:** `hermes mcp test pa` finds the tools; in Telegram, "Add call my guide tomorrow 5pm" answers with the
exact saved time, and `curl -s 127.0.0.1:8100/health` shows `open_tasks: 1`.

### Modules

One service, one SQLite file. Anything that has to fire at a time (a follow-up, an interview, the end of a focus
session) becomes a reminder in the Cloudflare worker; if scheduling fails, the record is still saved and the
tool result carries a `warning` the bot must pass on.

| Module | Tables | Notes |
|---|---|---|
| Tasks, inbox, reminders, agenda | `tasks`, `inbox` (+ FTS) | Inbox items stay "new" until sorted (`inbox_triage`) |
| Habits, expenses | `habits`, `checkins`, `expenses` | Weekday habits keep their streak over weekends |
| Projects, weekly review | `projects`, `tasks.project_id`, `tasks.milestone` | Names match ignoring case and punctuation; review sent Sundays 18:00 |
| Follow-ups | `followups` | Reminder at the due time (09:00 if no time); closing it cancels the reminder |
| Job tracker | `applications`, `application_events` | Interview time → reminders 18:00 the day before and 1 h before; status changes logged |
| Focus sessions | `focus_sessions` | One at a time; end message with a break; a forgotten session closes at its planned end |
| Learning | `subjects`, `study_log`, `modules` | Goals and durations parsed in code ("3 hours a week", "1h30"); focus sessions count as study |
| Spaced repetition | `cards` | Gaps 3 → 7 → 14 → 30 → 60 days, back to 1 on "forgot"; due topics in the morning briefing |

Schema changes are applied at start-up (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE` for new columns), so
updating is: back up `/data/pa/pa.db`, copy the new `app.py`, run the tests, restart `pa-service`.

## The reminder worker (Cloudflare)

On your laptop, in `assistant/reminder_worker`:

```bash
npx wrangler login
npx wrangler d1 create pa-reminders          # copy the database_id into wrangler.toml
npx wrangler d1 execute pa-reminders --remote --file=schema.sql
npx wrangler deploy
```

If deploy says your workers.dev subdomain is unavailable, pick one under Workers & Pages in the Cloudflare
dashboard and deploy again. Then set three secrets. Type the bot token yourself at the prompt; generate the
shared secret and give the same value to VM 1:

```bash
printf '%s' 'YOUR_TELEGRAM_USER_ID' | npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler secret put TELEGRAM_BOT_TOKEN
S=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
printf '%s' "$S" | npx wrangler secret put SYNC_SECRET
printf '{"url":"https://pa-reminders.YOUR_SUBDOMAIN.workers.dev","secret":"%s"}\n' "$S" | ssh VM1 '(umask 077; cat > /data/pa/worker.json)'
```

The worker runs every minute, sends due reminders, moves repeating ones to their next time (daily, weekdays,
weekly, monthly, in your local time), retries failures, and sends the 08:00 briefing. VM 1 pushes the raw open-task
list whenever tasks change, and the worker sorts it into overdue, today and the next 7 days at send time, so the
briefing is right even if the VM slept since.

A brand-new cron trigger can take a few minutes to start firing.

**Check:** in Telegram, "Remind me in 3 minutes to test reminders". The reply shows the exact time, and the
reminder arrives with a `[#rxxxxxxxx]` id. `curl -s https://pa-reminders.YOUR_SUBDOMAIN.workers.dev/health`
shows the pending count; any other path without the secret returns 401.

### Waking sleeping VMs (optional)

Free sandboxes pause when idle. A paused VM 1 doesn't poll Telegram, and a paused VM 2 can't answer model calls.
Two pieces cover both cases, and nothing runs unless a real message arrives (no keep-alive):

1. **VM 1 asleep:** every minute the worker asks Telegram how many messages are waiting (`getWebhookInfo`, which
   doesn't consume them). If any are waiting and a VM isn't running, it calls the provider's wake API and tells
   you "⏳ Waking up…" (at most one notice per 10 minutes).
2. **VM 2 asleep while VM 1 is awake:** the Hermes hook `vm1/hooks/wake-vm2` runs at the start of every agent
   turn. It pings freellmapi (2 s); only if that fails does it call the worker's `POST /wake/vm2` and wait up to
   60 s for freellmapi to answer.

For InstaBox, create a restricted API key per VM (scope `machines:write`) and set four secrets:

```bash
npx wrangler secret put INSTABOX_KEY_VM1        # type the key at the prompt
npx wrangler secret put INSTABOX_KEY_VM2
printf '%s' 'm_your_vm1_machine_id' | npx wrangler secret put INSTABOX_MACHINE_VM1
printf '%s' 'm_your_vm2_machine_id' | npx wrangler secret put INSTABOX_MACHINE_VM2
```

Without them the worker skips waking. Install the hook on VM 1 (set your VM 2 Tailscale IP first):

```bash
mkdir -p /data/hermes/hooks/wake-vm2 && cp vm1/hooks/wake-vm2/* /data/hermes/hooks/wake-vm2/
sed -i "s/VM2_TAILSCALE_IP/100.x.y.z/" /data/hermes/hooks/wake-vm2/handler.py
systemctl --user restart HERMES_GATEWAY_UNIT
```

**Check:** `curl -s -H "Authorization: Bearer $SYNC_SECRET" https://pa-reminders.YOUR_SUBDOMAIN.workers.dev/wake/check`
shows each VM's state without waking anything; `/wake/status` shows the last wake. A manually paused machine is
not woken by the wake call, only an idle-paused one.

## The dashboard (laptop)

```bash
mkdir %USERPROFILE%\.hermes-dashboard
ssh VM1 "cat /data/pa/token" > %USERPROFILE%\.hermes-dashboard\token
```

Set `PA_URL` in `laptop/dashboard/server.py` (or the environment) to `http://VM1_TAILSCALE_IP:8100`, then run
`laptop/dashboard.ps1`. It opens `http://127.0.0.1:8765` with tasks, habits, spending, reminders, the inbox and
the health of every component. The local server keeps the token out of the browser, only forwards an allow-list
of API calls, and refuses requests from other websites.
