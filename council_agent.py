#!/usr/bin/env python3
"""
Council Agent — Harness that connects an external AI agent CLI to the Council Bus.

This harness runs an event loop:
  1. Poll the bus for new messages and open votes
  2. Build a prompt that includes the conversation context, the agent's role,
     and any pending votes
  3. Send the prompt to the agent CLI (Claude Code, Codex, or a shell command)
  4. Parse the agent's response
  5. Post the response to the bus as a message
  6. If there's an open vote, parse the vote response and submit it

Usage:
    python3 council_agent.py \\
        --bus http://127.0.0.1:8747 \\
        --role reviewer \\
        --agent-id reviewer-01 \\
        --backend claude-code \\
        --workdir /path/to/repo \\
        --max-rounds 20

Backends:
    claude-code   Uses `claude -p '...' --allowedTools Read --max-turns 5`
    codex         Uses `codex exec '...'`
    opencode      Uses `opencode run '...'`
    shell         Uses a custom --command '...' (for any other agent)
    subagent      Prints the prompt and reads a response from stdin (for Hermes
                  delegate_task integration — the coordinator provides the response)
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import textwrap
import urllib.request
import urllib.error

# --- Bus Client ---

class BusClient:
    """Thin HTTP client for the Council Bus."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _request(self, method, path, body=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())
        except Exception as e:
            return {"error": str(e)}

    def join(self, agent_id, role, model=""):
        return self._request("POST", "/join", {"agent_id": agent_id, "role": role, "model": model})

    def leave(self, agent_id):
        return self._request("POST", "/leave", {"agent_id": agent_id})

    def post_message(self, agent_id, content, msg_type="message"):
        return self._request("POST", "/message", {"agent_id": agent_id, "content": content, "type": msg_type})

    def get_messages(self, since=0):
        return self._request("GET", f"/messages?since={since}")

    def get_room(self):
        return self._request("GET", "/room")

    def propose_vote(self, agent_id, proposal, options=None):
        return self._request("POST", "/vote/propose", {"agent_id": agent_id, "proposal": proposal, "options": options})

    def respond_vote(self, agent_id, vote_id, response, rationale=""):
        return self._request("POST", "/vote/respond", {"agent_id": agent_id, "vote_id": vote_id, "response": response, "rationale": rationale})

    def get_vote_status(self, vote_id):
        return self._request("GET", f"/vote/status/{vote_id}")


# --- Agent Backends ---

def run_claude_code(prompt, workdir, max_turns=10, read_only=False):
    """Run Claude Code in print mode."""
    tools = "Read" if read_only else "Read,Edit,Write,Bash"
    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", tools,
        "--max-turns", str(max_turns),
        "--output-format", "json",
    ]
    result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=300)
    try:
        parsed = json.loads(result.stdout)
        return parsed.get("result", result.stdout)
    except json.JSONDecodeError:
        return result.stdout


def run_codex(prompt, workdir, full_auto=True):
    """Run Codex CLI."""
    cmd = ["codex", "exec"]
    if full_auto:
        cmd.append("--full-auto")
    cmd.append(prompt)
    result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=300)
    return result.stdout


def run_opencode(prompt, workdir):
    """Run OpenCode CLI."""
    cmd = ["opencode", "run", prompt]
    result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=300)
    return result.stdout


def run_shell(command, prompt, workdir):
    """Run a custom shell command, passing the prompt via stdin."""
    result = subprocess.run(
        command, input=prompt, cwd=workdir,
        capture_output=True, text=True, timeout=300, shell=True
    )
    return result.stdout


def run_subagent(prompt):
    """For Hermes delegate_task integration — prints prompt, reads response from stdin."""
    print("=== AGENT PROMPT ===")
    print(prompt)
    print("=== END PROMPT (paste response below, end with EOF on empty line) ===")
    sys.stdout.flush()
    lines = []
    try:
        while True:
            line = input()
            if line == "EOF":
                break
            lines.append(line)
    except EOFError:
        pass
    return "\n".join(lines)


# --- Prompt Building ---

ROLE_DESCRIPTIONS = {
    "coordinator": (
        "You are the COORDINATOR of the AI Council. You orchestrate the work, "
        "ensure repo patterns are followed, and break ties. You guide the conversation, "
        "summarize progress, and decide when to call votes. You have the final say "
        "on process, but technical decisions are made by consensus."
    ),
    "reviewer": (
        "You are the REVIEWER of the AI Council. Your job is to critically examine "
        "plans, code, and tests proposed by others. You check for: bugs, security issues, "
        "missing edge cases, repo pattern violations, test coverage gaps, and design flaws. "
        "Be specific — reference file paths, line numbers, and actual patterns. "
        "When you approve, say 'APPROVE'. When you want changes, say 'REQUEST_CHANGES: <details>'."
    ),
    "coder": (
        "You are the CODER of the AI Council. Your job is to implement features following "
        "the plan and repo patterns. Write tests first (TDD). Run tests after each change. "
        "Commit when green. When you're done, say 'IMPLEMENTATION_COMPLETE: <summary>'. "
        "If you disagree with the plan, raise it in the council before implementing."
    ),
}


