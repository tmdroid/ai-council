# AGENTS.md — Guide for AI Agents Working on AI Council

## What This Project Is

AI Council is a multi-agent collaboration harness. AI agents (powered by
different CLI tools — Claude Code, Codex, Ollama, etc.) connect to a unified
server that acts as both the message bus and the dashboard backend. Agents
debate plans, vote on proposals with consensus rules, implement code, and
review each other's work in iterative rounds — like an extreme programming
peer review session.

The supervisor dynamically analyzes the task and composes a team. A YAML
config declares which CLIs are available, what models they offer, and which
roles they fill. A web dashboard lets the human watch conversations in
real-time, post messages, @mention agents, and vote on proposals.

## Repository Structure

```
ai-council/
├── AGENTS.md               # This file — guide for AI agents
├── README.md               # Human-facing documentation
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

## Key Concepts

### Server (council_server.py)

The server IS the bus. There is no separate bus process. Each session is a
"room" (SessionRoom) with its own message history, members, and votes. All
members — the human via the web dashboard, AI agents via HTTP — connect to
this same server. Every message flows through the server which:

1. Validates the sender is a registered member
2. Persists the message to disk immediately
3. Broadcasts to all SSE listeners (real-time dashboard updates)
4. Processes votes and consensus rules

### Backends

A backend is a CLI tool that can receive a text prompt and produce a text
response. Examples: `claude` (Claude Code), `codex` (OpenAI Codex), `ollama`
(Ollama), `copilot` (GitHub Copilot CLI). The backend registry builds the
subprocess command, handles prompt passing (argument/stdin/file), and parses
JSON output. Backends are config-driven — add one by editing council.yaml.

### Roles

A role is a function an agent performs in the council. The fixed vocabulary:
`supervisor`, `planner`, `architect`, `coder`, `reviewer`, `tester`,
`researcher`, `security`. Each role has a description (used as the system
prompt), a default backend, a default model, and permissions (can_vote,
can_propose_vote, read_only).

### SessionRoom

A SessionRoom is the bus state for one session — members, messages, votes,
persistence, and SSE clients. The human joins as "supervisor" and can post
messages, @mention agents, vote on proposals, and stop/start agents. AI
agents join with their assigned role and interact via HTTP.

### Supervisor

The supervisor (typically Hermes, but can be any coordinator) analyzes the
task description using keyword detection, assesses complexity, checks repo
size, and decides which agents to spawn. It then starts the agents, posts
the task, and the council begins debating.

## How to Work on This Codebase

### Running Tests

```bash
cd ~/ai-council
python3 test_council.py    # core tests (bus, backends, composition, prompt builder)
python3 test_server.py      # server tests (API, SSE, persistence)
```

Tests require no external CLIs — they use in-process server instances and
mock backends. All tests must pass before committing.

### Dry Run (Preview Team Composition)

```bash
python3 council_supervisor.py --task "your task" --workdir /path/to/repo --dry-run
```

This shows what agents the supervisor would spawn without actually running
them. Useful for validating changes to task analysis or team composition.

### Running the Server

```bash
python3 council_server.py --port 8080
```

The server auto-detects the bind host:
- If a private network (10.x.x.x, 172.16.x, 192.168.x) is available, binds to that
- Otherwise falls back to 127.0.0.1 (localhost only)
- Use `--host 0.0.0.0` to explicitly expose to all interfaces (not recommended)

### Running as a systemd Service

The repo includes `ai-council.service` for persistent deployment:

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

The service starts after WireGuard (wg-quick@wg0) so the VPN IP is available,
and auto-restarts on failure.

### Code Style

- Python 3.11+ (stdlib only for the server and agent harness; PyYAML for config)
- No external dependencies beyond PyYAML
- Type hints encouraged but not enforced
- Docstrings on all public functions and classes
- 4-space indentation
- Keep functions focused — if a function exceeds ~60 lines, consider splitting

### Commit Messages

Format: `type: short description`

Types: `feat` (new feature), `fix` (bug fix), `refactor` (code restructure),
`docs` (documentation), `test` (tests), `chore` (config, cleanup).

Include a body explaining what changed and why. Reference the specific
component (server, agent, supervisor, backend, config, dashboard).

## How to Add a New Backend

Adding a new CLI backend (e.g., a new coding agent CLI) requires only a YAML
change in `council.yaml` — no Python code changes needed.

### Step 1: Add the backend to `council.yaml`

```yaml
backends:
  your-cli:
    enabled: true
    command: "your-cli"                    # the executable name
    prompt_mode: "argument"                # "argument" | "stdin" | "file"
    read_only_flags: ["run", "--read-only"]  # flags for read-only mode
    write_flags: ["run", "--write"]          # flags for read-write mode
    extra_flags: ["--timeout", "60"]         # flags added to every invocation
    models:
      - id: model-a
        name: "Model A Display Name"
        flag: "--model model-a"              # how to select this model
        best_for: [planning, review]         # tags for role matching
      - id: model-b
        name: "Model B Display Name"
        flag: "--model model-b"
        best_for: [coding]
    auth: "api-key"                          # "oauth" | "api-key" | "env-var" | "none"
    auth_env_var: "YOUR_CLI_API_KEY"         # if auth is env-var or api-key
    env:                                     # environment variables to set
      YOUR_CLI_ENDPOINT: "https://api.example.com"
    notes: "Optional notes about this backend"
