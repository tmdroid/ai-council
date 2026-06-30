# AI Council — Phased Workflow Engine

## Brainstorming Session (June 30, 2026)

### The Problem

The current AI Council is a flat chat room. All agents join one session, talk
simultaneously, and debate everything at once. There's no structure — a reviewer
reviews code that doesn't exist yet, a security agent analyzes a plan that hasn't
been agreed, everyone steps on each other's toes.

The supervisor is passive — it posts a task and waits. Agents don't have clear
roles in a workflow, they don't know what phase the work is in, and they can't
coordinate effectively.

### The Vision

The supervisor becomes an **active orchestrator** — an AI agent itself that
reasons about the task, decides what phases are needed, spawns the right agents
for each phase, monitors debate, calls votes, transitions between phases, and
declares completion.

Not a hardcoded pipeline. The orchestrator analyzes each task and composes a
custom phase list. A "fix a typo" task might be just execute → done. A "upgrade
all libraries" task might be explore → research → verify → plan → review plan →
execute → code review → deliver. The orchestrator decides.

### How Communication Works (Low Level)

**Context flow between phases:**

An agent spawned in phase 3 gets context from phases 1 and 2 through the
session's message history. When the orchestrator spawns a new agent, the agent
polls the server for all messages since timestamp 0. The `build_prompt()`
function assembles the full conversation into the prompt.

But raw history is noisy. A phase 3 agent doesn't need every debate detail — it
needs the outcome. The orchestrator posts a **phase summary** at the end of each
phase:

```
[supervisor] PHASE COMPLETE: exploration
Summary: The app uses Ktor 3.1.2, SQLDelight 2.2.1, Kotlin 2.1.20.
Key files: libs.versions.toml, build.gradle.kts
Next phase: planning
```

The phase 3 agent's prompt includes this summary plus the full history. The
summary gives it the digest; the history is there if it needs to dig deeper.

**How the orchestrator reads subagent progress:**

The orchestrator is itself an OllamaClaudeCodeAgent. It polls the server just like
any other agent — it sees new messages as they come in. But unlike a regular
agent that responds to messages, the orchestrator's prompt is different:

```
You are the ORCHESTRATOR. Current phase: exploration.
Phase goals: Understand current library versions and architecture.
Current agents: researcher-01 (thinking since 30s ago)
Recent messages: [last 5 messages]
```

The orchestrator reads the conversation, checks if the phase goals are met, and
decides what to do next. It's an agent watching other agents.

**How the orchestrator decides when to transition phases:**

Decision loop:
1. Poll for new messages (like any agent)
2. Read the current phase goals
3. Read recent messages
4. Ask itself (via OllamaClaudeCodeAgent.send()): "Have the phase goals been met?"
5. If yes → post "PHASE COMPLETE" summary, stop current agents, start next phase
6. If no → wait and poll again

The orchestrator calls Claude Code with a prompt like:

```
You are the council orchestrator. Current phase: exploration.
Phase goals: Identify current library versions and dependencies.
Messages so far:
  [researcher-01] I found the following versions: Ktor 3.1.2, ...
  [researcher-01] DONE: Exploration complete.

Have the phase goals been met? Reply with exactly:
PHASE_COMPLETE: <summary>
or
PHASE_CONTINUE: <reason>
```

If the researcher said DONE, the orchestrator sees that and says PHASE_COMPLETE.
If the researcher is still working (no DONE signal, messages still flowing), the
orchestrator says PHASE_CONTINUE.

**How the orchestrator stops agents:**

Already built — `session.stop_agents()` terminates all agent processes. The
orchestrator calls this, then spawns new agents for the next phase. But stopping
should be graceful when possible:

1. Orchestrator posts: "PHASE TRANSITION: exploration → planning. Wrap up and
   signal DONE."
2. Wait a reasonable timeout (30s).
3. If agents signal DONE, great. If not, force-stop them.

**How the orchestrator stays the boss:**

Two mechanisms:

1. **Prompt authority** — the orchestrator's system prompt makes clear it's the
   boss:
   ```
   You are the ORCHESTRATOR of this council. You decide phases, spawn agents,
   and approve transitions. Other agents work WITHIN the phase you've defined.
   They do not decide phase transitions. You are not a peer — you are the
   supervisor.
   ```

2. **Protocol enforcement** — agents can only post messages and votes. They
   can't spawn agents, change phases, or stop other agents. Those are
   orchestrator-only actions, enforced by the server API. An agent can say "I
   think we should move to execution" but only the orchestrator can actually
   call `set_phase("execution")`. An agent that goes rogue is just posting
   messages — the orchestrator reads them and decides whether to incorporate or
   ignore them.

### Example Workflow (End to End)

