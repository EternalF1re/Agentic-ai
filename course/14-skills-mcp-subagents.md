# Chapter 14 — Skills, MCP, and subagents: three shapes of one capability

## TL;DR

A capability that the model needs but does not yet have can take one of three shapes: a **skill** — instructions for the model on how to do something, written as a markdown file; an **MCP server** — an external process that exposes the capability as tools (Ch.13); or a **subagent** — a separate agent loop with its own context and result contract (Ch.10). They are not interchangeable. A skill is cheap and teaches the model *how*; an MCP server is moderate cost and isolates the *execution*; a subagent is expensive and isolates the *reasoning*. This chapter is the decision rubric, the design rules for each shape, the failure modes per shape, and how to move a capability from one shape to another as the system matures.

---

## Why this matters

The first instinct of every team building an agent that hits a new capability gap is *"spawn another agent."* Most of the time, the right answer is *"write a skill."* The second-most-of-the-time, the right answer is *"call out to an MCP server."* The full agent loop is the most powerful and the most expensive option — useful exactly when the work needs its own context and reasoning, almost never otherwise.

A team that defaults to subagents accumulates cost they cannot see (every spawn is a full model loop) and complexity they will eventually pay for (multi-agent orchestration adds failure modes that single-agent doesn't have). A team that knows the decision rubric and starts at the lightest level moves faster and ships cleaner.

---

## The concept

### The three shapes in one sentence each

- **Skill** — markdown instructions baked into the agent's prompt that teach the model how to use the tools it already has for a recurring task.
- **MCP server** — a separate process exposing tools the agent calls; the capability lives outside the agent and is reusable across many agents.
- **Subagent** — a full agent loop spawned by the parent for a bounded subtask, with its own prompt, tool set, budget, and result contract (Ch.10).

The same capability — *"review this PR"* — can take all three shapes. Pick the lightest one that fits.

### Skills — anatomy

A skill is a markdown file with YAML frontmatter and a free-form body:

```markdown
---
name: review_typescript
description: Review TypeScript code for type, async, and security issues.
version: 1.2.0
platforms: [coding-agent, code-review-bot]
prerequisites: [typescript-installed]
---

# Review TypeScript code

When reviewing TypeScript code, in this order:

1. Check public function inputs are typed.
2. Check async errors are handled (no swallowed promises).
3. Check user-controlled strings reach shell / SQL / HTML sinks safely.
4. Report findings before style comments.
5. Quote the file:line you're commenting on.

Do not invent issues. If unsure, flag *suggested review needed* and move on.
```

Five fields recur across production systems: `name`, `description`, `version`, `platforms`, `prerequisites`. The body is markdown — instructions, examples, gotchas. Hermes Agent's skill format follows the agentskills.io community convention — an emerging hub for sharing skills, not a formal published standard with a governance body. OpenClaw and OpenCode use the same shape with minor variations.

### Skills — discovery, loading, and the hub

Skills live in four places across systems:

- **Bundled** — shipped with the agent. Universal patterns, baseline behaviors.
- **User-installed** — under `~/.hermes/skills/`, `~/.openclaw/skills/`, or a workspace `skills/` directory. Per-machine or per-project.
- **Plugin-contributed** — registered by a plugin (Ch.11) at boot. Treated as user-installed but versioned with the plugin.
- **Hub-distributed** — Hermes Agent integrates with `agentskills.io`: `hermes skills install <name>` pulls a skill from the hub, the agent reads it next session. This is the marketplace pattern; expect more agents to adopt it.

Discovery is a directory scan at startup; the scanner reads the frontmatter and registers each skill. The full body is not loaded into memory at scan time — that comes later.

### Skills — progressive disclosure (in brief)

Ch.06 covered the retrieval pattern in full: a skill *index* (name + description + version) lives in the prompt every turn — a few hundred tokens regardless of how many skills exist — while the skill *body* loads on demand through a `skill_view(name)` tool. The Ch.14 angle worth restating: every entry in the index is prefix cost, every body is potential prompt injection (see the trust subsection below), and twenty crisp skills consistently outperform two hundred mostly-irrelevant ones. The Ch.06 budget rule applies — archive skills the agent has not touched in months — and the trust rules below apply to anything you index.

### Skills — curation

Skills age. A skill the agent never uses, or one that calls deprecated APIs, is worse than no skill — it pulls the model toward stale patterns. Ch.07 covered the full curator lifecycle (active → stale → archived); the skill-specific applications:

- **Active** — used in the last N days; appears in the index.
- **Stale** — not used in 30 days; still in the index but flagged.
- **Archived** — not used in 90 days; removed from the index, recoverable.

Hermes Agent's curator runs on an idle-time schedule and can do something stronger: *write new skills from successful sequences*. If the agent reliably runs three tools in the same order to handle a recurring task, the curator promotes that sequence to a skill the model can name. This is one of the more powerful patterns in production — *skills that write skills*.

### Skills — provenance, trust, and prompt-injection risk

A skill is text the agent reads as instructions every session. That makes it one of the highest-leverage attack surfaces in the whole system — a malicious skill is, mechanically, prompt injection by another name. The right default: *treat every user-installed or hub-distributed skill as untrusted until you have a reason not to.* The trust model worth pinning even while the protocols mature:

- **Provenance.** Every skill carries `name`, `version`, *and* a `source` — the URL it came from, the hub entry, the file path, or the plugin that contributed it. The install gate (Ch.12) reads `source` and decides whether to ask. Skills that come from outside the bundled set should not enter the index silently.
- **Install-time approval.** A new skill is a Ch.12 approval, the same as a new MCP server. Show the user the skill's body — every line of it — before it enters the index. *"Trust this skill from this source"* is scoped by source, version, and a fingerprint of the body; a body rewrite invalidates the trust and triggers a fresh ask.
- **Signing.** Where the hub or distribution channel supports it, verify the signature against a published key. Skill registries are early enough that signing semantics are not standardized — track the spec, sign what you can, refuse to install unsigned skills from public sources by default.
- **Body inspection.** Before adding a skill to the index, run a Ch.18 threat scan over the body — the same patterns the memory layer uses in Ch.07. A skill that contains *"ignore previous instructions"* never reaches the prompt.
- **Uninstall is one click.** If the source becomes untrusted (compromised hub, compromised author), the user must be able to remove the skill without editing files. The curator from Ch.07 owns archive; uninstall is its operational sibling.

The general rule that surprises teams the first time they think about it: *a skill is more dangerous than an MCP server*. The server's tools execute in process isolation; the skill's text executes inside your model's prompt. Treat the skill boundary at least as carefully as the MCP-trust boundary — and usually more.

### MCP servers — when to write your own

Ch.13 covered the MCP protocol. The remaining question is: *when do I write an MCP server instead of a built-in tool or a skill?* Three signals:

- **The capability lives outside the agent process** — a database, a browser, a third-party SaaS, a service in a different language or runtime. Process isolation is genuinely useful.
- **The capability is reusable across many agents** — you build it once and several different agents in your org consume it.
- **The capability needs its own credentials or trust boundary** — the MCP server holds the API key; the agent process never sees it.

If none of these is true, the lighter answer is usually a built-in tool (Ch.03) or a skill.

### MCP servers — naming, schema, auth

The design choices that matter when you do write one:

- **Single-purpose vs multi-capability.** A small, focused server (`pg-query`, `s3-list`) is easier to test, secure, and version than one server with twenty unrelated tools. Prefer many small servers over one giant one.
- **Tool naming.** The harness will namespace your tool as `mcp__<server>__<tool>` (Ch.13); pick clear short tool names since they show up in the model's prompt every turn.
- **Schemas.** Tool schemas are part of the prefix (Ch.04). Keep them tight; every optional field is prefix bytes and a chance for the model to fill them wrong.
- **Annotations.** Mark each tool's metadata explicitly via MCP's `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` — so the harness wires Ch.02 parallelism, Ch.12 approval, and Ch.08 retry safety correctly when it consumes you. The `Hint` suffix is deliberate: a consuming harness should treat these as conservative defaults a server *claims*, not assertions it has *proven* (Ch.13).
- **Auth.** Hold credentials inside the server; never accept them as tool arguments from the model. Use OAuth or an environment-mounted secret; rotate them without the agent needing to know.

### Subagents — the profile as the unit

Ch.10 covered the delegation mechanics. The thing *this* chapter cares about is the unit of extension: a subagent is best understood as a *profile* you can spawn — a named role with a fixed system prompt, a tool list, a model, a budget, and a result schema.

```ts
type SubagentProfile = {
  name:           string;       // "reviewer", "implementer", "researcher"
  description:    string;       // what the supervisor reads when picking
  systemPrompt:   string;       // role-specific instructions
  model:          string;       // often cheaper than the parent's
  toolAllowlist:  string[];     // tighter than parent's
  maxSteps:       number;
  recursionDepth: number;       // usually 1 — see Ch.10
  resultSchema:   JsonSchema;
};
```

The supervisor (Ch.10) picks profiles by name; the registry is just a map. OpenCode's built-in profiles — `build`, `plan`, `general`, `explore` — are the canonical reference. Custom profiles are how you add specialists for your project.

### Subagents — built-in profiles vs custom

A useful starting set, across production systems:

- **`explore`** — read-only tools, cheap model, returns structured findings. Safest default for *find something* tasks.
- **`build`** — full tool set with writes, expensive model. The general-purpose worker.
- **`plan`** — read-only tools, cheap model, returns a structured plan (Ch.09). Output is a plan, not an action.
- **`reviewer`** — read-only tools, takes another subagent's output as input, returns *approve* or *issues found*. Cheap insurance from Ch.10's verification pattern.

Custom profiles fit the same shape. The discipline: name the profile after the role it plays in your project, not the underlying tools. *"Database migration reviewer"* is a profile name; *"calls pg_query and write_file"* is an implementation detail.

### The decision rubric

| Dimension | Skill | MCP server | Subagent |
|---|---|---|---|
| What it adds | Instructions for the model | External tools | A separate reasoning loop |
| Cost per use | A few prompt tokens; body only when loaded | One tool-call protocol hop | A full model loop |
| Isolation | None | Process boundary | Context + tool + model boundary |
| Best for | Stable procedures the model keeps re-inventing | Capabilities outside the agent process | Bounded subtasks needing their own reasoning |
| Failure mode | Model ignores or misapplies | Server crashes, schema drift | Subagent loops, drifts, over-spends |
| Update cadence | At session start | Independent server deploys | Per agent-config change |
| Versioning | YAML frontmatter `version` | Server release | Profile definition |

Add concrete cost estimates when you can measure them in your own stack: a skill is essentially free per use after the indexing cost; an MCP tool call adds a handful of milliseconds plus serialization; a subagent run adds hundreds of milliseconds and a full model loop's token spend.

```mermaid
flowchart TD
    Q1{"Is it mostly stable<br/>procedural knowledge?"}
    Q1 -- yes --> Skill["Write a skill"]
    Q1 -- no --> Q2{"Does it need code,<br/>state, credentials,<br/>or external services?"}
    Q2 -- yes --> MCP["Write or use an MCP server"]
    Q2 -- no --> Q3{"Does it need its own<br/>multi-step reasoning<br/>and context?"}
    Q3 -- yes --> Subagent["Spawn a subagent"]
    Q3 -- no --> Local["Keep it as a built-in tool<br/>or in the parent prompt"]
```

The defaults production systems land on: skills are tried first, subagents last. If your team is reaching for subagents on most new capabilities, your skills layer is probably underdeveloped.

### The same capability three ways

A concrete example to make the rubric tangible. The capability is *"summarize a long document."*

**As a skill** — when the document is already in the agent's context and the model just needs the procedure:

```markdown
---
name: summarize_document
description: Summarize a document already in context.
version: 1.0.0
---

# Summarize document

1. State the central claim in one sentence.
2. List up to five supporting points.
3. Mention caveats from the source.
4. Keep the summary under 150 words.
Do not add unsupported opinions.
```

**As an MCP tool** — when summarization needs external processing: PDF parsing, a document store, vector lookup:

```ts
const summarizeTool = {
  name: "summarize_document",
  description: "Summarize a stored document by ID.",
  input_schema: {
    type: "object",
    required: ["documentId"],
    properties: { documentId: { type: "string" } },
  },
  // Implementation lives in the MCP server, calling private stores.
};
```

**As a subagent** — when summarization is itself a research task: many documents, conflicting evidence, iterative reading, structured synthesis:

```ts
await delegate({
  role:         "researcher",
  objective:    "Synthesize the strongest claims across these documents.",
  context:      buildContextPacket(documentIds),
  allowedTools: ["read_document", "search_documents"],
  maxSteps:     12,
  outputSchema: ResearchSummarySchema,
});
```

Three shapes, three cost profiles, three failure modes. The capability is the same; the choice depends on where the complexity lives.

### Composition: how the three combine

The three shapes are designed to compose:

```mermaid
flowchart LR
    P["Parent agent"]
    P -->|reads| SK["Skill: how to review"]
    P -->|calls| MT["MCP tool: read_file"]
    P -->|delegates| SA["Subagent: reviewer"]
    SA -->|reads| SK2["Skill: review checklist"]
    SA -->|calls| MT2["MCP tool: lint"]
```

Three patterns from production:

- **A skill that calls MCP tools.** The skill instructs the model on how to compose a sequence of MCP-wrapped tool calls. The model reads the skill, then dispatches the tools.
- **A subagent that has its own skills.** When a subagent is spawned (Ch.10), it inherits the parent's skill index by default; OpenCode lets you pass a subset. The subagent sees the same `skill_view` tool the parent does.
- **An MCP server whose tool internally runs a subagent.** A plugin wraps a subagent invocation as an MCP-exposed tool. From the outside it looks like a tool; inside it spawns a full agent loop. Useful for reusing a specialist across many agent installations without re-implementing the profile.

The three layers are not a hierarchy. You mix them per capability based on the rubric.

### Migration between shapes

Capabilities move between shapes as a system matures. Four common migrations:

- **One-shot tool sequence → skill.** If the model keeps calling the same three tools in the same order, write a skill that names the pattern. The model reaches for it directly instead of rediscovering it.
- **Skill → MCP server.** If a skill grows large or starts to need credentials or external state, lift it into a server. The skill becomes a one-line instruction *"call mcp__server__do_thing"* and the work moves out of the prompt.
- **MCP server → built-in tool.** If an MCP tool is called on every turn, the per-call protocol cost adds up. Promote it to a built-in (Ch.03) for the latency win.
- **Subagent → skill + tools.** When a subagent profile is essentially executing a procedure (not exploring), collapse it into a skill that the parent reads, executed against the parent's own tools. Saves a full model loop per invocation.

Migration is normal, not a sign of bad initial design. The shape that fit at week one is rarely the shape that fits at month six.

### Failure modes per shape

| Shape | Failure | How you notice | What to do |
|---|---|---|---|
| Skill | Model ignores it | `skill_view(name)` is never called; the model's output bypasses the skill's procedure | Tighten the description; promote a key step to a built-in tool |
| Skill | Stale guidance | Model follows outdated steps | Curator archival (Ch.07); version field; explicit deprecation |
| MCP server | Crash or timeout | Tool-result error envelope | Reconnect with backoff (Ch.13); fall back to a built-in if available |
| MCP server | Schema drift | A new `tools/list` returns a different shape | Re-list on every connect; warn the operator if a tool disappeared |
| Subagent | Loops, drifts | Step budget hits its cap; reviewer disagrees | Tighten profile's tools + system prompt; lower the budget; add a reviewer |
| Subagent | Over-spends | Token or cost budget exceeded | Budget cap (Ch.10); cheaper model for the profile |

A useful note across all three: name failures are usually the *first* sign something is wrong. A skill called `review_typescript` is harder to confuse with a different skill than `reviewer`. An MCP tool prefixed `mcp__github__create_pr` is harder to mis-dispatch than `create_pr`. A subagent named `db-migration-reviewer` is more legible to the supervisor than `subagent-7`. Naming is design.

### Plugin skills, plugin tools, plugin agents

A note on the third axis: plugins (Ch.11) can contribute any of the three shapes. A single plugin can ship:

- a **skill set** — markdown files registered into the skill index;
- an **MCP server** — bundled binary or stdio-spawned process;
- a **subagent profile** — system prompt + tool list + result schema, registered in the profile registry.

OpenClaw and Hermes Agent both have all three; OpenCode plugins extend skills and tools but not profiles. The choice within the plugin follows the same rubric — pick the lightest shape that fits the plugin's purpose.

---

## Real-system notes

- **Hermes Agent** is the richest reference for skills: full SKILL.md format compatible with `agentskills.io`, a directory scanner, a curator that promotes successful sequences to new skills, hub integration via `hermes skills install/push`, and version-aware archival.
- **OpenCode** exposes both subagent-style delegation (the `task` tool) and a `skill` tool, plus filters tools through agent permissions. The cleanest reference for the built-in profile set (`build`, `plan`, `general`, `explore`) as a starter taxonomy.
- **Paperclip** uses skills and adapters to coordinate external agent runtimes — it shows how these three primitives become operational controls at the org level: skills as instructions, adapters as the MCP-shaped boundary, agents-as-subagents in the control plane.
- **OpenClaw** shows the plugin layer most cleanly: plugins contribute skills, MCP servers, and channel adapters through one plugin SDK. Good reference for *all three shapes from one plugin*.

---

## Common failure cases

The chapter above is the rubric and the design rules. This section is what still breaks once the three shapes are live in production — the failures you actually get paged for — and the pattern that resolves each. They are ordered by how often they bite, not by how interesting they are: the first two go wrong on almost every agent that grows past a handful of capabilities; the last three start to matter once you have external servers, untrusted skill sources, or a system old enough that its shapes have ossified.

### Every new capability becomes a subagent

*The symptom in one line: the agent's token bill climbs faster than its traffic, and a flame graph shows full model loops nested inside full model loops.*

This is the single most common mistake on this chapter's topic, and the chapter's own *"Why this matters"* names the instinct behind it: the team hits a capability gap and reaches for *"spawn another agent"* every time. Each spawn is a full model loop — its own system prompt, its own context handoff, its own multi-step reasoning — for work a skill (a few prompt tokens) or an MCP tool (one protocol hop) would have handled. The cost is invisible per request and brutal in aggregate, because a subagent's spend does not show up next to the parent's; it shows up as a mysterious multiplier on the monthly invoice (Ch.17). Worse, every subagent adds the failure modes from Ch.10 — drift, over-spend, silent wrong answers — that single-agent code never had.

The fix is the chapter's *"skills first, subagents last"* default, made measurable. Track the **shape mix**: of capabilities added this quarter, what fraction landed as skills vs. MCP tools vs. subagents? A healthy system is bottom-heavy — most capabilities are skills. If subagents are the plurality, your skills layer is underdeveloped and you are paying model-loop prices for procedural knowledge. Pair that with a **subagent-spawn-per-turn ratio**: if the parent spawns a subagent on most turns, alarm on it — that is the anti-pattern of *"delegation as a fancy tool call"* (Ch.10), and the right move is usually `subagent → skill + tools` from the migration list. The discipline that prevents the regret: when a capability is proposed, walk the decision rubric *before* writing code, and require a concrete reason — its own reasoning, its own context, or a separate trust boundary — to justify each rung above a skill.

### Skills pile up in the index and the model stops following them

*The symptom in one line: you have two hundred skills, every prompt is heavier for it, and the model still solves the task its own way as if the skills weren't there.*

Skills are cheap to add, so teams add them — bundled, user-installed, plugin-contributed, hub-pulled — until the index is a few hundred entries. Two things break at once. First, every index entry is prefix bytes on every turn (Ch.04), so the whole agent gets slower and more expensive whether or not any skill is used. Second, and worse, an index full of near-duplicates and stale entries makes the model *worse at picking*: it either loads the wrong skill or, more often, ignores the index entirely and reinvents the procedure inline. The chapter's line — *"twenty crisp skills consistently outperform two hundred mostly-irrelevant ones"* — is the symptom stated as a design rule; in production it shows up as a `skill_view` call rate that is near zero for most of the index.

The fix is to measure dead weight and prune on the number, not the vibe. Instrument **per-skill `skill_view` rate** — for each indexed skill, how often the model actually loads its body when it would have been relevant. A skill that is never loaded is pure prefix tax: archive it through the Ch.07 curator (active → stale → archived), don't leave it in the index "just in case." Set an **index byte budget** the same way Ch.04 budgets the prefix, and when the index crosses it, the curation pass runs before the next session can start. The second, sneakier failure inside this one — the model ignoring a skill it *should* use — is a description problem, not a count problem: per the chapter's failure table, a skill the model never loads usually has a description that does not match how the model thinks about the task. Tighten the description first; if a step is load-bearing and the model keeps skipping it, promote that step to a built-in tool (Ch.03) where the harness can't ignore it. The anti-pattern to name out loud: treating the skill index as append-only. An index that only grows is a context leak with good intentions.

### One sick MCP server drags down capabilities that have nothing to do with it

*The symptom in one line: a single flaky external server goes down, and somehow a chunk of the agent's unrelated abilities go with it.*

Every capability you put behind an MCP server inherits that server's uptime — and how much *else* goes down when it fails is a shaping decision you made when you drew the server boundaries. Adopt one twenty-tool "kitchen-sink" server and the day it develops a memory leak or gets rate-limited, all twenty tools become unavailable at once, including the nineteen that had nothing to do with the failure. The resilience mechanics that stop one hung call from freezing a turn — the per-call timeout, the circuit breaker, the recoverable-error envelope — belong to the connector layer and are Ch.13's job. What is *this* chapter's job is the shaping choice that decides the blast radius when that breaker trips.

The fix is to **shape MCP servers single-purpose, and give load-bearing capabilities a fallback shape**. Keep each server scoped to one coherent capability (`pg-query`, not `everything-database-and-files-and-email`) so a tripped breaker takes out `pg-query` alone, not nineteen bystanders — the same "fewer, sharper" instinct this chapter applies to tools, applied to where you draw a server. For a capability the agent genuinely cannot work without, don't leave it single-sourced behind one external server: back it with a built-in tool (Ch.03) or a skill that degrades gracefully, so the breaker's open state routes to a fallback instead of a dead end. And treat a reconnect as a chance for the capability set itself to have *changed* — re-list the server's tools on reconnect (Ch.13 owns the connect/reconnect lifecycle) so a server that returns with a different schema doesn't leave the model reaching for a tool that no longer exists. The instrumentation that warns a server is heading for the breaker — per-server error rate and p95 latency — lives in Ch.13 with the rest of the connector telemetry; the decision that limits what its failure *costs* you lives here, in how you drew the server in the first place.

### A skill changes under you and quietly steers the model

*The symptom in one line: a skill the user trusted months ago now contains instructions nobody on your team wrote, and it has been shaping the model's behavior every session since.*

A skill is text the model reads as instructions every session — the chapter is blunt that this makes it *"more dangerous than an MCP server,"* because the server's tools run behind a process boundary while the skill's text runs inside the model's prompt. The production failure is supply-chain shaped: a hub-distributed or user-installed skill is rewritten upstream (a compromised author, a hijacked hub entry, or just an "update" that smuggles in a new instruction), and because the install was trusted *once*, the rewritten body flows straight into the prompt with the original blessing. Now you have prompt injection that the user authorized, that survives restarts, and that no inbound message ever carried — it lives in a file you decided to trust. The same hole, smaller, is a skill whose body was never threat-scanned at install and quietly contains an *"ignore previous instructions"* the moment it loads.

The fix is to **bind trust to a fingerprint of the body, not to the source name** (Ch.12's install gate makes this concrete). Trust is scoped by source, version, *and* a content fingerprint; a body rewrite changes the fingerprint, which invalidates the trust and triggers a fresh install-time approval that shows the user every changed line before the skill re-enters the index. Run the Ch.18 threat scan over the body at install *and on every fingerprint change*, not just the first time. Two operational additions the chapter gestures at but worth pinning: **quarantine a freshly-changed skill** for a few sessions before it can influence behavior, giving the curator or a human a window to catch a hostile diff, and keep **uninstall one click** so a source that turns out to be compromised can be pulled without editing files. The mental shift that closes this whole class: a trusted skill is trusted *content*, not a trusted *name* — re-verify the content every time it changes, because the file that survives restarts is exactly the one an attacker most wants to write to.

### The shape that fit at week one is wrong at month six, and nobody re-shapes it

*The symptom in one line: a skill has grown into a thousand-line monster that needs credentials, or an MCP tool is being called on literally every turn, and everyone just lives with it.*

The chapter says migration between shapes is *"normal, not a sign of bad initial design."* The failure is that teams know this and still never do it, because nothing tells them when a shape has outgrown itself. A skill accretes steps until it needs external state or secrets it cannot safely hold in prompt text (it should have become an MCP server). An MCP tool that started occasional gets called every single turn, paying the protocol hop each time when a built-in (Ch.03) would be a latency win. A subagent profile turns out to run the same deterministic procedure every time, paying for a full model loop where a skill plus the parent's own tools would do. None of these error; they just quietly cost more than they should, month over month, until someone profiles the system and finds the obvious in hindsight.

The fix is to make the **migration triggers measurable and review them on a cadence**, exactly the four migrations the chapter lists. Track **per-MCP-tool call frequency**: a tool called on most turns is an `MCP → built-in` candidate (promote it; pocket the per-call latency). Track **repeated tool sequences in the logs**: the same three tools in the same order, often enough, is a `tool-sequence → skill` candidate — and Hermes Agent's curator does exactly this automatically, promoting reliable sequences to named skills. Track **subagent determinism**: a profile whose runs almost never branch is a `subagent → skill + tools` candidate (collapse it; save the loop). And track **skill body size and prerequisite creep**: a skill that has grown large or started to want credentials is a `skill → MCP server` candidate. Put a quarterly *shape audit* on the calendar — classify everything in the skill index, every MCP server, every subagent profile, and flag anything in the wrong shape. The cost of staying in the wrong shape is never a single dramatic failure; it is a slow tax you stop noticing, which is exactly why it needs a scheduled review rather than a page to surface it.

---

## Pair with your agent

A few prompts that work well on this chapter:

- *"Take ten new capabilities I might add to my agent. For each, walk the decision rubric and tell me whether it should be a skill, an MCP tool, or a subagent. Justify each pick with the dimension that drove it."*
- *"Audit my current agent. Classify everything in `skills/`, every MCP server I'm calling, and every subagent profile. Flag anything that's in the wrong shape and propose a migration."*
- *"Write three versions of the *summarize a document* capability for my stack — one as a skill, one as an MCP tool, one as a subagent. Measure latency and tokens for each on the same 10 KB input."*
- *"Implement the skill index pattern with `skill_view`. Add a metric for how often the model actually calls `skill_view` per skill. Tell me which skills are dead weight in the index."*
- *"Set up a subagent profile registry with `explore`, `build`, `plan`, and one custom profile for my project. Show me the supervisor's profile-picking logic and the result schemas for each."*
- *"Spot the migration candidates in my agent's last month of logs. Which tool sequences are repeated enough to be skills? Which MCP tools are called every turn and should be built-ins? Which subagent profiles are essentially deterministic and should collapse to skills?"*
- *"Write a plugin that contributes all three shapes: one skill, one MCP tool, one subagent profile. Verify each registers cleanly and the agent can use all three in one session."*

---

## What's next

You now know the unit of extension. Ch.15 moves to the *backend* that keeps the harness running at scale — queues, streaming endpoints, durable side-effect machinery, and the runtime that hosts the loop, the memory, the persistence, and the connectors when there are more than one user and more than one session in flight.
