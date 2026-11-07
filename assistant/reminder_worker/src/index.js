// Reminder worker (Cloudflare Workers + D1): stores reminders and sends them to Telegram on time,
// even while the assistant's VMs are paused. A cron trigger runs every minute.
//
// API (all except /health need "Authorization: Bearer <SYNC_SECRET>"):
//   POST /reminders            {text, due_utc (ISO), repeat: none|daily|weekdays|weekly|monthly, tz_offset_min}
//   GET  /reminders?status=pending|sent|cancelled|failed|all&limit=N
//   POST /reminders/:id/cancel
//   POST /reminders/:id/snooze {minutes}
//   PUT  /briefing             {text, time_local "HH:MM", tz_offset_min}   (agenda snapshot for the morning message)
//   GET  /health
// Secrets: SYNC_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.  Binding: DB (D1).

const REPEATS = ["none", "daily", "weekdays", "weekly", "monthly"];
const MAX_ATTEMPTS = 10;

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

function authorized(request, env) {
  const got = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  const want = env.SYNC_SECRET || "";
  if (!got || got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i); // constant time
  return diff === 0;
}

// next occurrence of a repeating reminder, computed in the user's local time (fixed offset)
function nextDue(dueMs, repeat, offsetMin) {
  const off = offsetMin * 60000;
  const local = new Date(dueMs + off); // treat UTC fields as local fields
  const step = (d) => {
    if (repeat === "daily" || repeat === "weekdays") d.setUTCDate(d.getUTCDate() + 1);
    else if (repeat === "weekly") d.setUTCDate(d.getUTCDate() + 7);
    else if (repeat === "monthly") d.setUTCMonth(d.getUTCMonth() + 1);
  };
  step(local);
  if (repeat === "weekdays") while ([0, 6].includes(local.getUTCDay())) local.setUTCDate(local.getUTCDate() + 1);
  let next = local.getTime() - off;
  while (next <= Date.now()) { // catch up if the worker missed several cycles
    const l = new Date(next + off); step(l);
    if (repeat === "weekdays") while ([0, 6].includes(l.getUTCDay())) l.setUTCDate(l.getUTCDate() + 1);
    next = l.getTime() - off;
  }
  return next;
}

