# AI Council

A multi-agent collaboration harness where AI agents work together in a shared
conversation with voting-based consensus, like an extreme programming peer
review session. The supervisor dynamically composes a team based on task
analysis, assigns backends and models from a YAML config, and agents
debate/vote/implement through iterative rounds.

A web dashboard lets you watch conversations in real-time, post messages,
@mention specific agents, and vote on proposals — all from your browser.

## Architecture

```
                 council.yaml (config)
                 ┌────────────────────────────┐
                 │ backends: claude-code,     │
                 │   codex, ollama, copilot,  │
                 │   claude-ollama, shell     │
                 │ roles: supervisor, planner,│
                 │   architect, coder,        │
                 │   reviewer, tester,        │
                 │   researcher, security     │
                 │ consensus: majority/      │
                 │   supermajority/unanimous │
                 └─────────────┬──────────────┘
                               │
                 ┌─────────────▼──────────────┐
                 │    Council Server (council_server.py)   │
                 │  ┌──────────────────────────────────┐   │
                 │  │  Session "room" 1 (messages,     │   │
                 │  │  votes, members, SSE, persistence)│   │
                 │  ├──────────────────────────────────┤   │
                 │  │  Session "room" 2                │   │
                 │  ├──────────────────────────────────┤   │
                 │  │  Session "room" N                │   │
                 │  └──────────────────────────────────┘   │
                 │  + REST API + static file serving       │
                 └──┬──────────┬──────────┬───────────────┘
                    │          │          │
         ┌──────────▼┐  ┌───────▼───┐  ┌──▼────────┐
         │ Browser   │  │ Agent 1   │  │ Agent 2   │
         │ (human)   │  │ (coder)   │  │ (reviewer)│
         │ SSE+HTTP  │  │ HTTP      │  │ HTTP      │
         │ @mentions │  │ claude    │  │ codex     │
         │ voting    │  │           │  │           │
         └───────────┘  └───────────┘  └───────────┘
```

The server IS the bus. There is no separate bus process. Every message flows
through the server, which persists it, broadcasts to SSE listeners, and
processes votes. The human and AI agents all connect to the same server.

## Files

```
ai-council/
├── AGENTS.md               # Guide for AI agents working on this codebase
├── README.md               # Human-facing documentation (this file)
├── council.yaml            # Config: backends, roles, models, consensus
├── council_backends.py     # Extensible backend registry (CLI plugins)
├── council_bus.py          # Bus core: message/voting/prompt builder/parser
├── council_agent.py        # Config-driven agent harness (connects CLIs to server)
├── council_supervisor.py   # Dynamic team composition + task analysis
├── council_server.py       # Unified server: bus + dashboard API + SSE + persistence
├── test_council.py          # Core integration test (bus, backends, composition)
├── test_server.py           # Server integration test (API, SSE, persistence)
├── dashboard/
│   └── index.html          # Web UI: session list, live conversation, @mention, voting
├── ai-council.service       # systemd service file for persistent deployment
└── .gitignore
```

## Quick Start

### Run tests (no external tools needed)

```bash
cd ~/ai-council
python3 test_council.py    # core tests
python3 test_server.py      # server tests
```

### Start the dashboard

```bash
python3 council_server.py --port 8080
# Open http://<detected-ip>:8080 in your browser
```

The server auto-detects the bind host:
- If a WireGuard VPN or private network (10.x.x.x) is available, binds to that
- Otherwise falls back to 127.0.0.1 (localhost only)
- Use `--host 0.0.0.0` to explicitly expose to all interfaces

### Run as a persistent service

```bash
sudo cp ai-council.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ai-council
sudo systemctl start ai-council

# Manage:
sudo systemctl status ai-council
sudo systemctl restart ai-council
journalctl -u ai-council -f   # follow logs
```

Starts on boot, auto-restarts on failure, survives SSH disconnects.

### Dry run — preview team composition

```bash
python3 council_supervisor.py --task "your task" --workdir ~/repo --dry-run
```

## Dashboard Features

- **Session list** — sidebar showing all past and running sessions with status indicators
- **Live conversation** — messages stream in real-time via SSE, no page refresh
- **@mention dropdown** — type @ to get a filtered list of agents in the room; navigate with arrow keys, select with Enter/Tab or mouse click
- **Agent panel** — right sidebar showing all agents, their roles, backends, models, and online status
- **Voting banners** — open votes appear inline with approve/reject/request_changes buttons
- **Stop/start agents** — control agent processes from the UI
- **Session persistence** — past sessions with full message history are viewable after server restart
- **New session modal** — enter task, working directory, consensus rule, auto-start option

## council.yaml Configuration

### Backends (extensible — add any CLI)