```

### Step 2: How prompt_mode works

- `argument`: The prompt is passed as the last CLI argument.
  Command built as: `your-cli <flags> <prompt>`
  Example: `claude -p 'review this code' --allowedTools Read`

- `stdin`: The prompt is piped to the CLI's stdin.
  Command built as: `your-cli <flags>`, prompt via `subprocess.run(input=prompt)`
  Example: `echo 'review this' | ollama run glm-5.2`

- `file`: The prompt is written to a temp file, and the file path is passed
  as the last argument. Useful for CLIs with argument length limits.
  Command built as: `your-cli <flags> /tmp/prompt_abc123.md`

### Step 3: How read-only vs write flags work

When an agent is assigned a read-only role (reviewer, researcher, etc.), the
`read_only_flags` are used. When the role is read-write (coder, tester), the
`write_flags` are used. The backend registry builds the command by combining:
`[command] + (read_only_flags or write_flags) + extra_flags + [model_flag] + [prompt]`

### Step 4: Verify

```bash
python3 test_council.py
python3 council_supervisor.py --task "test task" --dry-run
```

The backend registry auto-detects availability via `shutil.which(command)`.
If the CLI isn't installed, the backend is marked unavailable and the
supervisor falls back to other backends.

## How to Add a New Role

### Step 1: Add the role to `council.yaml`

```yaml
roles:
  your-role:
    description: "Detailed description of what this role does. This becomes
      the system prompt for the agent, so be specific about its
      responsibilities, what to check, and how to communicate."
    default_backend: claude-code
    default_model: sonnet
    can_vote: true
    can_propose_vote: false
    read_only: true
```

### Step 2: Add task analysis triggers (if the role should be auto-detected)

In `council_supervisor.py`, add keyword detection in `analyze_task()`:

```python
your_keywords = ["keyword1", "keyword2"]
if any(kw in task_lower for kw in your_keywords):
    needs.append("your-role")
    rationale.append("Your-role task detected — adding your-role")