```
Human: "Upgrade all libraries to latest versions"
  ↓
Orchestrator reads task → reasons about phases
  ↓
Orchestrator posts: "Proposed phases:
  1. Explore current deps
  2. Research latest versions online
  3. Verify compatibility
  4. Plan upgrade
  5. Review plan
  6. Execute
  7. Code review"
  ↓
Human: approves (or edits)
  ↓
PHASE 1 (explore):
  Orchestrator spawns researcher-01
  Researcher reads libs.versions.toml, posts versions
  Researcher signals DONE
  Orchestrator reads DONE → posts phase summary → stops researcher
  ↓
PHASE 2 (research):
  Orchestrator spawns researcher-02 (different agent, sees phase 1 summary)
  Researcher-02 searches online for latest versions
  Posts: "Ktor 3.5.1 available, SQLDelight 2.3.2, Kotlin 2.4.0"
  Signals DONE
  Orchestrator → phase summary → stops researcher-02
  ↓
PHASE 3 (verify compatibility):
  Orchestrator spawns architect-01
  Architect reads phase 1 + 2 summaries, checks compatibility
  Posts: "Ktor 3.5.1 needs Kotlin 2.3+ — we're on 2.1.20, need to upgrade
  Kotlin first. SQLDelight 2.3.2 is compatible."
  Signals DONE
  Orchestrator → phase summary
  ↓
PHASE 4 (plan):
  Orchestrator spawns planner-01
  Planner reads all phase summaries
  Posts: "Upgrade order: 1. Kotlin 2.1.20→2.4.0, 2. Ktor, 3. SQLDelight,
  4. Decompose, 5. Koin. Run tests after each."
  ↓
PHASE 5 (review plan):
  Orchestrator spawns reviewer-01
  Reviewer reads the plan, debates with planner
  "Kotlin 2.4.0 might break Compose MP 1.10 — need 1.11+"
  Planner: "Good catch, let me update the order"
  They vote → consensus → orchestrator transitions
  ↓
PHASE 6 (execute):
  Orchestrator spawns coder-01 (or multiple coders)
  Each coder gets a subtask from the plan
  Coder implements, runs tests, posts progress
  If coder finds issue: "Kotlin 2.4.0 breaks WasmJs target"
  Orchestrator reads this → decides: "Pause execution, spawn architect
  to evaluate WasmJs compatibility"
  Architect reports back → orchestrator: "OK, use Kotlin 2.3.0 instead"
  Coder continues
  ↓
PHASE 7 (code review):
  Orchestrator spawns reviewer-02
  Reviews the changes, debates with coder
  Finds issue → coder fixes → reviewer approves
  ↓
Orchestrator: "TASK COMPLETE. All phases done."
```

### Implementation Plan — Small Increments

Each increment is useful on its own and builds on the previous one.

#### Increment 1: Phase state + supervisor proposes phases

Add a `phase` field to SessionRoom. Make the supervisor an active
OllamaClaudeCodeAgent that reads the task, reasons about what phases are needed,
and posts a "phase plan" to the council. No spawning, no transitions — just the
plan. The human can approve or edit it.

**Why first:** Everything builds on phases existing. The supervisor reasoning
about the task is the brain — without it, nothing else works.

**Files to change:**
- `council_server.py` — add `phase` field to SessionRoom, add `/api/sessions/<id>/phase` endpoint
- `council_orchestrator.py` (NEW) — the orchestrator agent loop
- `dashboard/index.html` — show current phase in the session header

#### Increment 2: Spawn agents per phase

The orchestrator doesn't just say "we need phase 1" — it spawns the right agent
for that phase. For exploration: one researcher. For planning: one planner. For
plan review: one reviewer + the planner stays. Agents from previous phases
retire; new agents join. The conversation history persists.

**Why second:** Once phases exist, we need agents working within them. This is
where the orchestrator becomes an active manager, not just a planner.

**Files to change:**
- `council_orchestrator.py` — add `spawn_for_phase()` that creates agents based on the current phase
- `council_server.py` — add server-side support for phase-scoped agent spawning

#### Increment 3: Phase transition logic

When does a phase end? Three triggers:
- Consensus vote (agents vote to move on)
- Orchestrator decision (reads conversation, decides goals are met)
- Human override (click "Next Phase" in dashboard)

The orchestrator's decision loop: poll → read messages → ask "are phase goals
met?" → if yes, post phase summary, stop agents, transition.

**Why third:** The transition is what makes phases real. Without it, it's just a
label. With it, the workflow actually progresses.

**Files to change:**
- `council_orchestrator.py` — add `check_phase_transition()` decision loop
- `council_server.py` — add `transition_phase()` that stops agents, posts summary, sets new phase
- `dashboard/index.html` — add "Next Phase" button for human override