```yaml
backends:
  claude-code:
    enabled: true
    command: "claude"
    prompt_mode: "argument"      # argument | stdin | file
    read_only_flags: ["-p", "--allowedTools", "Read", "--output-format", "json"]
    write_flags: ["-p", "--allowedTools", "Read,Edit,Write,Bash", "--output-format", "json"]
    models:
      - id: opus
        flag: "--model opus"
        best_for: [planning, review]
      - id: sonnet
        flag: "--model sonnet"
        best_for: [coding, general]
    auth: "oauth"

  codex:
    enabled: true
    command: "codex"
    write_flags: ["exec", "--full-auto"]
    models:
      - id: gpt-5.5
        best_for: [coding, review]
    auth: "api-key"
    auth_env_var: "OPENAI_API_KEY"

  ollama:
    enabled: false              # set true when installed
    command: "ollama"
    prompt_mode: "stdin"
    models:
      - id: glm-5.2
        flag: "glm-5.2"
        best_for: [coding, general]

  # Claude Code running an Ollama model
  claude-ollama:
    enabled: false
    command: "claude"
    env:
      ANTHROPIC_BASE_URL: "http://localhost:11434/v1"
    models:
      - id: glm-5.2
        flag: "--model glm-5.2"

  copilot:
    enabled: false              # set true when Copilot CLI installed

  # Escape hatch for any other CLI
  shell:
    enabled: true
    prompt_mode: "stdin"
```

### Roles (fixed vocabulary)

```yaml
roles:
  supervisor:   # orchestrates, gates votes, presents to human
  planner:      # breaks down tasks, writes implementation plans
  architect:    # designs system approach, evaluates tradeoffs
  coder:        # implements features, writes code (read-write)
  reviewer:     # reviews plans and code (read-only)
  tester:       # writes and runs tests (read-write)
  researcher:   # explores repo, extracts patterns (read-only)
  security:     # security-focused review (read-only)
```

Each role has: description, default_backend, default_model, can_vote,
can_propose_vote, read_only.

### Consensus

```yaml
consensus:
  rule: majority       # majority | supermajority | unanimous
  debate_rounds: 3
  revote_on_fail: true
```

### Explicit agents (optional — overrides auto-composition)

```yaml
agents:
  - id: planner-01
    role: planner
    backend: claude-code
    model: opus
  - id: coder-01
    role: coder
    backend: codex
    model: gpt-5.5
agents: null    # null = supervisor decides dynamically
```

## Dynamic Team Composition

The supervisor analyzes the task and decides:

1. **Complexity** (simple/moderate/complex) based on keywords and role needs
2. **Required roles** based on task type:
   - Security keywords (auth, vulnerability, token) → add security reviewer
   - Architecture keywords (refactor, migrate, design) → add architect
   - Testing keywords (test, coverage, TDD) → add tester
   - Research keywords (explore, investigate) → add researcher
   - Large repo (>50 source files) → add researcher
   - Complex task → add planner + second coder
3. **Backend assignment** — distributes across available backends so
   reviewer and coder use different models when possible

### Examples

```
"Fix typo in README"                    → 2 agents (coder + reviewer)
"Add API endpoint with error handling"  → 4 agents (coder, reviewer, planner, researcher)
"Migrate auth to JWT + security review" → 7 agents (security, architect, tester,
                                                 2x coder, reviewer, planner)
```

## How Agents Communicate

Each agent CLI (Claude Code, Codex, Ollama) runs in its own process. The harness
(`council_agent.py`) wraps it:

1. **Poll**: The harness polls the server for new messages and open votes
2. **Build prompt**: It constructs a prompt containing:
   - The agent's role description (from YAML)
   - The full conversation history (last 80 messages)
   - Any open votes that need a response
   - The working directory and context
3. **Execute**: It runs the agent CLI via the backend registry
4. **Parse**: It scans the response for structured actions:
   - `VOTE: <vote_id> <option> -- <rationale>` — respond to a vote
   - `PROPOSE_VOTE: <proposal> | options: <opt1, opt2>` — call a new vote
   - `DONE: <summary>` — signal completion
5. **Post**: It posts the message and any vote actions to the server
6. **Loop**: Back to step 1

The human interacts through the web dashboard:
- Post messages to the council (visible to all agents)
- @mention specific agents to direct questions or instructions
- Vote on proposals (approve/reject/request_changes)
- Stop agents when you want to give more context or change direction
- Start agents when ready to resume

## Voting and Consensus

| Rule          | Requirement                    | Result String              |
|---------------|--------------------------------|----------------------------|
| `majority`    | >50% approve                   | `approved_majority`        |
| `supermajority` | >67% approve                 | `approved_supermajority`   |
| `unanimous`   | 100% approve, no dissent       | `approved_unanimous`        |

If consensus is not reached, the vote status is `failed_no_consensus` and
the council continues debating until a new vote is called.

## Session Persistence

- Session metadata saved to `.council_data/<id>.json`
- Full message history saved to `.council_data/<id>_history.json`
- On server restart, all past sessions are loaded automatically
- Past conversations are viewable in the dashboard with full history
- Sessions can be deleted from the dashboard (or by clearing .council_data/)

## Adding a New Backend

1. Add it to `backends:` in council.yaml
2. Set `enabled: true` when the CLI is installed
3. The backend registry auto-detects availability via `shutil.which()`
4. No code changes needed — the agent harness reads the config

See AGENTS.md for detailed instructions.

## Requirements

- Python 3.11+ with PyYAML (`pip install pyyaml`)
- At least one agent CLI installed for real sessions:
  - `npm install -g @anthropic-ai/claude-code` + `claude auth login`
  - `npm install -g @openai/codex` + set OPENAI_API_KEY
  - `npm i -g opencode-ai@latest` + `opencode auth login`
- The `shell` backend is always available as a fallback
- For VPN-only access: WireGuard or any private network (10.x.x.x)