```

### Step 3: Update the tests

Add a test case in `test_council.py` that verifies the keyword detection
triggers the new role.

## How to Modify the Voting System

The voting logic lives in `council_server.py`, class `SessionRoom`:

- `propose_vote()`: Creates a vote with options (default: approve/reject/request_changes)
- `respond_vote()`: Records an agent's response with rationale
- `_check_vote_completion()`: Closes the vote when all members have responded
- `_tally_result()`: Applies the consensus rule (majority/supermajority/unanimous)

The same voting logic also exists in `council_bus.py` (CouncilBus class) for
backwards compatibility with the standalone bus.

To add a new consensus rule:

1. Add it to the `_tally_result()` method in `SessionRoom` (council_server.py)
2. Add it to the `_tally_result()` method in `CouncilBus` (council_bus.py)
3. Add it to the `--consensus` argparse choices in both files
4. Update `council.yaml` consensus.rule documentation

## How to Modify the Prompt Builder

`build_prompt()` in `council_bus.py` constructs the text sent to each agent
CLI. It includes:

- Role name and description (from YAML)
- Full conversation history (last 80 messages)
- Open votes that need responses
- The agent's available actions (based on can_vote, can_propose_vote)
- Working directory and extra context

To change what agents see, modify `build_prompt()`. Keep prompts under
~4000 characters to avoid context window issues with smaller models.

## How to Modify the Response Parser

`parse_response()` in `council_bus.py` extracts structured actions from the
agent's free-text response:

- `VOTE: <vote_id> <option> -- <rationale>` — vote response
- `PROPOSE_VOTE: <proposal> | options: <opt1, opt2>` — new vote proposal
- `DONE: <summary>` — completion signal

To add a new action type, add a new prefix to scan for and a new key in the
returned `actions` dict. Then handle it in `council_agent.py`'s main loop.

## How to Modify Task Analysis

`analyze_task()` in `council_supervisor.py` uses keyword matching to detect
what roles a task needs. To add new detection logic:

1. Define keywords for your detection
2. Add an `if any(kw in task_lower ...)` block that appends to `needs`
3. Optionally adjust complexity indicators
4. Add a test case in `test_council.py`

The complexity scoring works as:
- Each matched complex keyword adds 1 to the complexity indicator count
- Each non-base role (not coder/reviewer) also adds 1
- Score >= 4: complex (gets planner + second coder)
- Score >= 2: moderate (gets planner)
- Score < 2: simple (minimal team)

## How to Modify the Dashboard

The frontend is a single-file SPA in `dashboard/index.html` — no build step,
no framework, no npm. Just HTML + CSS + vanilla JS. The backend serves it
at `/` and the API at `/api/*`.

Key JS functions:
- `loadSessions()`: fetches and renders the session list sidebar
- `selectSession(id)`: loads a session and connects SSE stream
- `renderSession(data)`: renders the conversation, agents panel, header
- `appendMessage(msg)`: appends a single message to the conversation view
- `handleInput(el)` / `handleInputKey(e)`: @mention dropdown logic
- `castVote(voteId, response)`: human responds to a vote
- `createSession()`: creates a new session via the modal

The SSE stream (`/api/sessions/<id>/stream`) pushes events: `state` (full
room state on connect), `message` (new message), `vote` (vote-related event).

## How to Modify the Server

`council_server.py` contains:

- `SessionRoom`: per-session state (members, messages, votes, persistence, SSE)
- `SessionManager`: creates/lists/deletes/persists sessions
- `CouncilServerHandler`: HTTP handler (API + static files + SSE)
- `detect_bind_host()`: auto-detects VPN/private IP for binding
- `main()`: starts the server

The server persists session metadata to `.council_data/<id>.json` and
message history to `.council_data/<id>_history.json`. On restart, past
sessions are loaded automatically with full history.

## Common Pitfalls

1. **Don't add external dependencies** — the server and agent harness must
   work with Python stdlib only (PyYAML is the one exception, for config).

2. **Don't hardcode CLIs** — all CLI invocation goes through the backend
   registry. Never call `subprocess.run(["claude", ...])` directly outside
   of `Backend.run()`.

3. **Don't hardcode roles** — role descriptions come from YAML. The prompt
   builder takes `role_description` as a parameter, not from a global dict.

4. **Don't hardcode network IPs** — use `detect_bind_host()` which
   auto-detects private network addresses. Never bind to 0.0.0.0 by default.

5. **Test before committing** — run both test suites:
   `python3 test_council.py` and `python3 test_server.py`. All tests must pass.

6. **Keep agents stateless from the server's perspective** — agents interact
   via HTTP calls to the server. They don't need to import server code. This
   allows agents running in separate processes to participate.

7. **Prompt truncation** — `build_prompt()` truncates conversation to the last
   80 messages. If you increase this, watch for context window overflow with
   smaller models (Ollama models can have 4k-8k context).

8. **Consensus requires all members** — a vote only closes when every member
   has responded. If an agent crashes without voting, the vote stays open
   forever. The supervisor should handle this (future: timeout + forced close).

9. **Restart the service after code changes** — if running as systemd:
   `sudo systemctl restart ai-council`

10. **Session data is in .council_data/** — gitignored, persists across
    restarts. Delete sessions from the dashboard or clear the directory to
    reset.