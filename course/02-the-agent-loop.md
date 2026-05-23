# Chapter 02 — The agent loop

## TL;DR

Ch.01 was one tool call. This chapter wraps that call in a loop. The model emits a tool request, your code runs it, the result goes back, and the model decides — again — whether to call another tool or stop. The hard part is not the loop body; it is the stop. Get the stop conditions wrong and you either build a chatbot that quits mid-thought or an agent that runs until your bill explodes. This chapter covers the shape of the loop, the spectrum of ways to end it, the failure modes hiding inside it, and the step boundary where every production capability — durability, observability, permissions, approvals, compaction — eventually attaches.

---

## Why this matters

A colleague hands you an agent that "works in the demo but runs forever in production." You read the code. The loop is there. The model emits tool calls. Tool results flow back. But nothing tells the model when to stop, and nothing tells the loop what to do when the model never says it is done. You add `if step > 20: break`. The loop exits — but now it exits mid-answer. You move the break to after the model's reply. It exits cleanly most of the time, but occasionally the model emits one more tool call after what looked like a final answer, and you silently miss results. You spend a day on this.

The fix is not more code. The fix is understanding that a loop has *several* ways to end, all of which need to be there, and that the model's own `stop_reason` is the primary signal — not a line counter.

---

## The concept

### One tool call is rarely enough

A simple question — "what's the weather in Tokyo" — fits in one call. A real one — "is the weather in Tokyo good for a picnic this weekend, and if so is my Saturday calendar free?" — needs at minimum a weather check, a calendar check, and a comparison. Two calls, possibly conditional on each other, possibly a third for clarification. You cannot pre-plan exactly how many calls are needed; the model must decide at each step, looking at what it has learned so far.

That is the agent loop: the Ch.01 cycle repeated, with a decision point after every round — keep going, or stop?

### The five stages

Picture a busy kitchen during service. The head chef (the model) calls out orders, the kitchen (your tools) executes them, the head chef tastes what comes back and calls out the next round — until the dish is plated and sent out. The sous-chef (your loop controller) does not decide when the dish is done. The head chef does. But if the head chef goes silent, or keeps calling out orders after the plate has already left the pass, the sous-chef needs a backup plan — a budget, a timer, a hand on the bell — to keep the kitchen from sliding into chaos.

Call the loop whatever you want — ReAct, plan-and-execute, think-act-observe. Underneath, the same five stages appear:

- **Observe.** Gather everything the model needs: the user's message, the system prompt, prior tool results, retrieved context. In practice, this is the growing message array.
- **Plan.** Call the model. It returns either tool requests, a final answer, or a question. Your code makes no decision here; the model does.
- **Act.** Execute the tool requests. One call or many — same dispatch you wrote in Ch.01, now inside a loop.
- **Reflect.** Append the tool results, with matching IDs, to the message array. The model can now see what happened.
- **Stop.** Check whether any stop condition has fired. If so, return. If not, back to Observe.

```mermaid
stateDiagram-v2
    [*] --> Observe
    Observe --> Plan : messages ready
    Plan --> Act : tool requests
    Plan --> Stop : final answer (stop_reason: end_turn)
    Act --> Reflect : results collected
    Reflect --> Stop : a stop condition fired
    Reflect --> Observe : continue
    Stop --> [*]
```

### What the loop actually carries

The loop is not just walking the message array. Between iterations it also holds:

- **Tokens spent so far** — for budget checks.
- **Step count** — for the iteration cap.
- **A short history of recent tool calls** — for doom-loop detection (below).
- **An abort token** — so the user, or another part of the system, can cancel mid-loop.
- **The system prompt** — kept byte-stable across iterations so prefix caches keep hitting (Ch.04 explains why).

You only realize how much the loop carries the first time you try to *resume* one from a crash. That is Ch.08's problem. For now, just know the message array is not the whole story.

### Stop conditions are a spectrum, not a checklist

Every production loop uses several stop conditions, layered from softest to hardest:

- **Model-driven stop.** The model returns no tool calls and a finish reason of `end_turn` (or `stop` on OpenAI-shaped APIs). This is the primary signal — the model considers itself done.
- **Explicit `final_answer` tool.** Add a `final_answer(text)` tool to the registry. Make it the only legal way for the model to submit a result. This forces intentional finishing, prevents drift into extra calls after the answer exists, and gives you a clean canonical output to log.
- **Grace call.** Some systems give the model one last turn when the budget is almost exhausted, with a message in the prompt: "you have one turn left; wrap up." The model usually closes cleanly. Without this, hard caps cut off mid-thought. OpenClaw is the clearest reference for this pattern.
- **Step cap.** A hard ceiling on iterations — typically 10–50, sometimes ~90 in long-running assistant systems. This is the safety net, not the primary stop. If your loop ends here most of the time, something is wrong upstream.
- **Token or cost cap.** Exit when total tokens or accumulated cost crosses a threshold. Return whatever has been produced, labeled as partial.

The shape on the wire:

```ts
// Minimal loop — the shape, not your final code.
for (let step = 0; step < MAX_STEPS && totalTokens < TOKEN_BUDGET; step++) {
  const response = await llm.complete({ messages, tools });
  totalTokens += response.usage.totalTokens;

  // Model-driven stop or explicit final_answer.
  if (isFinalAnswer(response)) return finalize(response);

  // Act + Reflect.
  for (const call of response.toolCalls) {
    const result = await dispatch(call.name, call.args);
    messages.push(toolResult(call.id, result));
  }
}
return partialResult(messages, "budget_exhausted");
```

Ask your agent to translate this into your stack, then add grace-call behavior so you do not silently cut off mid-thought.

### Sometimes the right answer is to compact, not continue or stop

After each step, the loop actually has three choices, not two: continue to the next iteration, stop because a condition fired, or *compact* — pause, shrink the message array, then continue. Compaction is triggered when the context window is filling up; OpenCode's session processor watches a usable-context calculation, Hermes Agent triggers it from a token-overflow check. The mechanics — what to clip, what to summarize, what to keep verbatim — belong to Ch.05. What belongs *here* is the recognition that the loop has a third lever, not just an on/off switch, and that the step boundary is where it gets pulled.

### Errors are also turns

When a tool fails or the model emits a malformed tool call, the right move — almost always — is to append the error as a `tool_result` and keep looping. The model is good at reading an error and either retrying with corrected arguments or pivoting to a different approach. Throwing an exception out of the loop is almost never the answer.

Two error classes matter:

- **Transient.** Network glitch, rate limit, model overloaded. Retry with backoff (production systems use schedules from a few seconds up to two hours). On repeated failure, fall back to a *compatible* model — one that supports the same tool schemas, has at least the context window this turn needs, and meets the task's reasoning and content-policy requirements. A fallback that lacks the primary's tool format, context size, or policy parity is not a fallback — it's a different failure mode. Hermes Agent and OpenClaw both ship configurable fallback chains; the chain definition is where compatibility is declared.
- **Permanent.** Bad credentials, schema validation failure, tool not in the registry. Surface immediately. No amount of retrying will fix it.

Every system worth studying converges on the same shape: classify the error first, then route to retry, fallback, or surface. Ask your agent to wire `classify_error(err) → action` into your loop and write the tests that prove each class routes correctly.

### Doom loops and how to catch them

The most common runaway pattern is the *doom loop*: the model calls the same tool with the same arguments three or four times in a row, gets the same useless result, and never notices it is stuck. OpenCode and Hermes Agent both ship explicit detection — the usual rule is "if the last three tool calls have identical names and identical arguments, pause and ask for permission to continue."

Byte-for-byte equality catches most cases. It does not catch slow loops where the call shape changes but no real progress is made — `read(file, offset=0)` → `read(file, offset=100)` → `read(file, offset=200)` — where the model keeps "looking" without ever finding. For those you need either a tool that tracks its own progress or a heuristic on how much the message array has grown without producing useful output. Most teams start with byte-for-byte, add a step cap, and accept that the cost budget will catch the subtler stuck states.

### Parallel tool calls in one turn

Modern providers let the model emit multiple tool requests in a single response. If the tools are independent and safe to run concurrently, you should — it cuts wall-clock latency substantially. The pattern in OpenClaw and Hermes Agent: mark each tool as `concurrency_safe: true | false`, and run safe ones on a worker pool (eight workers is a common cap) while serializing the rest. Read-only tools are safe. Anything that writes, sends, or pays is not.

