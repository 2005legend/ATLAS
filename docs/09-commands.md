# 9. Command reference

Everything you can type or run once ATLAS is set up. Placeholders such as `VM1_TAILSCALE_IP` and
`HERMES_GATEWAY_UNIT` are explained in [step 1](01-overview.md).

Contents:
[talking to the bot](#talking-to-the-bot) ·
[slash commands](#telegram-slash-commands) ·
[dashboard](#dashboard) ·
[laptop scripts](#laptop-scripts) ·
[VM 1 admin](#vm-1-admin) ·
[VM 2 admin](#vm-2-admin) ·
[Cloudflare](#cloudflare-reminder-worker) ·
[assistant tools](#tools-hermes-can-use) ·
[service API](#assistant-service-api)

## Talking to the bot

You don't need commands for daily use. Write normally; the bot picks the tool. It only confirms after the
database saved something, and it repeats the exact date it stored. Times are read in your time zone: "tomorrow
10am", "in 30 minutes", "by Sunday", "next Monday 9am", "tonight", "tomorrow evening". A date without a time
means 23:59 for a task deadline and 09:00 for a reminder.

### Tasks

| Say | What happens |
|---|---|
| `Add submit assignment tomorrow 10am, high priority, college` | One task, with priority, category and deadline |
| `Tomorrow I need to finish the report, call my guide and do an hour of DSA. Add all three.` | Three separate tasks |
| `What do I have to do today?` / `What's overdue?` / `Deadlines this week?` | Lists from the database, most urgent first |
| `Mark the report as done` | Completes it (it looks up the task first) |
| `Move the guide call to Friday 5pm` / `Make DSA urgent` | Changes the task |
| `Remove the DSA task` | Cancels it |
| `Break the research experiment into: run baseline, write analysis` | Subtasks under the bigger task |

Priorities: low, medium, high, urgent. Categories: college, internship, research, personal, career, finance,
projects, other.

### Reminders

| Say | What happens |
|---|---|
| `Remind me at 8 PM to study German` | One-time reminder |
| `Remind me in 30 minutes to call home` | Relative time |
| `Remind me every day at 7am to drink water` | Daily |
| `Remind me every weekday at 9 to check email` | Monday to Friday |
| `Remind me every Sunday 6pm to review my week` | Weekly |
| `Remind me 3 days before the paper deadline` | Counts back from that task's deadline |
| `What reminders do I have?` | Upcoming reminders |
| `Snooze that 10 minutes` | Sends the latest reminder again later |
| `Cancel the German reminder` | Stops it (repeating ones too) |

Reminders arrive as `⏰ Reminder: …  [#r1a2b3c4d]`. The `#r…` id is how the bot finds the reminder when you
reply about it. Reminders and the 08:00 briefing come from Cloudflare, so they arrive even while the VMs sleep.

### Notes, ideas and the inbox

| Say | Saved as |
|---|---|
| `Save this idea: a bot that summarises arXiv papers every morning` | idea |
| `I have an idea for my SLM research: …` | idea, project "slm research" |
| `Note down this git command for deployment: git push origin main --tags` | snippet |
| `Save this link for my thesis: https://…` | link |
| `What ideas did I save for SLM research?` / `Find my note about quantisation` | Search |
| `Turn that idea into a task due next Friday` | Creates the task and marks the note processed |

### Habits

| Say | What happens |
|---|---|
| `Track DSA practice every day` | Daily habit |
| `Track German 5 times a week` / `Gym twice a week` | Weekly target |
| `Track reading on weekdays` | Monday to Friday |
| `I did DSA today` / `I worked out yesterday` | Check-in (once per day), replies with the streak |
| `How are my habits going?` | Streaks, this week's count, what's still due today |
| `Stop tracking gym` | Removes it (history is kept) |

A habit repeats; a task is done once. Weekday habits don't break their streak over the weekend.

### Expenses

| Say | What happens |
|---|---|
| `Spent ₹120 on lunch` | Logged as food, with this month's total |
| `Spent ₹120 on lunch and ₹45 on the metro` | Two entries |
| `Paid ₹500 for books yesterday` | Logged on yesterday's date |
| `How much did I spend this month?` / `this week?` | Total, by category, and the previous period |
| `Delete the ₹45 metro expense` | Removes it |

Categories: food, transport, shopping, bills, education, health, entertainment, rent, other. It is a log only;
nothing connects to a bank.

### Planning

| Say | What happens |
|---|---|
| `What's on today?` / `Give me my agenda` | Overdue, today, next 7 days, top undated tasks, new inbox items |
| `Plan my day, I have 3 hours` | A suggested order using your real tasks and deadlines |
| `I have 2 hours free, what should I work on?` | Picks from urgent and due-soon tasks |

### Projects and the weekly review

| Say | What happens |
|---|---|
| `Create a project SLM research, deadline end of November` | A project with a deadline |
| `Add "run the baseline" to SLM research as a milestone, due Friday` | A task linked to the project |
| `Show me everything pending in my research project` | Open tasks, milestones, overdue items, linked notes, progress %, days left |
| `Pause the SLM project` / `Mark it done` | Changes its status |
| `How did my week go?` | Done, missed, habits, spending, projects, focus, study, next week |

The weekly review also arrives by itself on Sundays at 18:00.

### Follow-ups and job applications

| Say | What happens |
|---|---|
| `I emailed my professor about the thesis. Remind me to follow up in four days` | Follow-up with a reminder at 09:00 that day |
| `Who am I waiting on?` | Open follow-ups, due dates, days waiting |
| `The professor replied` | Closes it and cancels the reminder |
| `Add the Amazon SDE intern role to my application tracker, applied today` | Application, status applied |
| `My Amazon interview is Thursday 3pm, technical round` | Status interview; reminders 18:00 the day before and 1 hour before |
| `Amazon rejected me` / `I got the offer` | Status change; pending interview reminders are cancelled |
| `Show my applications` | Active applications, next steps, counts per status |

### Focus sessions and sorting the inbox

| Say | What happens |
|---|---|
| `Start a 50-minute research session` | Timer; a message arrives when it ends ("take a 10-min break") |
| `I got distracted` | Counts a distraction in the running session |
| `Done, stop the timer` | Ends it and tells you the minutes |
| `How much did I focus this week?` | Minutes per topic and project |
| `Sort my inbox` | Lists unsorted captures with a suggestion each; acts only on what you confirm |
| `Turn the GPU note into a reminder for tomorrow 10am` / `Archive the meme link` / `Keep the JAX note under my sim project` | One item sorted |

A session you forget to stop is closed at its planned end an hour later.

### Learning and revision

| Say | What happens |
|---|---|
| `I'm learning ML, 3 hours a week` | Subject with a weekly goal (`30 min a day` works too) |
| `I studied ML for 1h30` | Study time; focus sessions whose topic names the subject count by themselves |
| `ML modules: linear models, trees, neural nets` / `I finished trees` | Course modules and progress |
| `How is my learning going?` | Hours this week vs goal, streak, modules, next module |
| `Remind me to revise transformers after three days` | A revision card |
| `What should I revise today?` / `I remembered it` / `I forgot it` | Gaps grow 3 → 7 → 14 → 30 → 60 days; forgetting brings it back tomorrow |

Topics due for revision show in the morning briefing as "📚 Revise today".

### Coding (the DSH worker on VM 2)

| Say | What happens |
|---|---|
| `Use DSH to write a prime checker with pytest tests and run them` | Runs in a sandbox on VM 2; you get a task id and the test output |
| `What's the status of task-…?` | Progress of a running task |
| `Fetch the files from task-… and show me the main file` | Copies them to VM 1 under `/data/workspace/dsh-results/<task>/` |
| `Cancel task-…` | Stops the sandbox |

### Memory and past conversations

| Say | What happens |
|---|---|
| `Remember that I prefer short answers` | A memory write, waiting for your approval (see `/memory`) |
| `What did I tell you earlier about the internship?` | Searches past conversations |

When the bot gets stuck it stops and asks you with buttons instead of looping. Tap one, or tap "Other" and
type your answer.

## Telegram slash commands

These are built into Hermes.

| Command | What it does |
|---|---|
| `/new` | Start a fresh conversation. Use it when you change topic; old history makes every reply slower |
| `/stop` | Stop what the bot is doing (if it doesn't react, restart the gateway, see below) |
| `/status` | Session status |
| `/usage` | Token usage |
| `/insights` | Usage insights (`/insights 7` for the last 7 days) |
| `/model` | Show or switch the model for this chat |
| `/memory pending` | Memory writes waiting for approval |
| `/memory approve <id>` / `/memory approve all` | Save them |
| `/memory reject <id>` / `/memory reject all` | Drop them |
| `/approve` / `/deny` | Answer a pending approval for a risky command |
| `/compress` | Shrink the conversation history now |
| `/goal <text>` | Give the bot a standing goal across turns |
| `/cron` | Scheduled jobs (the bot asks before creating one) |
| `/kanban <action>` | The task queue for background agent work |
| `/voice on` / `/voice off` | Voice replies |
| `/sethome` | Make this chat the place for scheduled results and notifications |
| `/commands` | Full list of commands |

## Dashboard

On the laptop: `laptop/dashboard.ps1` (or `powershell -ExecutionPolicy Bypass -File laptop\dashboard.ps1`). It opens
`http://127.0.0.1:8765`.

- Tasks: overdue, today, next 7 days, no deadline. Tick a box to complete; ✕ deletes.
- Add a task with a plain-language due time.
- Habits: tick to check in for today; shows streaks.
- Spending this month by category, with a form to log an expense.
- Reminders: upcoming, with cancel, and a form to add one.
- Inbox: capture an idea or note; search old ones.
- Projects: progress bar, days left, next milestone.
- Follow-ups & applications: open follow-ups with a "✓ done" button; application counts per status and next steps.
- Focus & learning: the running focus session with a Stop button, study time against each weekly goal, and today's
  revisions with "✓ got it" / "✗ forgot".
- System: Hermes and Telegram, DSH bridge, freellmapi, laptop Ollama, the reminder worker, VM 1 disk space.

If it says VM 1 is unreachable, run `wake-vms.ps1` and check that Tailscale is connected.

## Laptop scripts

| Command | What it does |
|---|---|
| `laptop/wake-vms.ps1` | Wakes VM 2 then VM 1, checks every service, copies the newest backups and a reminders export to `%USERPROFILE%\hermes-backups\` |
| `laptop/wake-vms.ps1 -NoBackup` | Same, without the backup copy |
| `laptop/dashboard.ps1` | Starts the dashboard and opens it |

If Windows blocks scripts: `powershell -ExecutionPolicy Bypass -File <script>`.

## VM 1 admin

Run with `export HERMES_HOME=/data/hermes`.

| Command | What it does |
|---|---|
| `hermes doctor` | Checks config, tools and dependencies |
| `hermes mcp test pa` / `hermes mcp test dsh` | Confirms the assistant and DSH tools load |
| `hermes chat --oneshot -q "…"` | One question from the shell (same tools as Telegram) |
| `hermes config get <key>` / `hermes config set <key> <value>` | Read or change settings |
| `hermes fallback list` | Model fallback order |
| `hermes pairing list` | Approved and pending Telegram users |
| `hermes pairing approve telegram <code>` | Let a new user in |
| `hermes pairing revoke telegram <user id>` | Remove a user |
| `hermes backup -o /data/backups/hermes/` | Manual full backup |
| `systemctl --user restart HERMES_GATEWAY_UNIT` | Restart the bot (also stops a stuck turn) |
| `journalctl --user -u HERMES_GATEWAY_UNIT -f` | Live gateway log |
| `tail -f /data/hermes/logs/agent.log` | Model calls and tool calls |
| `systemctl --user status pa-service` | The assistant service |
| `curl -s 127.0.0.1:8100/health` | Assistant service health and local time |
| `systemctl --user start hermes-backup.service` | Run today's backup now |
| `systemctl --user list-timers` | When the next backup runs |

## VM 2 admin

| Command | What it does |
|---|---|
| `curl -s localhost:8000/health` | Bridge, freellmapi, Ollama and DSH status, task counts |
| `sudo systemctl status dsh-bridge ollama docker nftables` | Service states |
| `sudo journalctl -u dsh-bridge -f` | Bridge log |
| `systemctl list-units 'dsh-task-*'` | Sandboxes running right now |
| `sudo /usr/local/sbin/dsh-ctl list <task-id>` | Files a task produced |
| `sudo /usr/local/sbin/dsh-ctl read <task-id> <file>` | Read one of them |
| `sudo /usr/local/sbin/dsh-ctl stop <task-id>` | Kill a task's sandbox |
| `cd /data/freellmapi/src && sudo docker compose logs -f` | freellmapi log |
| `sudo docker compose ps` | freellmapi container state |
| `curl -s 127.0.0.1:3001/api/ping` | freellmapi is up |
| `ollama ps` | Which model is loaded and how much RAM it uses |
| `sudo nft list table inet hermes_guard` | The firewall rules |
| `sudo systemctl start vm2-backup.service` | Run today's backup now |

The freellmapi dashboard (`http://VM2_TAILSCALE_IP:3001` from the laptop) is where you add provider keys, see
usage per key, and set monthly token caps.

## Cloudflare reminder worker

From `assistant/reminder_worker` on the laptop:

| Command | What it does |
|---|---|
| `npx wrangler deploy` | Deploy after changing `src/index.js` |
| `npx wrangler tail` | Live log of each cron run and request |
| `npx wrangler secret list` | Which secrets are set (not their values) |
| `npx wrangler secret put TELEGRAM_BOT_TOKEN` | Replace the bot token (type it at the prompt) |
| `npx wrangler d1 execute pa-reminders --remote --command "SELECT id, text, status FROM reminders ORDER BY due_ms DESC LIMIT 10"` | Look at stored reminders |
| `npx wrangler d1 export pa-reminders --remote --output reminders.sql` | Back them up |
| `curl -s https://pa-reminders.YOUR_SUBDOMAIN.workers.dev/health` | Pending reminder count |

## Tools Hermes can use

The bot picks these itself; you never call them by name.

**Assistant (`pa`, 50 tools):**
- Tasks and planning: `task_add`, `task_list`, `task_done`, `task_update`, `task_delete`, `agenda`, `parse_time`.
- Inbox: `idea_add`, `inbox_add`, `inbox_search`, `inbox_to_task`, `inbox_triage`, `inbox_to_reminder`, `inbox_keep`, `inbox_archive`.
- Reminders: `reminder_add`, `reminder_list`, `reminder_cancel`, `reminder_snooze`.
- Habits and expenses: `habit_add`, `habit_checkin`, `habit_list`, `habit_remove`, `expense_add`, `expense_list`,
  `expense_summary`, `expense_delete`.
- Projects: `project_add`, `project_list`, `project_overview`, `project_update`, `weekly_review`.
- Follow-ups and applications: `followup_add`, `followup_list`, `followup_done`, `application_add`,
  `application_update`, `application_list`.
- Focus: `focus_start`, `focus_stop`, `focus_distracted`, `focus_stats`.
- Learning: `learning_add`, `study_log`, `module_add`, `module_done`, `learning_progress`, `revise_add`, `revise_due`, `revise_done`.

**Coding worker (`dsh`, 8 tools):** `dsh_submit`, `dsh_wait`, `dsh_status`, `dsh_result`, `dsh_cancel`,
`dsh_history`, `dsh_list_files`, `dsh_fetch_files`.

Hermes also has its own built-in tools: web search and page fetch, terminal, files, code execution, browser,
memory, past-conversation search, todo lists, delegation to subagents, scheduled jobs and `clarify` (the
buttons).

## Assistant service API

For scripts. Base URL `http://127.0.0.1:8100` on VM 1 (or `http://VM1_TAILSCALE_IP:8100` from the laptop),
header `Authorization: Bearer <contents of /data/pa/token>`. Only `/health` works without the token.

| Method and path | Purpose |
|---|---|
| `GET /health` | Status, time zone, local time, open task count |
| `GET /system` | Health of every component (used by the dashboard) |
| `POST /parse_time` `{"text": "next Monday 9am"}` | How a time phrase will be read |
| `POST /tasks` `{"title", "priority", "category", "due", "notes", "parent_id"}` | Add a task |
| `GET /tasks?status=open&overdue=true&due_within_days=7&q=…` | List tasks |
| `PATCH /tasks/{id}` · `POST /tasks/{id}/done` · `DELETE /tasks/{id}` | Change, complete, cancel |
| `POST /inbox` `{"text", "kind", "project", "tags"}` · `GET /inbox?q=…&project=…` | Capture and search |
| `POST /inbox/{id}/to_task` | Turn an item into a task |
| `POST /reminders` `{"text", "when", "repeat"}` or `{"text", "before_task_id", "before": "3 days"}` | Add a reminder |
| `GET /reminders?status=pending` · `POST /reminders/{id}/cancel` · `POST /reminders/{id}/snooze` `{"minutes"}` | Manage reminders |
| `POST /habits` `{"name", "how_often"}` · `GET /habits` · `POST /habits/{id}/checkin` `{"day"}` · `DELETE /habits/{id}` | Habits |
| `POST /expenses` `{"amount", "category", "note", "day"}` · `GET /expenses?days=30` · `GET /expenses/summary?period=month` · `DELETE /expenses/{id}` | Expenses |
| `GET /agenda?days=7` | Overdue, today, upcoming, inbox count, done today |
| `POST /projects` `{"name", "description", "deadline"}` · `GET /projects` · `PATCH /projects/{name}` · `GET /projects/{name}/overview` | Projects |
| `GET /review` | This week's review (also sent Sundays 18:00) |
| `POST /followups` `{"person", "about", "when", "channel"}` · `GET /followups` · `POST /followups/{id}/done` (or `/cancel`) | Follow-ups |
| `POST /applications` `{"company", "role", "status", …}` · `PATCH /applications/{company or id}` `{"status", "next_step", "next_when"}` · `GET /applications` | Job tracker |
| `POST /focus/start` `{"topic", "minutes", "project"}` · `POST /focus/stop` · `POST /focus/distracted` · `GET /focus/stats?period=week` | Focus sessions |
| `GET /inbox/triage` · `POST /inbox/{id}/to_reminder` `{"when"}` · `POST /inbox/{id}/keep` `{"project"}` · `POST /inbox/{id}/archive` | Sorting the inbox |
| `POST /learning` `{"subject", "target"}` · `POST /learning/log` `{"subject", "duration"}` · `POST /learning/modules` · `POST /learning/modules/done` · `GET /learning` | Learning |
| `POST /revise` `{"topic", "first_in", "prompt"}` · `GET /revise/due?days=0` · `POST /revise/{id}` `{"remembered"}` · `POST /revise/{id}/retire` | Spaced repetition |
| `POST /briefing/push` | Send the current task list to the reminder worker now |

Example:

```bash
T=$(cat /data/pa/token)
curl -s -X POST 127.0.0.1:8100/tasks -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"title":"Renew library card","due":"by Friday","category":"personal"}'
```