def build_prompt(role, conversation, open_votes, agent_id, round_num, workdir, extra_context=""):
    """Build the prompt to send to the agent CLI."""
    role_desc = ROLE_DESCRIPTIONS.get(role, f"You are a {role} in the AI Council.")

    # Format conversation history
    conv_lines = []
    for msg in conversation:
        prefix = f"[{msg['role']}]"
        if msg["type"] == "system":
            conv_lines.append(f"  *{prefix} {msg['content']}*")
        elif msg["type"] == "vote_proposal":
            conv_lines.append(f"  {prefix} PROPOSED VOTE: {msg['content']}")
        elif msg["type"] == "vote_response":
            conv_lines.append(f"  {prefix} {msg['content']}")
        else:
            conv_lines.append(f"  {prefix} {msg['content']}")

    conversation_text = "\n".join(conv_lines[-60:]) if conv_lines else "  (no messages yet)"

    # Format open votes
    votes_text = ""
    if open_votes:
        vote_lines = []
        for v in open_votes:
            vote_lines.append(
                f"  Vote {v['vote_id']}: {v['proposal']}\n"
                f"    Options: {', '.join(v.get('options', ['approve', 'reject', 'request_changes']))}\n"
                f"    Responses so far: {v['total_responses']}/{v['total_members']}\n"
                f"    Tally: {v.get('tally', {})}"
            )
        votes_text = "\n".join(vote_lines)

    prompt = f"""{role_desc}

You are agent '{agent_id}' in round {round_num} of the AI Council.
Working directory: {workdir}

=== COUNCIL CONVERSATION ===
{conversation_text}
=== END CONVERSATION ===

"""
    if votes_text:
        prompt += f"""=== OPEN VOTES (you must respond to these) ===
{votes_text}
=== END VOTES ===

To respond to a vote, include a line like:
VOTE: <vote_id> <option> -- <rationale>

"""
    if extra_context:
        prompt += f"=== CONTEXT ===\n{extra_context}\n=== END CONTEXT ===\n\n"

    prompt += """=== YOUR TURN ===
Respond with your contribution to the council. You can:
- Share analysis, findings, or concerns
- Propose a plan or code changes (describe them clearly)
- Call a vote by writing: PROPOSE_VOTE: <proposal> | options: <opt1, opt2, ...>
- Respond to an open vote: VOTE: <vote_id> <option> -- <rationale>
- Ask another member a question: ASK @<role>: <question>
- Signal completion: DONE: <summary>

Keep your response focused and actionable.
"""
    return prompt


# --- Response Parsing ---

def parse_response(response_text):
    """Parse the agent's response for structured actions."""
    actions = {"message": None, "votes": [], "propose_vote": None, "done": False}

    lines = response_text.strip().split("\n")
    message_lines = []
    in_message = True

    for line in lines:
        stripped = line.strip()

        # Vote response: VOTE: <vote_id> <option> -- <rationale>
        if stripped.upper().startswith("VOTE:"):
            parts = stripped[5:].strip().split(None, 1)
            if len(parts) >= 2:
                vote_id = parts[0].strip()
                rest = parts[1]
                if "--" in rest:
                    option, rationale = rest.rsplit("--", 1)
                    option = option.strip()
                    rationale = rationale.strip()
                else:
                    option = rest.strip()
                    rationale = ""
                actions["votes"].append({"vote_id": vote_id, "response": option, "rationale": rationale})
                in_message = False

        # Vote proposal: PROPOSE_VOTE: <proposal> | options: <opt1, opt2, ...>
        elif stripped.upper().startswith("PROPOSE_VOTE:"):
            rest = stripped[len("PROPOSE_VOTE:"):].strip()
            if "|" in rest:
                proposal, opts_part = rest.rsplit("|", 1)
                proposal = proposal.strip()
                opts_part = opts_part.strip()
                if opts_part.lower().startswith("options:"):
                    opts_part = opts_part[len("options:"):].strip()
                options = [o.strip() for o in opts_part.split(",")]
            else:
                proposal = rest
                options = ["approve", "reject", "request_changes"]
            actions["propose_vote"] = {"proposal": proposal, "options": options}
            in_message = False

        # Done signal
        elif stripped.upper().startswith("DONE:"):
            actions["done"] = True
            if in_message:
                message_lines.append(stripped)
        else:
            if in_message:
                message_lines.append(line)

    actions["message"] = "\n".join(message_lines).strip() if message_lines else response_text.strip()
    return actions