### Streaming, partial deltas, and refusals

Modern providers stream the response in chunks: text tokens, reasoning blocks, tool-use blocks, finish reasons, and sometimes refusals or safety stops. The loop has to assemble these into a coherent picture before it acts. Five concerns that show up only in streaming mode:

- **Tool-call arguments arrive incrementally.** OpenAI-style streaming emits tool-call arguments as JSON-string deltas — `{"city"` then `: "Tok` then `yo"}` — across multiple events. The loop must accumulate all deltas for a given tool-call `id` before parsing and dispatching. Dispatching on a partial fragment is the most common streaming bug.
- **Malformed JSON in args.** Even after accumulation, the model can emit JSON that does not parse — a trailing comma, an unterminated string, a key without a value. Treat it like any other recoverable error: return a `tool_result` saying *"your args did not parse; here is the error; try again,"* and let the next turn correct it. The model is good at fixing its own JSON when shown the parse error.
- **Refusals as a terminal turn.** The model can decline to call a tool (or any tool) on safety grounds. Anthropic emits a `refusal` block; OpenAI surfaces a different content type or finish reason. To the loop, a refusal ends the turn with a refusal message, not a tool result. Log it; surface it to the user; do not retry blindly against the same prompt.
- **Safety stops mid-stream.** A response can be cut short by the provider's content filter — the stream ends with a `finish_reason` of `content_filter` (OpenAI) or its equivalent. Treat as a terminal failure for that turn; surface the partial output if useful; do not retry blindly (the same filter will fire again on the same input).
- **Cancellation mid-stream.** The abort token from the next subsection applies to the stream too, not just the next turn boundary. A clean cancel stops reading from the provider, closes the connection, and does not commit any half-formed tool call. Anything already dispatched gets a *"user cancelled"* tool_result.

The wire format differs across providers; the loop's response shape — accumulate, validate, dispatch, or surface — is the same everywhere.

### Interrupts and cancellation

A user pressing Ctrl-C, a timeout firing, a parent process deciding the loop has gone on long enough — all need to propagate inward. The pattern: every loop holds an abort token, every long-running step checks it, and a tripped token cleanly unwinds with a partial result rather than tearing the process down. "Interrupt arrived mid-tool-call" deserves its own thought: let the tool finish (or have it check the token itself) rather than orphaning a half-done write.

### Step boundaries are where everything attaches

The transition between Act and Reflect — after results are collected, before they are appended — is the natural checkpoint. At that moment the loop holds a complete unit of work: one plan, one set of tool calls, one set of results. Five categories of production capability hang off this boundary:

- **Durability.** Save state. Survive a crash without repeating expensive work. → Ch.08.
- **Observability.** Emit a structured trace per step: tools called, tokens used, latency, error counts. → Ch.16.
- **Permission checks.** Gate the Act phase before dispatching. → Ch.03, Ch.12.
- **Human approvals.** Pause and wait for sign-off before high-risk actions. → Ch.12.
- **Context compression.** Clip oversized results, deduplicate, summarize older turns. → Ch.05.

```mermaid
sequenceDiagram
    participant L as Loop controller
    participant M as Model
    participant P as Policy
    participant T as Tools
    participant S as State store

    L->>M: Observe + Plan (messages)
    M-->>L: tool calls or final answer
    L->>P: Permission check
    P-->>L: approve / deny / escalate
    L->>T: Dispatch approved tool calls
    T-->>L: tool results
    L->>S: Save step
    L->>L: Reflect + check stop conditions
```

The loop body is small. The boundary around it is where the production system actually lives.

---

## Real-system notes

- **OpenCode** runs the loop inside a `SessionProcessor` that streams events for every part of every step, dispatches tools through a worker pool, and triggers compaction when the context window starts to fill.
- **Hermes Agent** runs a similar loop in `run_conversation` with a max-iterations cap near 90, a credential pool that rotates API keys on rate-limit errors, and a fallback model chain for context-overflow recovery.
- **OpenClaw** is the clearest reference for graceful-stop behavior: it counts iterations, gives the model a *grace call* when the budget is one shy, and only then forces a hard stop.
- **Paperclip** does not run the inner loop itself — adapters do. Its job is the loop *around* the loop: scheduling, heartbeats, retry policies with delays from minutes to hours, liveness reviews, durable run logs.

