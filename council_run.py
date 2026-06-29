#!/usr/bin/env python3
"""
Council Coordinator — Spawns the bus and all agents, manages the session.

This is the entry point. You run this script and it:
  1. Starts the Council Bus (HTTP message server)
  2. Spawns the Reviewer agent (Claude Code, read-only)
  3. Spawns the Coder agent (Codex, full-auto)
  4. The Coordinator (Hermes) joins as a member and orchestrates
  5. Agents talk to each other through the bus, vote on proposals
  6. You (the human) can watch the conversation and jump in

Usage:
    python3 council_run.py \\
        --task "Implement dark mode in the settings screen" \\
        --workdir /path/to/repo \\
        --reviewer-backend claude-code \\
        --coder-backend codex \\
        --consensus majority

If Claude Code / Codex are not installed, use --reviewer-backend subagent
and --coder-backend subagent to run with Hermes delegate_task subagents
(the coordinator will print prompts for you to paste into delegate_task calls).

You can also use --reviewer-backend shell --reviewer-command 'echo "APPROVE"'
for a dummy agent that always approves (useful for testing the bus).
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path


def find_free_port(default=8747):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", default))
            return default
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def start_bus(port, consensus, verbose=True):
    """Start the Council Bus as a background process."""
    bus_script = Path(__file__).parent / "council_bus.py"
    cmd = [sys.executable, str(bus_script), "--port", str(port), "--consensus", consensus]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    # Wait for bus to be ready
    for _ in range(10):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    if verbose:
                        print(f"  Bus ready on port {port}")
                    return proc
        except Exception:
            time.sleep(0.3)

    # Bus didn't start
    stderr = proc.stderr.read() if proc.stderr else ""
    print(f"  Bus failed to start. stderr: {stderr}", file=sys.stderr)
    return None


def spawn_agent(bus_url, role, backend, workdir, agent_id=None, read_only=False,
                max_turns=10, command=None, context="", model=""):
    """Spawn a council_agent.py process."""
    agent_script = Path(__file__).parent / "council_agent.py"
    cmd = [sys.executable, str(agent_script),
           "--bus", bus_url,
           "--role", role,
           "--backend", backend,
           "--workdir", workdir]

    if agent_id:
        cmd += ["--agent-id", agent_id]
    if read_only:
        cmd += ["--read-only"]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if command:
        cmd += ["--command", command]
    if context:
        cmd += ["--context", context]
    if model:
        cmd += ["--model", model]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return proc


class BusClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _request(self, method, path, body=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def join(self, agent_id, role, model=""):
        return self._request("POST", "/join", {"agent_id": agent_id, "role": role, "model": model})

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


def monitor_bus(bus_url, stop_event):
    """Print bus activity to the console for the human to watch."""
    client = BusClient(bus_url)
    last_ts = 0
    while not stop_event.is_set():
        try:
            msgs = client.get_messages(since=last_ts)
            for msg in msgs.get("messages", []):
                last_ts = msg["timestamp"]
                role = msg.get("role", "?")
                mtype = msg.get("type", "message")
                content = msg.get("content", "")
                # Truncate long messages for display
                if len(content) > 200:
                    content = content[:200] + "..."
                marker = ">>>" if mtype == "system" else "  "
                print(f"{marker} [{role}] {content}")
        except Exception:
            pass
        time.sleep(1.5)


def main():
    parser = argparse.ArgumentParser(description="Run an AI Council session")
    parser.add_argument("--task", required=True, help="Task description for the council")
    parser.add_argument("--workdir", default=".", help="Repository path")
    parser.add_argument("--reviewer-backend", default="claude-code",
                       choices=["claude-code", "codex", "opencode", "shell", "subagent"],
                       help="Backend for the reviewer agent")
    parser.add_argument("--coder-backend", default="codex",
                       choices=["claude-code", "codex", "opencode", "shell", "subagent"],
                       help="Backend for the coder agent")
    parser.add_argument("--consensus", default="majority",
                       choices=["majority", "supermajority", "unanimous"],
                       help="Consensus rule for votes")
    parser.add_argument("--reviewer-command", default=None, help="Custom command for shell backend reviewer")
    parser.add_argument("--coder-command", default=None, help="Custom command for shell backend coder")
    parser.add_argument("--reviewer-turns", type=int, default=10, help="Max turns for reviewer (Claude Code)")
    parser.add_argument("--coder-turns", type=int, default=30, help="Max turns for coder (Claude Code)")
    parser.add_argument("--max-rounds", type=int, default=20, help="Max council rounds per agent")
    parser.add_argument("--context", default="", help="Extra context (e.g. Jira ticket details, repo patterns)")
    parser.add_argument("--port", type=int, default=None, help="Bus port (auto-selected if not set)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Start bus + dummy agents only (for testing the harness)")
    args = parser.parse_args()

    port = args.port or find_free_port()
    bus_url = f"http://127.0.0.1:{port}"

    print("=" * 60)
    print("  AI COUNCIL — Multi-Agent Collaboration Session")
    print("=" * 60)
    print()
    print(f"  Task: {args.task}")
    print(f"  Repo: {args.workdir}")
    print(f"  Reviewer: {args.reviewer_backend}")
    print(f"  Coder: {args.coder_backend}")
    print(f"  Consensus: {args.consensus}")
    print()

    # 1. Start the bus
    print("[1/4] Starting Council Bus...")
    bus_proc = start_bus(port, args.consensus)
    if not bus_proc:
        print("FATAL: Bus failed to start", file=sys.stderr)
        sys.exit(1)

    # 2. Coordinator (Hermes) joins the bus
    print("[2/4] Coordinator joining the council...")
    coordinator_id = "coordinator"
    client = BusClient(bus_url)
    client.join(coordinator_id, "coordinator", "hermes")

    # Post the task to the council
    task_msg = (
        f"COUNCIL TASK: {args.task}\n\n"
        f"Working directory: {args.workdir}\n"
        f"Consensus rule: {args.consensus}\n"
    )
    if args.context:
        task_msg += f"\nContext:\n{args.context}\n"
    task_msg += (
        "\n---\n"
        "Council members: please review the repository, discuss the approach, "
        "and agree on a plan before implementing. The reviewer should verify "
        "the plan follows repo patterns. The coder should implement only after "
        "the plan is approved by vote. After implementation, the reviewer "
        "should review the changes, and we vote again to accept or request changes."
    )
    client.post_message(coordinator_id, task_msg, "task")

    # 3. Spawn agents
    procs = []
    print("[3/4] Spawning agents...")

    if args.dry_run:
        # Dummy agents that always approve via shell backend
        reviewer_cmd = 'echo "APPROVE. The plan looks good. VOTE: $1 approve -- looks correct"'
        coder_cmd = 'echo "IMPLEMENTATION_COMPLETE: done. DONE: implemented the task"'
        r_proc = spawn_agent(bus_url, "reviewer", "shell", args.workdir,
                            agent_id="reviewer-01", command=reviewer_cmd)
        c_proc = spawn_agent(bus_url, "coder", "shell", args.workdir,
                           agent_id="coder-01", command=coder_cmd)
        procs = [r_proc, c_proc]
    else:
        r_proc = spawn_agent(bus_url, "reviewer", args.reviewer_backend, args.workdir,
                            agent_id="reviewer-01", read_only=True,
                            max_turns=args.reviewer_turns, context=args.context)
        procs.append(r_proc)
        print(f"  Reviewer spawned (backend={args.reviewer_backend})")

        c_proc = spawn_agent(bus_url, "coder", args.coder_backend, args.workdir,
                           agent_id="coder-01", read_only=False,
                           max_turns=args.coder_turns, context=args.context)
        procs.append(c_proc)
        print(f"  Coder spawned (backend={args.coder_backend})")

    # 4. Monitor the conversation
    print("[4/4] Council session active. Monitoring conversation...")
    print("  (Press Ctrl+C to end the session)")
    print()

    stop_event = threading.Event()
    monitor_thread = threading.Thread(target=monitor_bus, args=(bus_url, stop_event), daemon=True)
    monitor_thread.start()

    # Wait for agents to finish or user interrupt
    try:
        while True:
            # Check if any agent process has exited
            for proc in procs:
                if proc.poll() is not None:
                    print(f"\nAgent process exited (pid={proc.pid})")
            if all(p.poll() is not None for p in procs):
                print("\nAll agents have exited.")
                break
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n\nEnding council session...")

    # Cleanup agents first
    stop_event.set()
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # Print final conversation summary BEFORE killing the bus
    print("\n" + "=" * 60)
    print("  COUNCIL SESSION ENDED")
    print("=" * 60)
    try:
        final_msgs = client.get_messages(since=0)
        print(f"  Total messages: {final_msgs.get('count', 0)}")
    except Exception:
        print("  (could not fetch final messages)")

    print(f"  Bus URL was: {bus_url}")
    print()

    # Show last few messages
    try:
        messages = final_msgs.get("messages", [])
        if messages:
            print("  Last messages:")
            for msg in messages[-5:]:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if len(content) > 150:
                    content = content[:150] + "..."
                print(f"  [{role}] {content}")
    except Exception:
        pass

    # Now kill the bus
    if bus_proc and bus_proc.poll() is None:
        bus_proc.terminate()
        try:
            bus_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bus_proc.kill()


if __name__ == "__main__":
    main()