#### Increment 4: Subtask breakdown and parallel execution

After the plan is agreed, the orchestrator (or planner) breaks it into
subtasks. Each subtask gets assigned to a coder. Multiple coders work in
parallel. Each posts progress. The orchestrator monitors all of them.

**Why fourth:** This is where execution becomes structured. Instead of one coder
doing everything, work is parallelized with clear ownership.

**Files to change:**
- `council_orchestrator.py` — add `break_into_subtasks()` and `assign_subtasks()`
- `council_server.py` — add subtask tracking to SessionRoom

#### Increment 5: Issue escalation during execution

A coder finds something that conflicts with the plan. Instead of implementing
wrong code, the coder raises an issue. The orchestrator decides: fix the plan,
or tell the coder to work around it. This is negotiation, not just reporting.

**Why fifth:** This is where the council becomes truly adaptive. Plans change
based on reality. The orchestrator mediates between "what we planned" and "what
we found."

**Files to change:**
- `council_agent.py` — add `RAISE_ISSUE:` action parsing
- `council_orchestrator.py` — add `handle_issue()` that reads the issue, reasons, and responds

#### Increment 6: Dynamic phase composition

Not every task needs all phases. The orchestrator analyzes the task and proposes
a custom phase list. This is already partially done in Increment 1, but here we
make it truly dynamic — the orchestrator can add or skip phases based on what
happens during execution. E.g., if compatibility check reveals no issues, skip
"deep architecture review" phase.

**Why sixth:** This makes the workflow adaptive, not a fixed pipeline.

#### Increment 7: Repetition loop prevention

Detect when agents are stuck in a loop ("my position is unchanged" for N rounds)
and auto-trigger consensus or phase transition. The orchestrator reads the
conversation and detects steady-state.

**Why seventh:** Quality of life. Without it, sessions can run forever with
agents repeating themselves.

### Current Architecture (What We Have)

```
council_server.py          — Flask + Socket.IO server (the bus)
council_agent.py            — Agent harness (polls server, calls CLI, posts response)
cli_agent.py                — Abstract CLIAgent base class (ABC)
cli_agents/
  ollama_claude_code.py     — OllamaClaudeCodeAgent (Claude Code + Ollama, --continue)
  ollama.py                 — OllamaCLIAgent (plain Ollama, text-only)
  claude.py                 — ClaudeCLIAgent (Claude Code with Anthropic subscription)
  codex.py                  — CodexCLIAgent (Codex with ChatGPT subscription)
  generic.py                — GenericCLIAgent (any CLI)
council_bus.py              — BusClient, build_prompt(), parse_response()
council_supervisor.py       — Dynamic team composition (analyze_task, compose_team)
council_backends.py         — Backend registry for one-shot subprocess mode
council_tools.py            — ReAct tool-use loop (fallback for non-agentic backends)
council_tmux.py             — Lower-level tmux helpers
council.yaml                — Config: backends, roles, consensus rules
dashboard/index.html        — Web UI
ai-council.service           — systemd service
test_council.py             — Core tests
test_server.py              — Server tests
```

### What's Already Working

- Server runs as systemd service on VPN (http://power.vpn:8080)
- Socket.IO for real-time dashboard updates
- OllamaClaudeCodeAgent: Claude Code + Ollama, context via --continue flag
- Agents autonomously read files, run commands, grep codebase
- First successful agentic council: 2 agents found 7 real bugs with line numbers
- Session persistence, voting, @mention, agent panel
- Abstract CLIAgent ABC with concrete implementations in separate files

### Key Design Decisions

1. **Orchestrator is an active agent, not a message poster.** It uses
   OllamaClaudeCodeAgent to reason about the task, monitor progress, and make
   decisions. It's the brain.

2. **Phases are data, not code.** The `phase` field on SessionRoom is just a
   string. The orchestrator sets it. The dashboard shows it. Agents check it.
   No hardcoded phase pipeline.

3. **Context flows through message history.** New agents in new phases see all
   previous messages. Phase summaries provide digests. No separate context store.

4. **Agents can only talk.** The orchestrator is the only entity that can spawn
   agents, change phases, and stop agents. This is enforced by the server API.

5. **The orchestrator uses the same CLIAgent interface.** It's an
   OllamaClaudeCodeAgent with a special system prompt. No separate code path —
   it just has different instructions and different API permissions.

6. **Not every task needs all phases.** The orchestrator analyzes the task and
   proposes a custom phase list. The human can approve or edit.

7. **No hardcoded iteration counts.** The orchestrator decides when a phase is
   done based on conversation state, not a counter. A debate might take 3 rounds
   or 10 — the orchestrator reads the conversation and decides.