---

## Common failure cases

The chapter above is the design. This section is what still breaks once that loop is running in production — the failures you actually get paged for — and the pattern that resolves each. They are ordered by how often they bite, not by how interesting they are: the first two go wrong on almost every agent that ships a loop; the last two start to matter once you run real traffic or call tools concurrently.

### The loop only ever stops because it hit the step cap

*The symptom in one line: runs finish, but they finish at the iteration ceiling, not because the model said it was done.*

The agent ships, the demos look fine, and the bills are higher than they should be. You open the traces and notice that most long runs ended on the **step cap** — the hard iteration ceiling — rather than on a model-driven `end_turn`. The cap is the safety net, not the primary stop; when it becomes the primary stop, every run is paying for the maximum number of model calls instead of the number the task actually needed. The cause is usually that the softer stop conditions never fire: there is no `final_answer` tool, so the model has no clean way to declare it is done, and it keeps emitting one more "let me just double-check" tool call until the counter trips.

The fix is to make the soft stops do the work and to *watch which stop fired*. Add the `final_answer` tool and make it the only legal way to submit a result, so finishing is an intentional act rather than a drift. Then make the **stop-reason a first-class metric**: emit one trace field per run — `end_turn` / `final_answer` / `step_cap` / `token_cap` / `cancelled` — and graph the distribution (Ch.16 is where this signal lives). The healthy shape is the vast majority on `end_turn` or `final_answer`; **alarm when the `step_cap` share crosses ~5–10%**, because every run in that slice is one that ran longer than it needed to. The anti-pattern that produces this failure is treating the step cap as a stop condition you *design around* instead of a fuse you *almost never blow* — if you are tuning the cap upward to "let runs finish," you are tuning the wrong knob.

### A transient blip turns into a retry storm

*The symptom in one line: the provider hiccups for thirty seconds, and your agent's load and bill spike far past what the hiccup should have cost.*

A rate limit or a `503` is supposed to be a turn, not a crash — append the error, back off, retry. But two things go wrong under real load. First, naive retries from many concurrent runs all back off on the *same* schedule and slam the provider in synchronized waves the instant the window reopens — a *thundering herd* that turns a brief blip into a sustained outage you caused. Second, retrying an expensive deep-model call several times multiplies its cost silently; a run that should have cost one call now costs four, and nothing in the happy-path metrics shows it. The uglier variant is retrying something that was never transient — a `400` for a malformed schema will fail identically every time, so retrying it just burns the budget before surfacing the same permanent error.

Three fixes the chapter gestures at but teams skip under deadline. First, **add full jitter to the backoff** — randomize each wait across the whole interval (`sleep(random(0, base * 2^attempt))`), not a fixed schedule, so concurrent runs de-synchronize instead of marching in lockstep. Second, **classify before you retry, and cap the retries**: `classify_error(err) → {retry, fallback, surface}` must route permanent errors (bad credentials, schema validation, tool-not-found) straight to *surface* with zero retries, and transient errors get a small bounded budget (3–5 attempts) before falling back to a *compatible* model — same tool format, context window, and policy parity, per this chapter's fallback rule. Third, put a **circuit breaker** in front of the provider: after N consecutive failures, stop attempting for a cooldown window and fail fast, so you stop hammering a service that is already down. Track **retries-per-successful-run** as a metric (Ch.16); a number creeping above ~0.2 means your loop is paying a retry tax it has not noticed, and a sudden spike is your early warning of an upstream degradation.

```ts
// Full jitter is the load-bearing line. A fixed schedule is what creates the herd.
async function retryTransient(call, { maxAttempts = 5, base = 1000 }) {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try { return await call(); }
    catch (err) {
      if (classify(err) !== "transient") throw err;        // permanent → surface now
      if (attempt === maxAttempts - 1) throw err;          // budget exhausted → fall back
      const ceiling = base * 2 ** attempt;
      await sleep(Math.random() * ceiling);                // jitter across the WHOLE window
    }
  }
}
```

