# Chapter 20 — Proactive agents

## TL;DR

Most of this course assumes a reactive shape: user message arrives, agent loop runs, response goes back. Proactive agents do work *when no user asked* — scheduled cron jobs, event-driven wakeups, watchdogs reacting to external state changes, background curation, and the rare self-initiated task. The mechanics are mostly familiar from earlier chapters (Ch.08's run state machine, Ch.13's channel adapters, Ch.15's heartbeat scheduler), but the design discipline is genuinely new: when to interrupt vs queue vs digest, how to design opt-in semantics so proactivity helps rather than annoys, the escalation ladder from notify to ask to act, the failure modes specific to work done when no one is watching, and the rule that proactivity is a permission the user grants *per category* — never a default.

---

## Why this matters

A reactive agent's worst failure is a wrong answer. A proactive agent's worst failure is one of three things: a *wrong action* no one was there to stop, a *cost spiral* no one was watching, or a *notification flood* that trains the user to ignore everything from the agent. Each is a category of incident that does not show up in synchronous request-response systems; each is the predictable failure mode if you ship proactive features without the disciplines in this chapter.

The other reason it matters: proactive features are the difference between a tool the user opens when they remember and an agent that becomes part of how the user works. A daily 9 a.m. briefing, a watchdog that flags when a deploy fails, a cron job that summarizes the week's PRs — these are the moments an agent earns its place. Done well, they compound the user's trust. Done badly, they squander it in a week.

---

## The concept

### Reactive vs proactive — when each fits

Most agents start reactive and stay reactive. Add proactive shapes only when one of these is true:

- The user has a **recurring need** that does not require their attention each time — daily reports, weekly summaries, periodic health checks.
- Something in the world **changes** and the user needs to know within minutes, not hours — a deploy failed, a metric crossed a threshold, an email arrived from a watched sender.
- The work itself is best done when the user is *not* around — background curation, eval runs, idle-window training (Ch.21 picks this up).

If none of these is true, do not add a proactive shape. *Proactivity is a feature; idle running is a cost.*

### The trigger taxonomy

Five trigger types account for almost all production proactive work:

```mermaid
flowchart LR
    Cron["Cron / schedule<br/>fixed times"] --> Loop["Agent loop"]
    Event["Event<br/>webhook, channel, file"] --> Loop
    Watchdog["Watchdog / poll<br/>check condition"] --> Loop
    Pattern["User pattern<br/>idle, calendar, behavior"] --> Loop
    Self["Self-initiated<br/>rare; tightly bounded"] --> Loop
    Loop --> Action["Observe, notify, ask, or act"]
```

- **Cron / schedule.** Fixed times — every weekday at 9 a.m., every hour on the hour. The simplest and most predictable; works for routine recurring tasks.
- **Event-driven.** A webhook fires (Ch.13), a channel message arrives, a file changes, a calendar event triggers. The most responsive; feels intelligent because it reacts to the world rather than the clock.
- **Watchdog / polling.** Agent periodically checks a condition (a price, a queue depth, a status page) and acts only when it is met. Useful when the source system does not emit events.
- **User-pattern triggered.** Agent notices a behavior pattern — user is idle, has a calendar gap, has not responded in N hours — and offers help. Hardest to get right; easiest to make annoying.
- **Self-initiated.** Rare. The agent decides on its own that something is worth doing without a trigger. Reserve for tightly-bounded, low-stakes actions (the background curator from Ch.07 is one).

Most real systems combine two or more. *Cron + event* is the most common pair: a cron job that checks something, plus event handlers that fire when something specific happens.

### Cron — the workhorse

Three things separate working cron from broken cron:

- **Persisted job definitions.** Hermes Agent stores cron jobs in `~/.hermes/cron/jobs.json`, a file the scheduler reads on each tick. Paperclip stores routines in a Postgres `routines` table that survives restarts. OpenClaw keeps them in config. The store must survive a process restart — anything else loses scheduled work when you redeploy.
- **Missed-fire policy.** What happens when a job's scheduled time passed while the process was down? Three options — *fire once on recovery* (run it now), *skip* (treat as if it ran), *fire each missed instance* (catch up by running once per missed window). Pick one explicitly; the default in many cron libraries is implementation-defined and confusing.
- **Idempotency.** A cron job that re-fires after a crash mid-execution should not do its work twice. Use a run key derived from the cron expression plus the scheduled time; deduplicate against it before executing. Ch.08's outbox pattern applies here unchanged.

```ts
// Cron job shape that survives restarts and avoids double-fire.
type CronJob = {
  id:           string;
  agent:        string;          // which agent profile runs the job
  schedule:     string;          // cron expression
  missedFire:   "skip" | "once_on_recovery" | "fire_each";
  payload:      unknown;         // what the agent should do
  enabled:      boolean;
  createdAt:    string;          // anchor for the first scheduled window
  lastFiredAt?: string;
  ownerUserId:  string;          // for tenant scoping and audit (Ch.05, Ch.15)
};

function runKey(job: CronJob, scheduledFor: Date): string {
  return sha256(`${job.id}:${scheduledFor.toISOString()}`).slice(0, 32);
}

async function maybeFireCron(job: CronJob, now: Date, ctx: SchedulerCtx) {
  // Anchor next from the last fired window or — for a never-fired job —
  // from createdAt. Computing from `now` here would silently skip every
  // window that should have fired between creation and now, which is
  // wrong for any missed-fire policy except "skip".
  const anchor = job.lastFiredAt ?? job.createdAt;
  const next   = nextScheduledTime(job.schedule, anchor);
  if (next > now) return;

  const key = runKey(job, next);

  // Atomic claim: the dedup record, the queue insert, and the lastFiredAt
  // update commit in one transaction. Without atomicity, a crash between
  // enqueue and record re-fires the job on recovery — double execution
  // of a side effect that may not be safe to repeat (Ch.08's outbox
  // pattern is the same shape, generalised).
  await ctx.db.transaction(async (tx) => {
    const claimed = await tx.dedup.tryClaim(key);   // false if key already seen
    if (!claimed) return;
    await tx.runs.enqueue({ agent: job.agent, payload: job.payload, runKey: key });
    await tx.cron.markFired(job.id, next);
  });
}
```

The anchor interacts with the missed-fire policy: `fire_each` walks forward from `createdAt` and claims a key per missed window; `once_on_recovery` claims exactly one regardless of how many windows passed; `skip` advances `lastFiredAt` to the most recent past window without firing. Per-tenant isolation matters here too: a cron job for tenant A runs against tenant A's data, billed to tenant A's budget (Ch.15), audited to tenant A's log (Ch.05). One tenant's runaway cron should never block another's.

### Event-driven wakeups

Event triggers ride on the connector layer from Ch.13. Three shapes:

- **Webhook triggers.** A platform fires an HTTP callback when an event happens — a Slack message, a Stripe event, a GitHub push. The webhook handler from Ch.13 (HMAC + dedup + 202-then-queue) hands the event to the agent loop. The agent treats it as a `ChannelEvent` — same shape as a user message, different semantic.
- **Channel-event subscriptions.** Discord WebSocket, Slack events API, IMAP push notifications. The channel adapter holds an open connection and queues events as they arrive.
- **File-system or storage watchers.** `inotify`, S3 bucket notifications, cloud storage triggers. The watcher fires when a file is created or modified; the agent inspects and decides whether to act.

The discipline that holds across all three: events go through the same queue as user messages (Ch.15), so the agent's loop, observability, and budget enforcement work uniformly. *An event is just a message the user did not type.*

### Watchdog and polling

When the source system does not emit events, the agent polls. Three rules:

- **Match the cadence to the volatility.** A price-watcher that polls every second is wasteful; a deploy-status poller that polls every hour is too slow. Pick a cadence that matches the source's rate of change and the consumer's latency budget.
- **Back off on stable.** When the watched value has not changed for a while, increase the poll interval. When it changes, drop back to the baseline. Saves the source system from unnecessary load.
- **Surface the watch itself as a metric.** Ch.16's observability pattern applies — the poller emits a span per check, a counter for *value changed*, a histogram for poll latency. A silent poller is a poller you cannot trust.

Paperclip's `scanSilentActiveRuns` (Ch.15) is a watchdog applied to the agent *itself* — checking for runs with no output over a threshold and escalating. The same pattern applied externally: agent watches a system, escalates when something drifts.

### Opt-in semantics — proactivity is a permission

The single most important rule: *proactivity is a permission the user grants per category, not a default.* The user should not have to mute their agent; they should have to opt into being interrupted.

```ts
// A coarse-grained permission record. Per category, not per message.
type ProactivePermission = {
  category:       string;        // "daily_brief", "deploy_alerts", "weekly_summary"
  enabled:        boolean;
  channel:        "inline" | "email" | "slack" | "push";
  frequencyCap?:  { count: number; per: "hour" | "day" | "week" };
  quietHours?:    { start: string; end: string; timezone: string };
  snoozeUntil?:   string;
};

// Before sending a proactive notification, check all gates.
async function shouldNotify(
  user: User,
  category: string,
  now: Date,
  ctx: ProactiveCtx,
): Promise<boolean> {
  const perm = await ctx.permissions.get(user.id, category);
  if (!perm?.enabled)                                           return false;
  if (perm.snoozeUntil && now < new Date(perm.snoozeUntil))    return false;
  if (perm.quietHours && isInQuietHours(now, perm.quietHours)) return false;
  if (perm.frequencyCap) {
    const sent = await ctx.notifyLog.countRecent(
      user.id, category, perm.frequencyCap.per,
    );
    if (sent >= perm.frequencyCap.count) return false;
  }
  return true;
}
```

Categories are coarse, not per-message — the user opts into *deploy alerts* once, not into every deploy. Channel is per-category — inline for urgent, email for digest. Frequency caps and quiet hours prevent the agent from violating implicit expectations even within an enabled category.

The honest framing: every proactive feature ships *disabled by default,* and the agent's first job for that feature is to ask the user whether they want it. *Surprise is the enemy of trust.*

### Timing intelligence — interrupt, queue, or digest

For every proactive event, three timing choices:

| Mode | When to use | Cost | Example |
|---|---|---|---|
| **Interrupt now** | High-urgency, time-bounded value | User attention | Production deploy failed |
| **Queue for next session** | Useful soon but not urgent | Small cognitive backlog | New PRs to review Monday |
| **Digest** | Useful in aggregate, low value individually | None per item | Daily email summary |

The default for most proactive features should be *digest.* Interrupt only for things the user has explicitly told you are interrupt-worthy. Even within a session, batch related notifications — five PR comments delivered together are less disruptive than five separate pings.

MetaClaw's idle-window scheduler (Ch.21's self-evolution chapter goes deeper) is timing intelligence applied to training: heavy work runs during sleep hours, keyboard idle, calendar gaps. The same principle applies to any proactive work — *do it when the user is not paying attention to anything else.*

