# AGENTS.md — Guide for AI Agents Working on AI Council

## What This Project Is

AI Council is a multi-agent collaboration harness. AI agents (powered by
different CLI tools — Claude Code, Codex, Ollama, etc.) join a shared HTTP
message bus, debate plans, vote on proposals with consensus rules, implement
code, and review each other's work in iterative rounds — like an extreme
programming peer review session.

The supervisor dynamically analyzes the task and composes a team. A YAML
config declares which CLIs are available, what models they offer, and which
roles they fill.

## Repository Structure

```
ai-council/
├── AGENTS.md               # This file — guide for AI agents
├── README.md               # Human-facing documentation
├── council.yaml            # Config: backends, roles, models, consensus
├── council_backends.py     # Backend registry — extensible CLI plugin system
├── council_bus.py          # HTTP message bus + voting + prompt builder + parser
├── council_agent.py        # Agent harness — connects any CLI to the bus
├── council_supervisor.py   # Dynamic team composition + session orchestration
├── test_council.py          # Integration test (no external CLIs needed)
└── .gitignore
```

## Key Concepts

### Backends

A backend is a CLI tool that can receive a text prompt and produce a text
response. Examples: `claude` (Claude Code), `codex` (OpenAI Codex), `ollama`
(Ollama), `copilot` (GitHub Copilot CLI). The backend registry builds the
subprocess command, handles prompt passing (argument/stdin/file), and parses
JSON output.

### Roles

A role is a function an agent performs in the council. The fixed vocabulary:
`supervisor`, `planner`, `architect`, `coder`, `reviewer`, `tester`,
`researcher`, `security`. Each role has a description (used as the system
prompt), a default backend, a default model, and permissions (can_vote,
can_propose_vote, read_only).

### Bus

The Council Bus is an HTTP server (stdlib `http.server`) that stores the
shared conversation, manages member registration, and handles the voting
system. All agents communicate via simple HTTP JSON calls to the bus.

### Supervisor

The supervisor (typically Hermes, but can be any coordinator) analyzes the
task description using keyword detection, assesses complexity, checks repo
size, and decides which agents to spawn. It then starts the bus, spawns
agent processes, posts the task, and monitors the conversation.

## How to Work on This Codebase

### Running Tests

```bash
cd ~/ai-council
python3 test_council.py
```

Tests require no external CLIs — they use in-process bus instances and mock
backends. All tests must pass before committing.

### Dry Run (Preview Team Composition)

```bash
python3 council_supervisor.py --task "your task" --workdir /path/to/repo --dry-run
```

This shows what agents the supervisor would spawn without actually running
them. Useful for validating changes to task analysis or team composition.

### Code Style

- Python 3.11+ (stdlib only for the bus and agent harness; PyYAML for config)
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
component (bus, agent, supervisor, backend, config).

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
        best_for: [planning, review]         # tags for role-matchinging
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

### Step 3: Update the test

Add a test case in `test_council.py` that verifies the keyword detection
triggers the new role.

## How to Modify the Voting System

The voting logic lives in `council_bus.py`, class `CouncilBus`:

- `propose_vote()`: Creates a vote with options (default: approve/reject/request_changes)
- `respond_vote()`: Records an agent's response with rationale
- `_check_vote_completion()`: Closes the vote when all members have responded
- `_tally_result()`: Applies the consensus rule (majority/supermajority/unanimous)

To add a new consensus rule:

1. Add it to the `_tally_result()` method in `CouncilBus`
2. Add it to the `set_consensus_rule()` validation
3. Add it to the `--consensus` argparse choices in `council_bus.py` main
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

## Common Pitfalls

1. **Don't add external dependencies** — the bus and agent harness must work
   with Python stdlib only (PyYAML is the one exception, for config loading).

2. **Don't hardcode CLIs** — all CLI invocation goes through the backend
   registry. Never call `subprocess.run(["claude", ...])` directly outside
   of `Backend.run()`.

3. **Don't hardcode roles** — role descriptions come from YAML. The prompt
   builder takes `role_description` as a parameter, not from a global dict.

4. **Test before committing** — run `python3 test_council.py` and verify all
   tests pass. The test suite covers config loading, backend registry, task
   analysis, team composition, bus operations, prompt building, response
   parsing, and command construction.

5. **Keep the bus stateless from the agent's perspective** — agents interact
   via HTTP calls. They don't need to import bus code. This allows agents
   running in separate processes (or even separate machines) to participate.

6. **Prompt truncation** — `build_prompt()` truncates conversation to the last
   80 messages. If you increase this, watch for context window overflow with
   smaller models (Ollama models can have 4k-8k context).

7. **Consensus requires all members** — a vote only closes when every member
   has responded. If an agent crashes without voting, the vote stays open
   forever. The supervisor should handle this (future: timeout + forced close).