# 5. The personal assistant, reminders and dashboard

## The service (VM 1)

```bash
sudo mkdir -p /data/pa/service && sudo chown -R "$USER" /data/pa && chmod 700 /data/pa
cp -r assistant/pa_service/* /data/pa/service/ && cd /data/pa/service
uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
(umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(32))" > /data/pa/token)
.venv/bin/python -m pytest -q          # 35 tests
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

## The dashboard (laptop)

```bash
mkdir %USERPROFILE%\.hermes-dashboard
ssh VM1 "cat /data/pa/token" > %USERPROFILE%\.hermes-dashboard\token
```

Set `PA_URL` in `laptop/dashboard/server.py` (or the environment) to `http://VM1_TAILSCALE_IP:8100`, then run
`laptop/dashboard.ps1`. It opens `http://127.0.0.1:8765` with tasks, habits, spending, reminders, the inbox and
the health of every component. The local server keeps the token out of the browser, only forwards an allow-list
of API calls, and refuses requests from other websites.