### The escalation ladder

For any class of proactive action, the agent has four rungs to choose from:

```mermaid
flowchart LR
    Obs["Observe<br/>log + metric only"] --> Notify["Notify<br/>user sees later"]
    Notify --> Ask["Ask<br/>user decides now"]
    Ask --> Act["Act<br/>agent does it"]
```

- **Observe.** Just record the event. No user-facing surface. Useful for building the data set that informs later rungs.
- **Notify.** Surface in a digest or low-priority channel. The user sees it; nothing acts on their behalf.
- **Ask.** Surface as an active prompt. The user decides whether to act; the agent's job is to make the decision easy.
- **Act.** Agent takes the action directly. Only valid when the user has previously opted into autonomous action for this category, the action is reversible, and the audit log records it (Ch.05).

A useful rule: *start at observe, earn the right to climb.* A new proactive feature ships at observe-only until you have data that the user wants the next rung. Then notify. Then ask. Then — only with explicit opt-in and rollback discipline — act.

### Notification design and the flood problem

The most predictable failure of proactive agents is the notification flood. Three defenses:

- **Frequency caps per category.** Five Slack pings an hour is annoying; one is welcome. Cap and queue the rest into a digest.
- **Adaptive cadence.** When the user ignores N notifications in a row, slow down. Ask explicitly whether to keep this category enabled.
- **Snooze and mute as first-class actions.** Every notification carries a *quiet this until later* control. The user choosing to snooze is information — log it and let it influence the cadence.