function fmtLocal(ms, offsetMin) {
  const d = new Date(ms + offsetMin * 60000);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const p = (n) => String(n).padStart(2, "0");
  return `${days[d.getUTCDay()]} ${p(d.getUTCDate())} ${months[d.getUTCMonth()]} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

async function sendTelegram(env, text) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text, disable_web_page_preview: true }),
  });
  return r.ok;
}

const rowOut = (r) => ({
  id: r.id, text: r.text, repeat: r.repeat, status: r.status, attempts: r.attempts,
  due_utc: new Date(r.due_ms).toISOString(), due_local: fmtLocal(r.due_ms, r.tz_offset_min),
  last_sent_local: r.last_sent_ms ? fmtLocal(r.last_sent_ms, r.tz_offset_min) : null,
});

async function handle(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "");
  if (path === "/health") {
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM reminders WHERE status='pending'").first();
    return json({ ok: true, pending: n.n });
  }
  if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);

  if (path === "/reminders" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const text = String(b.text || "").trim().slice(0, 1000);
    const due = Date.parse(b.due_utc);
    const repeat = REPEATS.includes(b.repeat) ? b.repeat : "none";
    const off = Number.isInteger(b.tz_offset_min) ? b.tz_offset_min : 330;
    if (!text || Number.isNaN(due)) return json({ error: "text and a valid due_utc are required" }, 422);
    if (due < Date.now() - 60000) return json({ error: "due time is in the past" }, 422);
    const id = "r" + crypto.randomUUID().slice(0, 8);
    await env.DB.prepare(
      "INSERT INTO reminders (id, text, due_ms, repeat, tz_offset_min, status, attempts, created_ms) VALUES (?,?,?,?,?,'pending',0,?)"
    ).bind(id, text, due, repeat, off, Date.now()).run();
    return json(rowOut(await env.DB.prepare("SELECT * FROM reminders WHERE id=?").bind(id).first()), 201);
  }

  if (path === "/reminders" && request.method === "GET") {
    const status = url.searchParams.get("status") || "pending";
    const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10) || 50, 200);
    const q = status === "all"
      ? env.DB.prepare("SELECT * FROM reminders ORDER BY due_ms DESC LIMIT ?").bind(limit)
      : env.DB.prepare("SELECT * FROM reminders WHERE status=? ORDER BY due_ms LIMIT ?").bind(status, limit);
    const { results } = await q.all();
    return json({ count: results.length, reminders: results.map(rowOut) });
  }

  const m = path.match(/^\/reminders\/(r[0-9a-f]{8})\/(cancel|snooze)$/);
  if (m && request.method === "POST") {
    const row = await env.DB.prepare("SELECT * FROM reminders WHERE id=?").bind(m[1]).first();
    if (!row) return json({ error: "reminder not found" }, 404);
    if (m[2] === "cancel") {
      await env.DB.prepare("UPDATE reminders SET status='cancelled' WHERE id=?").bind(m[1]).run();
    } else {
      const b = await request.json().catch(() => ({}));
      const minutes = Math.max(1, Math.min(parseInt(b.minutes, 10) || 10, 10080));
      await env.DB.prepare("UPDATE reminders SET status='pending', attempts=0, due_ms=? WHERE id=?")
        .bind(Date.now() + minutes * 60000, m[1]).run();
    }
    return json(rowOut(await env.DB.prepare("SELECT * FROM reminders WHERE id=?").bind(m[1]).first()));
  }

  if (path === "/briefing" && request.method === "PUT") {
    const b = await request.json().catch(() => ({}));
    const tasks = (Array.isArray(b.tasks) ? b.tasks : []).slice(0, 60).map((t) => ({
      title: String(t.title || "").slice(0, 120), priority: String(t.priority || "medium"),
      has_time: !!t.has_time, due_ms: Number.isFinite(t.due_ms) ? t.due_ms : null,
    }));
    const time = /^\d{2}:\d{2}$/.test(b.time_local || "") ? b.time_local : "08:00";
    const off = Number.isInteger(b.tz_offset_min) ? b.tz_offset_min : 330;
    const inbox_new = Number.isInteger(b.inbox_new) ? b.inbox_new : 0;
    const habits = (Array.isArray(b.habits) ? b.habits : []).slice(0, 20).map((h) => ({
      name: String(h.name || "").slice(0, 60), schedule: String(h.schedule || "daily"),
      streak: Number.isInteger(h.streak) ? h.streak : 0,
    }));
    await env.DB.prepare("INSERT OR REPLACE INTO kv (key, value) VALUES ('briefing', ?)")
      .bind(JSON.stringify({ tasks, habits, inbox_new, time, off, updated_ms: Date.now() })).run();
    return json({ ok: true, time_local: time, tasks: tasks.length });
  }
  if (path === "/briefing/preview" && request.method === "GET") {
    const kv = await env.DB.prepare("SELECT value FROM kv WHERE key='briefing'").first();
    return json({ text: kv ? briefingText(JSON.parse(kv.value), Date.now()) : null });
  }
  return json({ error: "not found" }, 404);
}

// Build the morning message from the raw task list, bucketed relative to *now* (not when it was pushed).
function briefingText(b, now) {
  const off = b.off * 60000;
  const local = new Date(now + off);
  const endToday = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate(), 23, 59, 59) - off;
  const week = endToday + 7 * 86400000;
  const rank = { urgent: 0, high: 1, medium: 2, low: 3 };
  const show = (t) => `• [${t.priority}] ${t.title}` +
    (t.due_ms ? ` (${t.has_time ? fmtLocal(t.due_ms, b.off) : fmtLocal(t.due_ms, b.off).slice(0, 10)})` : "");
  const overdue = b.tasks.filter((t) => t.due_ms && t.due_ms < now);
  const today = b.tasks.filter((t) => t.due_ms && t.due_ms >= now && t.due_ms <= endToday);
  const soon = b.tasks.filter((t) => t.due_ms && t.due_ms > endToday && t.due_ms <= week);
  const undated = b.tasks.filter((t) => !t.due_ms).sort((a, c) => rank[a.priority] - rank[c.priority]);
  const out = [];
  if (overdue.length) out.push(`⚠️ Overdue (${overdue.length}):`, ...overdue.slice(0, 8).map(show), "");
  out.push(`📌 Due today (${today.length}):`, ...(today.length ? today.slice(0, 10).map(show) : ["• nothing due today"]), "");
  if (soon.length) out.push(`🗓 Next 7 days (${soon.length}):`, ...soon.slice(0, 8).map(show), "");
  if (undated.length) out.push("🧩 No deadline, top priority:", ...undated.slice(0, 3).map(show), "");
  const habits = (b.habits || []).filter((h) => h.schedule !== "weekdays" || ![0, 6].includes(local.getUTCDay()));
  if (habits.length) out.push("💪 Habits today: " + habits.map((h) => h.name + (h.streak ? ` 🔥${h.streak}` : "")).join(", "), "");
  if (b.inbox_new) out.push(`📥 ${b.inbox_new} new item(s) in your inbox.`);
  return out.join("\n").trim();
}

async function tick(env) {
  const now = Date.now();
  const { results } = await env.DB.prepare(
    "SELECT * FROM reminders WHERE status='pending' AND due_ms <= ? ORDER BY due_ms LIMIT 40").bind(now).all();
  for (const r of results) {
    const late = now - r.due_ms > 5 * 60000 ? ` (late: was due ${fmtLocal(r.due_ms, r.tz_offset_min)})` : "";
    const ok = await sendTelegram(env, `⏰ Reminder: ${r.text}${late}\n[#${r.id}]`);
    if (ok && r.repeat !== "none") {
      await env.DB.prepare("UPDATE reminders SET due_ms=?, last_sent_ms=?, attempts=0 WHERE id=?")
        .bind(nextDue(r.due_ms, r.repeat, r.tz_offset_min), now, r.id).run();
    } else if (ok) {
      await env.DB.prepare("UPDATE reminders SET status='sent', last_sent_ms=? WHERE id=?").bind(now, r.id).run();
    } else {
      const status = r.attempts + 1 >= MAX_ATTEMPTS ? "failed" : "pending";
      await env.DB.prepare("UPDATE reminders SET attempts=attempts+1, status=? WHERE id=?").bind(status, r.id).run();
    }
  }

  // morning briefing, once per local day at the configured time
  const kv = await env.DB.prepare("SELECT value FROM kv WHERE key='briefing'").first();
  if (!kv) return;
  const b = JSON.parse(kv.value);
  const local = new Date(now + b.off * 60000);
  const today = local.toISOString().slice(0, 10);
  const hhmm = local.toISOString().slice(11, 16);
  const last = await env.DB.prepare("SELECT value FROM kv WHERE key='briefing_last_day'").first();
  if (hhmm >= b.time && (!last || last.value !== today)) {
    const ageH = (now - b.updated_ms) / 3600000;
    const stale = ageH > 48 ? `\n\n(Task list last synced ${Math.round(ageH / 24)} days ago; changes made since may be missing.)` : "";
    if (await sendTelegram(env, `☀️ Good morning! Here's your day.\n\n${briefingText(b, now)}${stale}`)) {
      await env.DB.prepare("INSERT OR REPLACE INTO kv (key, value) VALUES ('briefing_last_day', ?)").bind(today).run();
    }
  }
}

export default {
  fetch: (request, env) => handle(request, env).catch((e) => json({ error: String(e) }, 500)),
  scheduled: (event, env, ctx) => ctx.waitUntil(tick(env)),
};