# --- Main Agent Loop ---

def main():
    parser = argparse.ArgumentParser(description="Council Agent — connect an AI CLI to the Council Bus")
    parser.add_argument("--bus", default="http://127.0.0.1:8747", help="Bus URL")
    parser.add_argument("--role", required=True, choices=["coordinator", "reviewer", "coder"],
                       help="Agent role in the council")
    parser.add_argument("--agent-id", default=None, help="Unique agent ID (auto-generated if not set)")
    parser.add_argument("--backend", default="claude-code",
                       choices=["claude-code", "codex", "opencode", "shell", "subagent"],
                       help="Agent CLI backend")
    parser.add_argument("--workdir", default=".", help="Working directory for the agent")
    parser.add_argument("--max-rounds", type=int, default=20, help="Maximum conversation rounds")
    parser.add_argument("--max-turns", type=int, default=10, help="Max turns for Claude Code")
    parser.add_argument("--read-only", action="store_true", help="Restrict agent to read-only (reviewer)")
    parser.add_argument("--command", default=None, help="Custom shell command (for --backend shell)")
    parser.add_argument("--model", default="", help="Model identifier for display")
    parser.add_argument("--context", default="", help="Extra context to inject into every prompt")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between bus polls")
    parser.add_argument("--auto-vote", action="store_true",
                       help="Automatically respond to open votes after agent output")
    args = parser.parse_args()

    agent_id = args.agent_id or f"{args.role}-{uuid.uuid4().hex[:6]}"
    bus = BusClient(args.bus)

    # Join the council
    join_result = bus.join(agent_id, args.role, args.model)
    if "error" in join_result:
        print(f"Failed to join council: {join_result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"Joined council as [{args.role}] agent_id={agent_id}")
    print(f"Backend: {args.backend} | Workdir: {args.workdir}")
    print(f"Members in room: {list(join_result.get('room', {}).get('members', {}).keys())}")
    sys.stdout.flush()

    last_seen_timestamp = 0
    round_num = 0

    try:
        while round_num < args.max_rounds:
            round_num += 1

            # Poll for new messages
            msgs_result = bus.get_messages(since=last_seen_timestamp)
            new_messages = msgs_result.get("messages", [])
            if new_messages:
                last_seen_timestamp = new_messages[-1]["timestamp"]

            # Get room state (open votes, members)
            room = bus.get_room()
            open_votes = room.get("open_votes", [])

            # If no new messages and no open votes, wait
            if not new_messages and not open_votes:
                time.sleep(args.poll_interval)
                round_num -= 1  # don't count idle polls as rounds
                continue

            # Get full conversation for context
            all_msgs = bus.get_messages(since=0).get("messages", [])

            # Build the prompt
            prompt = build_prompt(
                role=args.role,
                conversation=all_msgs,
                open_votes=open_votes,
                agent_id=agent_id,
                round_num=round_num,
                workdir=args.workdir,
                extra_context=args.context,
            )

            print(f"\n--- Round {round_num} ---")
            print(f"New messages: {len(new_messages)} | Open votes: {len(open_votes)}")
            sys.stdout.flush()

            # Run the agent backend
            if args.backend == "claude-code":
                response = run_claude_code(prompt, args.workdir, args.max_turns, args.read_only)
            elif args.backend == "codex":
                response = run_codex(prompt, args.workdir)
            elif args.backend == "opencode":
                response = run_opencode(prompt, args.workdir)
            elif args.backend == "shell":
                response = run_shell(args.command, prompt, args.workdir)
            elif args.backend == "subagent":
                response = run_subagent(prompt)
            else:
                response = f"Unknown backend: {args.backend}"

            if not response or not response.strip():
                print("Agent returned empty response, skipping round")
                continue

            # Parse the response
            actions = parse_response(response)

            # Post the message to the bus
            if actions["message"]:
                bus.post_message(agent_id, actions["message"])
                print(f"Posted message ({len(actions['message'])} chars)")

            # Handle vote responses
            for vote_resp in actions["votes"]:
                result = bus.respond_vote(agent_id, vote_resp["vote_id"], vote_resp["response"], vote_resp["rationale"])
                print(f"Vote {vote_resp['vote_id']}: {vote_resp['response']} -> {result.get('status', 'unknown')}")

            # Handle vote proposals
            if actions["propose_vote"]:
                result = bus.propose_vote(agent_id, actions["propose_vote"]["proposal"], actions["propose_vote"]["options"])
                print(f"Proposed vote: {result.get('vote_id', 'error')}")

            # Check if done
            if actions["done"]:
                print("Agent signaled DONE — exiting loop")
                break

            # Brief pause before next poll
            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        print("\nInterrupted, leaving council...")
    finally:
        bus.leave(agent_id)
        print(f"Left council (agent_id={agent_id})")


if __name__ == "__main__":
    main()