The pattern across mature notification systems (Slack, GitHub, Linear): notifications get less attention each time the user does not engage. A proactive agent that learns from non-engagement is one the user keeps; one that does not is one they mute and forget.

### Permission and approval for unattended work

Ch.12's approval gate assumes a user is there to click. Proactive work breaks that assumption. Three policies:

- **Pre-approved categories.** Anything the user has explicitly enabled (the opt-in above) needs no further approval per execution — *provided* the action is bounded, non-destructive, and reversible. A category-level *yes* never bypasses the Ch.12 approval gate for destructive actions (delete, send, charge, deploy); those remain per-instance even within a pre-approved category. See *What NOT to make proactive* below for the residual list that always escalates.
- **Async approval.** The agent proposes the action, surfaces it through a channel that allows a deferred response (Slack, email, mobile push), waits for approval before acting. Bounded — if no response in N hours, default to *do not act* and log the timeout.
- **Default-deny.** Anything not in a pre-approved category and not asked-and-answered does not run. Period.

The trap to avoid is *implicit consent* — *"the user has been ignoring my proactive emails for a week, that means it's fine."* It does not. Lack of objection is not approval. If a category is not earning its keep, surface that to the user and ask whether to disable it.

### The "no user is watching" failure modes

Three failure classes specific to proactive work:

- **Silent errors.** A cron job has been failing for two weeks; nobody noticed because nobody runs it manually. Defense: every proactive run emits a span (Ch.16) and an alert on consecutive failures.
- **Cost spirals.** A watchdog polls every 30 seconds for a year; nobody sees the bill until it arrives. Defense: per-tenant budget gates (Ch.15) apply to proactive runs *the same as interactive ones.* Surface the trend in the cost dashboard (Ch.16).
- **Runaway loops.** A self-initiated agent spawns subagents that spawn subagents. Ch.10's recursion cap and Ch.02's step cap apply, but for proactive work the limits should be *tighter* than interactive — the user is not there to interrupt.

A useful production touch: every proactive run carries a tag (`triggered_by: cron | event | watchdog | pattern | self`) on its trace. Dashboards split by trigger type. When something goes wrong, you know whether the user kicked it off or the system did.

### What NOT to make proactive

The reverse list, by category of risk:

- **Destructive actions.** Anything that deletes, sends, charges, deploys. Always require an explicit user decision per instance, even within a pre-approved category.
- **Cross-tenant operations.** A proactive run for tenant A should never touch tenant B's data. Ch.06's namespace rule is non-negotiable.
- **Irreversible side effects.** If you cannot roll it back, do not let the agent do it on its own.
- **Anything the user has not seen first.** If a category has never been demoed to the user with their explicit *yes, please run this on its own*, it should not run on its own.

A useful rule: *if the action would make a reasonable user say "wait, what?" when they see the result, it should not have run proactively.*

---

## Real-system notes

- **Hermes Agent** is the strongest reference for file-backed cron and background-curator patterns: `~/.hermes/cron/jobs.json` with a file-locked tick scheduler, `spawn_background_review_thread` for post-turn proactive curation, and `maybe_run_curator` for idle-time skill lifecycle management. Cron jobs are scanned for prompt-injection patterns before execution — proactive runs get a tighter safety gate than interactive ones (Ch.18).
- **Paperclip** is the reference for orchestration-level proactive scheduling: heartbeat scheduler ticks every 30 seconds, `routineService.tickScheduledTriggers` fires due cron-based routines, `scanSilentActiveRuns` watchdogs detect stuck agents, retry delays escalate from 2 minutes to 2 hours. Per-company budget gates apply to all runs regardless of trigger type.
- **OpenClaw** is the reference for channel-event-driven proactive work: channel plugins hold their own subscriptions (Discord WebSocket, Slack events, Telegram polling), events go through the same gateway as user messages. Cron jobs run with full tool access by default — useful as a contrast for what *not* to do when proactive runs need a tighter trust boundary.
- **OpenCode** is mostly reactive (user-initiated coding sessions), but its session-event SSE stream and snapshot system are useful study for how to surface proactive activity to a connected UI.

---

## Common failure cases

The chapter above is the design. This section is what still breaks once that design is running unattended — the failures you get paged for, except half of them never page anyone, because the defining property of proactive work is that no user is watching when it goes wrong. They are ordered by how often they bite, not by how interesting they are: the first two go wrong on almost every agent that ships a proactive feature; the last three start to matter once you have real traffic, real tenants, or a watchdog left running for a year.

### The agent trains the user to ignore it

*The symptom in one line: the user has muted the agent's channel, and now even the one notification that mattered goes unseen.*

This is the most common proactive failure and the most quietly fatal, because nothing errors — the agent works exactly as built, and the user simply stops looking. It usually arrives as a slow drift: a new feature ships sending one ping a day, then someone adds a second category, then a deploy-alert that fires on every deploy including the green ones, and within two weeks every message from the agent reads as noise. The cause is almost always that interrupt was treated as the default delivery mode when it should have been the rare exception, and that frequency caps were configured per *feature* instead of per *user-perceived channel* — five categories each capped at "one per hour" still lands five pings an hour in the same Slack DM.