### The loop is stuck, but the doom-loop filter never trips

*The symptom in one line: the agent is clearly spinning, yet the byte-for-byte detector reports everything is fine.*

The detector watches for the last few tool calls being *identical* — same name, same arguments. That catches the blunt case, but the expensive stuck states in production are the ones where the call shape *changes* while no real progress is made: paging through a file forever (`read(offset=0)` → `read(offset=100)` → `read(offset=200)`), re-searching with slightly reworded queries that all return nothing, or alternating between two tools that each undo the other's effect. None of those triggers byte-for-byte equality, so the loop runs until the step or cost cap finally cuts it off — which is exactly the symptom from the first failure case, now with a different root cause.

The fix is to add a **no-progress heuristic** alongside the equality check. Equality is the cheap first layer; the second layer measures whether the message array is *growing without the run getting closer to done*. The concrete signal: track novel information added per step — new bytes that are not duplicates of prior tool results (the same dedupe scan Ch.05 uses for compaction surfaces this for free), or a monotonically advancing progress field that a paginating tool reports itself. **If K consecutive steps add no novel result, treat it as a doom loop**: pause and ask for permission to continue, exactly as the equality path does. The named pattern is *progress-bounded looping* — every step must either advance a measurable quantity or it does not count as progress, and a run that stops making progress is interrupted rather than left to burn the budget. Tools that page or search should report their own progress (`results_found`, `next_offset == prev_offset`) because the loop cannot always infer it from the outside; this is the same self-tracking the chapter notes for slow loops, made into a contract the tool owes the loop.

### Parallel tool calls that were not actually safe to parallelize

*The symptom in one line: turning on concurrent tool execution made things faster and occasionally, nondeterministically, wrong.*

Running independent tool calls on a worker pool is a real latency win, and the `concurrency_safe` flag is the right gate. The failure is that the flag gets set too generously. A tool marked safe because it "only reads" actually reads *shared mutable state* that another concurrent call is writing; two calls that each look idempotent in isolation race on the same file or row and one write is lost; or the model emitted calls in a deliberate order (read-then-write, check-then-act) and the pool reordered them, so the write lands before the read it depended on. These bugs are nondeterministic by construction — they pass every test that runs the calls one at a time and only surface under the timing of real concurrent execution, which is why they are blamed on "flakiness" for weeks.

The fix is a **conservative default and an explicit data-dependency check**, not a per-tool guess. Default `concurrency_safe` to **false**; a tool earns `true` only when it is genuinely read-only *and* touches no state another tool in the same turn could be mutating. Anything that writes, sends, pays, or transitions a workflow stays serialized — this is the side-effect classification Ch.03 makes part of the tool contract, and the loop is where it is enforced. When the model emits a batch, **partition it: run the safe set concurrently, run the rest in emission order**, and never let a write float ahead of a read it might depend on. The anti-pattern is treating "the model put these in one response" as proof they are independent — emission in one turn is a latency hint, not a safety guarantee. If you cannot prove two calls are independent, serialize them; the wall-clock you lose is cheaper than a lost write you cannot reproduce.

---

## Pair with your agent

A few prompts that work well on this chapter:

- *"Translate the minimal loop pseudocode into my stack. Add the grace-call behavior and the four other stop conditions. Show me where each one fires."*
- *"Implement doom-loop detection: byte-for-byte equality on the last three tool calls. Walk me through a test case where it catches a real stuck pattern and one where it misses."*
- *"Classify these errors as transient or permanent — rate limit, schema validation failure, tool-not-found, model overloaded, expired credentials — wire `classify_error → action` into my loop, and write the tests."*
- *"Wire an abort token through my loop. Show me what happens when a user cancels mid-tool-call vs. mid-model-call, and how the partial result is shaped."*
- *"Walk me through how OpenCode's SessionProcessor decides between continue, compact, and stop. Then write the equivalent in my stack — keeping the same shape, using my idioms."*

---

## What's next

You have a loop. The next thing the loop needs is tools it can trust. Ch.03 is about the contract beyond the schema — argument validation, side-effect classification, idempotency, the difference between a recoverable error and a fatal one, and why safe paths matter more than safe code.
