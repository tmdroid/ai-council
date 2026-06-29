# AI Council

A multi-agent collaboration harness where AI agents (coordinator, reviewer, coder)
work together in a shared conversation with voting-based consensus, like an extreme
programming peer review session.

## Architecture

```
                    ┌──────────────────────┐
                    │    Council Bus        │
                    │  (HTTP message server) │
                    │                       │
                    │  - Message history    │
                    │  - Member registry     │
                    │  - Voting system      │
                    │  - Consensus rules    │
                    └──────┬───────┬───────┘
                           │       │       │
            ┌──────────────┘       │       └──────────────┐
            │                      │                      │
     ┌──────▼──────┐      ┌────────▼────────┐    ┌────────▼────────┐
     │ Coordinator │      │    Reviewer     │    │      Coder      │
     │  (Hermes)   │      │ (Claude Code)   │    │   (Codex CLI)   │
     │             │      │  --read-only    │    │  --full-auto    │
     │ Orchestrates│      │  Reviews plans  │    │  Implements     │
     │ Posts tasks │      │  Reviews code   │    │  Writes tests   │
     │ Calls votes │      │  Votes on work  │    │  Votes on plans │
     └─────────────┘      └─────────────────┘    └─────────────────┘
```

### Components

1. **`council_bus.py`** — HTTP message bus (chat room + voting system)
   - Agents join with a role and post messages visible to all
   - Any agent can propose a vote; all members must respond
   - Consensus rules: majority, supermajority, or unanimous
   - Full conversation history accessible to all agents

2. **`council_agent.py`** — Agent harness that connects an external CLI to the bus
   - Polls the bus for new messages and open votes
   - Builds a prompt with conversation context + role description + pending votes
   - Sends the prompt to the agent CLI (Claude Code, Codex, OpenCode, or custom)
   - Parses the response for messages, vote responses, and vote proposals
   - Posts everything back to the bus

3. **`council_run.py`** — Entry point script that starts everything
   - Starts the bus
   - Spawns the reviewer and coder agents
   - Coordinator (you/Hermes) joins and posts the task
   - Monitors the conversation and prints it live

4. **`test_council.py`** — Integration test (no external CLIs needed)

## Quick Start

### Test the harness (no external tools needed)

```bash
cd ~/ai-council
python3 test_council.py
```

### Run a real council session

```bash
# With Claude Code (reviewer) + Codex (coder)
python3 council_run.py \
    --task "Add dark mode toggle to settings screen" \
    --workdir ~/my-repo \
    --reviewer-backend claude-code \
    --coder-backend codex \
    --consensus majority

# With Claude Code for both roles
python3 council_run.py \
    --task "Refactor auth module to use JWT" \
    --workdir ~/my-repo \
    --reviewer-backend claude-code \
    --coder-backend claude-code \
    --consensus supermajority

# Dry run (dummy agents, for testing the harness)
python3 council_run.py \
    --task "Test task" \
    --workdir /tmp \
    --dry-run
```

### Manual bus interaction (for debugging or custom agents)

```bash
# Start the bus
python3 council_bus.py --port 8747 --consensus majority

# Join
curl -X POST http://127.0.0.1:8747/join \
  -d '{"agent_id":"my-agent","role":"reviewer","model":"custom"}'

# Post a message
curl -X POST http://127.0.0.1:8747/message \
  -d '{"agent_id":"my-agent","content":"The plan looks good"}'

# Read the conversation
curl http://127.0.0.1:8747/messages | python3 -m json.tool

# Propose a vote
curl -X POST http://127.0.0.1:8747/vote/propose \
  -d '{"agent_id":"my-agent","proposal":"Approve the plan","options":["approve","reject"]}'

# Respond to a vote
curl -X POST http://127.0.0.1:8747/vote/respond \
  -d '{"agent_id":"my-agent","vote_id":"abc12345","response":"approve","rationale":"looks good"}'

# Check room state
curl http://127.0.0.1:8747/room | python3 -m json.tool
```

## How Agents Communicate

Each agent CLI (Claude Code, Codex) runs in its own process. The harness
(`council_agent.py`) wraps it:

1. **Poll**: The harness polls the bus for new messages and open votes
2. **Build prompt**: It constructs a prompt containing:
   - The agent's role description
   - The full conversation history
   - Any open votes that need a response
   - The working directory and context
3. **Execute**: It runs the agent CLI (e.g., `claude -p '...' --allowedTools Read`)
4. **Parse**: It scans the response for structured actions:
   - `VOTE: <vote_id> <option> -- <rationale>` — respond to a vote
   - `PROPOSE_VOTE: <proposal> | options: <opt1, opt2>` — call a new vote
   - `DONE: <summary>` — signal completion
5. **Post**: It posts the message and any vote actions to the bus
6. **Loop**: Back to step 1

This means agents don't need to know about HTTP or the bus protocol — they
just need to be able to read a prompt and produce text. Any CLI agent that
can do that can join the council.

## Voting and Consensus

### Consensus Rules

| Rule          | Requirement                    | Result String              |
|---------------|--------------------------------|----------------------------|
| `majority`    | >50% approve                   | `approved_majority`        |
| `supermajority` | >67% approve                 | `approved_supermajority`   |
| `unanimous`   | 100% approve, no dissent       | `approved_unanimous`        |

If consensus is not reached, the vote status is `failed_no_consensus` and
the council continues debating.

### Vote Flow

1. Any agent proposes a vote: `PROPOSE_VOTE: Should we use approach X?`
2. The bus creates the vote and notifies all members
3. Each agent responds: `VOTE: <id> approve -- because...`
4. When all members have voted, the bus tallies and closes the vote
5. The result is posted to the conversation as a system message
6. If failed, the council discusses and a new vote may be proposed

## Backends

| Backend       | Command                                      | Notes                           |
|---------------|----------------------------------------------|---------------------------------|
| `claude-code` | `claude -p '...' --allowedTools Read`        | Best for reviewer (read-only)   |
| `codex`       | `codex exec --full-auto '...'`              | Best for coder (write access)   |
| `opencode`    | `opencode run '...'`                         | Provider-agnostic               |
| `shell`       | Custom `--command '...'` (gets prompt via stdin) | For any other agent      |
| `subagent`    | Prints prompt, reads response from stdin    | For Hermes delegate_task        |

## Installation

No external dependencies — pure Python stdlib. Just install the agent CLIs
you want to use:

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code
claude auth login

# Codex
npm install -g @openai/codex
# Set OPENAI_API_KEY or login

# OpenCode
npm i -g opencode-ai@latest
opencode auth login
```

## Files

```
ai-council/
├── council_bus.py      # HTTP message bus with voting
├── council_agent.py    # Agent harness (connects CLI to bus)
├── council_run.py      # Entry point (starts bus + spawns agents)
├── test_council.py     # Integration test (no CLIs needed)
└── README.md           # This file
```