The fix is to make non-engagement a first-class signal and act on it. Cap at the *delivery surface*, not the category: budget total interrupts per user per channel per hour, and when categories compete for that budget, the lower-priority ones fall to the digest. Then instrument the thing the chapter's escalation ladder is really about — track the **engagement rate per category** (opened or acted-on ÷ delivered) and alarm when it crosses below a floor, say 10% over a rolling week. A category nobody engages with is not earning its interrupt; demote it from *interrupt* to *digest* automatically, and after a second week of silence, surface the explicit *"want to keep getting these?"* prompt the chapter describes. The anti-pattern to name and ban: *implicit consent from silence* — a week of ignored emails is not a yes, it is the user already halfway to muting you. Bias every default toward digest; you earn the right to interrupt with engagement data, never with a config flag.

### The cron job stopped firing and nobody noticed for two weeks

*The symptom in one line: the daily 9 a.m. brief just... isn't arriving, and the first person to find out is the user, not you.*

A reactive endpoint that breaks throws errors users complain about within minutes. A cron job that breaks throws nothing — the *absence* of a run is invisible unless you specifically watch for it. The boring causes dominate: the scheduler process died on a deploy and the new one came up with cron disabled; a job's timezone was stored as a fixed UTC offset so it drifted an hour at the daylight-saving boundary and started landing at the wrong local time; the persisted job store (Hermes' `jobs.json`, Paperclip's `routines` table) was reset by a migration; or the worker pool is saturated and the run got enqueued but never picked up. Every one of these produces the same symptom — no output — and none of them trips a normal error alarm.

The fix is **liveness monitoring on expected runs, not just on failures**. The chapter tells you to alarm on *consecutive failures*; that catches the job that runs and crashes, but not the job that never runs at all. Add a *heartbeat-of-expectation*: for every enabled scheduled job, record the next time it *should* fire, and alarm when that time passes by more than a grace window with no corresponding run recorded. This is dead-man's-switch monitoring — you alert on the *missing* event, the inverse of normal alerting. Two operational specifics the chapter gestures at but does not nail down: store schedules as a wall-clock time plus an IANA timezone name (`Europe/London`), never a fixed offset, so the scheduler recomputes across DST instead of silently drifting an hour twice a year; and emit a span per tick *and per skipped tick* (Ch.16) so "the scheduler is alive but decided not to fire" is distinguishable from "the scheduler is dead." A scheduler you cannot prove ticked is a scheduler you cannot trust.

### The same notification fires twice (or the same job runs twice)

*The symptom in one line: the user gets two identical 9 a.m. briefs, or a proactive action happens twice and one of them was a charge.*

Duplicate fires are the cost of every reliability mechanism you add. A retry after a transient failure, a scheduler that scaled to two replicas without a leader election, a process that crashed *after* doing the work but *before* recording that it did — each one re-runs a job the system already ran. The chapter's run-key dedup (the `runKey(job, scheduledFor)` claim) handles the single-process case cleanly, but it breaks the moment two scheduler instances tick the same second: both compute the same key, both check "have I seen this?", both see *no*, both fire. A duplicate digest is annoying; a duplicate *act*-rung action — a second email sent, a second deploy kicked off — is an incident.

The fix has two layers. First, the dedup claim must be **atomic across all schedulers**, not in-process: a single `INSERT ... ON CONFLICT DO NOTHING` (or a unique constraint, or `SELECT ... FOR UPDATE`) on the run key, in the same transaction that enqueues the run, so exactly one of the racing schedulers wins the claim and the rest no-op. This is Ch.08's atomic-claim / compare-and-swap pattern, and it is the difference between "we have a dedup table" and "dedup actually holds under concurrency." Second — and this is the layer teams skip — make the *downstream action* **idempotent** too (Ch.03's idempotency key, Ch.13's webhook dedup): pass the run key all the way through to the email send and the API call so that even if the same job somehow executes twice, the side effect commits once. Belt and suspenders, because the dedup table can fail you (a migration, a clock skew, a manual replay) and when it does, you want the side effect's own idempotency to be the thing that saves you from sending the charge twice.

```ts
// Layer 1: exactly one scheduler wins the claim, cross-process, atomically.
// The unique constraint on run_key is what makes the race safe — losers
// get a conflict and no-op instead of firing a duplicate.
await ctx.db.transaction(async (tx) => {
  const won = await tx.exec(
    `INSERT INTO cron_dedup (run_key) VALUES ($1)
     ON CONFLICT (run_key) DO NOTHING`, [key],   // 0 rows = someone else won
  );
  if (won.rowCount === 0) return;                 // not our job to fire
  await tx.runs.enqueue({ agent: job.agent, payload: job.payload, runKey: key });
});

// Layer 2: the side effect carries the same key, so a replay is a no-op.
await sendEmail({ to: user, body, idempotencyKey: key });   // commits once
```

### A watchdog quietly bills you for a year

*The symptom in one line: a poller has been running every 30 seconds since launch, and the first sign of a problem is the invoice.*

Polling and watchdog triggers are the proactive shape most prone to silent cost, because the failure mode is *steady*, not spiky — there is no crash, no error, no anomaly in the latency graph, just a constant low hum of model calls and API requests that nobody attributes to anything until finance asks what the line item is. The classic shapes: a watchdog that polls a status page every 30 seconds when the thing it watches changes twice a day; a poller left enabled after the feature it supported was deprecated; or a "back off on stable" cadence that was specified in the chapter but never actually implemented, so it polls at the baseline rate forever.

The fix is to put proactive work **under the same budget gate as interactive work, and then attribute it well enough to see it**. Per-tenant budget caps (Ch.15) must apply to proactive runs identically — Paperclip's per-company gates fire regardless of trigger type, which is the right default — so a runaway poller hits a ceiling instead of running unbounded. But a budget cap only stops the catastrophe; to catch the slow bleed you need the trigger-type tag the chapter recommends (`triggered_by: cron | event | watchdog | pattern | self`) wired into the **cost ledger**, so the cost dashboard (Ch.16) can break spend down by trigger and a "watchdog spend climbing week over week with no change in detections" trend is visible *before* the invoice. The operational metric to alarm on is **cost per useful detection**: dollars spent polling ÷ number of times the watched condition actually changed. A poller burning real money to confirm "still nothing" a million times is one whose cadence is wrong, and the *back-off-on-stable* rule from the chapter is what fixes it — but only if you measure the ratio that tells you it is broken.

### The agent did something unattended that a human would have stopped

*The symptom in one line: a proactive run took a real-world action, and when the user saw the result their reaction was "wait, what?"*

This is the rarest failure in the chapter and the most expensive when it lands, because there was no human in the loop to catch it. The setup is almost always benign: a category gets pre-approved for autonomous *act*, the action seemed bounded and reversible when it shipped, and then the world shifts under it — the data it acted on was stale, an upstream signal it trusted was wrong, or the action turned out to compose into something destructive that no single instance looked like. A reactive agent doing the same thing would have been caught at the Ch.12 approval gate; the proactive one had a category-level *yes* and ran straight through.

The fix is to keep the Ch.12 gate alive *even inside pre-approved categories*, and to lower the trust ceiling for unattended work below where you would set it for interactive work. Concretely: a category-level opt-in covers the *routine* instances of that category, but destructive, irreversible, or cross-tenant actions (the chapter's *What NOT to make proactive* list) still escalate to per-instance approval — and because no user is watching, that escalation must use **async approval with a default-deny timeout** (the agent proposes, waits N hours, and on no response does *not* act and logs the timeout). The mental shift that closes this class: *the bar for autonomous action is higher than the bar for the same action with a human watching, not lower.* The chapter's "start at observe, earn the right to climb" ladder is the discipline that prevents it — a new category does not get the *act* rung on day one, it gets *observe*, then *notify*, then *ask*, and only earns *act* after you have data that the agent's proposed actions in that category would have been the right call. If the action would make a reasonable user say "wait, what?", it never should have had the rung that let it run alone.

---

## What's next

You now have a frame for proactive design — the trigger taxonomy, the opt-in discipline, the escalation ladder, the timing modes, and the failure modes specific to work done while no user is watching. Ch.21 picks up from a related angle: instead of *the agent acting on its own*, what if *the agent improves itself on its own?* Self-evolving agents — memory consolidation, skill learning, prompt refinement, LoRA personalization — are the natural complement to proactive scheduling, with the same gating discipline and the same need for the rollback paths from Ch.07.
