#!/usr/bin/env python3
"""
Council Orchestrator — The active supervisor that manages phases.

The orchestrator is an AI agent (OllamaClaudeCodeAgent) that:
1. Reads the task and reasons about what phases are needed
2. Proposes a phase plan to the council
3. Spawns agents for the current phase
4. Monitors agent progress
5. Decides when a phase is complete and transitions to the next
6. Declares task completion when all phases are done

The orchestrator is the boss. It's the only entity that can:
- Set phases
- Spawn/stop agents
- Transition between phases
- Declare completion

Other agents can only post messages and votes within the current phase.

Usage:
    from council_orchestrator import Orchestrator

    orch = Orchestrator(
        session_id="abc123",
        server_url="http://10.0.0.8:8080",
        workdir="/home/hermes/imnuri-azsmr",
        task="Upgrade all libraries to latest versions",
    )
    orch.start()  # proposes phases, waits for approval
    # ... human approves in dashboard ...
    orch.run()    # manages the full workflow

Or via CLI:
    python3 council_orchestrator.py --session <id> --bus http://10.0.0.8:8080 --task "..."
"""

import json
import os
import sys
import time
import subprocess
import urllib.request
import argparse
from pathlib import Path
from typing import Optional


# Ensure venv packages are importable
_VENV_SITE = str(Path(__file__).parent / ".venv" / "lib" / "python3.14" / "site-packages")
if Path(_VENV_SITE).exists():
    sys.path.insert(0, _VENV_SITE)


class Orchestrator:
    """The active supervisor that manages the council workflow."""

    def __init__(self, session_id, server_url, workdir, task, config_path="council.yaml"):
        self.session_id = session_id
        self.server_url = server_url
        self.workdir = workdir
        self.task = task
        self.config_path = str(Path(__file__).parent / config_path)
        self.config = self._load_config()

        # The orchestrator's own AI agent (Claude Code + Ollama)
        self.agent = None
        self.phases = []
        self.current_phase_index = 0
        self.running = False

    def _load_config(self):
        import yaml
        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def _api(self, method, path, body=None):
        """Call the server API."""
        url = f"{self.server_url}/api/sessions/{self.session_id}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())

    def _create_agent(self):
        """Create the orchestrator's own OllamaClaudeCodeAgent."""
        from cli_agents.ollama_claude_code import OllamaClaudeCodeAgent
        self.agent = OllamaClaudeCodeAgent(
            name=f"orchestrator-{self.session_id[:6]}",
            model="glm-5.2:cloud",
            workdir=self.workdir,
        )
        self.agent.start()

    def _get_state(self):
        """Get current session state from the server."""
        return self._api("GET", "")

    def _get_messages(self):
        """Get all messages from the session."""
        state = self._get_state()
        return state.get("messages", [])

    def _post_message(self, content, msg_type="system"):
        """Post a message as the orchestrator."""
        return self._api("POST", "/message", {
            "agent_id": "orchestrator",
            "content": content,
            "type": msg_type,
        })

    def _join(self):
        """Join the session as orchestrator."""
        return self._api("POST", "/join", {
            "agent_id": "orchestrator",
            "role": "supervisor",
            "model": "orchestrator",
        })

    def _set_phases(self, phases):
        """Set the phase list on the server."""
        return self._api("POST", "/phases", {"phases": phases})

    def _transition_phase(self, summary=""):
        """Transition to the next phase."""
        return self._api("POST", "/phase/transition", {"summary": summary})

    def _start_agents(self, agents):
        """Start agents for the current phase."""
        try:
            return self._api("POST", "/start", {"agents": agents})
        except Exception as e:
            print(f"[orchestrator] Error starting agents: {e}")
            return {}

    def _stop_agents(self):
        """Stop all running agents."""
        try:
            return self._api("POST", "/stop", {})
        except Exception:
            return {}

    # ===================================================================
    # Phase 1: Propose phases
    # ===================================================================

    def propose_phases(self) -> list[dict]:
        """Read the task and reason about what phases are needed.

        Returns a list of phase dicts: [{name, goal, agents: [{role, ...}]}]
        """
        prompt = f"""You are the orchestrator of an AI council. A human has submitted this task:

TASK: {self.task}
WORKING DIRECTORY: {self.workdir}

Analyze this task and propose a phased workflow. Each phase has:
- name: short identifier (e.g., "exploration", "planning", "execution")
- goal: what should be accomplished in this phase
- agents: list of agent specs, each with role, backend, model, read_only

Available roles: researcher, planner, architect, coder, reviewer, tester, security
Available backends: ollama-claude-code (agentic, can read files/run commands)
Available models: glm-5.2:cloud

Rules:
- Not every task needs all phases. A simple task might need just 1-2 phases.
- Exploration phases need 1 researcher (read_only).
- Planning phases need 1 planner (read_only).
- Plan review phases need 1 reviewer + the planner stays (debate).
- Execution phases need 1+ coders (not read_only).
- Code review phases need 1 reviewer (read_only).
- Keep it minimal — don't add phases that aren't needed.

Respond in JSON format:
```json
[
  {{
    "name": "exploration",
    "goal": "Understand the current codebase structure and dependencies",
    "agents": [{{"id": "researcher-01", "role": "researcher", "backend": "ollama-claude-code", "model": "glm-5.2:cloud", "read_only": true}}]
  }},
  ...
]
```

Only output the JSON, nothing else."""

        response = self.agent.send(prompt, timeout=300)

        # Extract JSON from response
        try:
            # Try to find JSON array in the response
            json_start = response.find("[")
            json_end = response.rfind("]") + 1
            if json_start >= 0 and json_end > json_start:
                phases = json.loads(response[json_start:json_end])
                return phases
        except json.JSONDecodeError:
            pass

        # Fallback: default phases
        return [
            {
                "name": "exploration",
                "goal": "Explore the codebase and understand current state",
                "agents": [{"id": "researcher-01", "role": "researcher",
                             "backend": "ollama-claude-code", "model": "glm-5.2:cloud",
                             "read_only": True}]
            },
            {
                "name": "planning",
                "goal": "Create a plan based on findings",
                "agents": [{"id": "planner-01", "role": "planner",
                             "backend": "ollama-claude-code", "model": "glm-5.2:cloud",
                             "read_only": True}]
            },
            {
                "name": "execution",
                "goal": "Implement the plan",
                "agents": [{"id": "coder-01", "role": "coder",
                             "backend": "ollama-claude-code", "model": "glm-5.2:cloud",
                             "read_only": False}]
            },
            {
                "name": "review",
                "goal": "Review the implementation",
                "agents": [{"id": "reviewer-01", "role": "reviewer",
                             "backend": "ollama-claude-code", "model": "glm-5.2:cloud",
                             "read_only": True}]
            },
        ]

    # ===================================================================
    # Phase 2: Monitor and transition
    # ===================================================================

    def check_phase_completion(self) -> tuple[bool, str]:
        """Check if the current phase goals have been met.

        Returns (is_complete, summary_or_reason).
        """
        state = self._get_state()
        messages = state.get("messages", [])
        phase_info = state.get("phase", "init")
        phases = state.get("phases", [])
        phase_index = state.get("phase_index", 0)

        if not phases or phase_index >= len(phases):
            return True, "No phases defined"

        current_phase = phases[phase_index]
        phase_name = current_phase["name"]
        phase_goal = current_phase.get("goal", "")

        # Get recent messages (last 5)
        recent = messages[-5:] if len(messages) > 5 else messages
        recent_text = "\n".join([
            f"[{m.get('role','?')}] ({m.get('type','message')}) {m.get('content','')[:1000]}"
            for m in recent
        ])

        # Check for DONE signals
        for m in reversed(messages):
            content = m.get("content", "").upper()
            if "DONE:" in content or "DONE." in content:
                # Agent signaled done — check if it's in this phase
                if m.get("timestamp", 0) > state.get("created_at", 0):
                    # Ask the orchestrator to evaluate
                    break

        prompt = f"""You are the council orchestrator. Current phase: {phase_name}
Phase goal: {phase_goal}

Recent messages:
{recent_text}

Evaluate the current phase:

1. Have the phase goals been met?
2. If this is a review/debate phase (plan-review, code-review), check:
   - Did the reviewer raise issues?
   - Has the original author (planner/coder) addressed those issues?
   - Are there unresolved disagreements?
   If there are unresolved issues, the phase is NOT complete — say PHASE_CONTINUE
   so the agents can keep debating.
3. If this is an execution phase, check:
   - Did the coder signal completion?
   - Are there unresolved errors?
   If there are errors, say PHASE_CONTINUE.

Respond with exactly one of:
PHASE_COMPLETE: <brief summary of accomplishments>
PHASE_CONTINUE: <reason why we should wait>"""

        response = self.agent.send(prompt, timeout=120)

        if "PHASE_COMPLETE:" in response:
            summary = response.split("PHASE_COMPLETE:")[1].strip()
            return True, summary
        elif "PHASE_CONTINUE:" in response:
            reason = response.split("PHASE_CONTINUE:")[1].strip()
            return False, reason
        else:
            # Can't parse — default to continue
            return False, f"Orchestrator unsure, continuing. Response: {response[:100]}"

    # ===================================================================
    # Main loop
    # ===================================================================

    def start(self):
        """Start the orchestrator: join session, propose phases, wait for approval."""
        self._join()
        self._create_agent()

        print("[orchestrator] Joined session, proposing phases...")
        self.phases = self.propose_phases()

        # Post proposal to the council
        phase_list = "\n".join([
            f"  {i+1}. {p['name']}: {p.get('goal','')}"
            for i, p in enumerate(self.phases)
        ])
        self._post_message(
            f"ORCHESTRATOR PROPOSAL — Phased Workflow\n\n"
            f"Task: {self.task}\n\n"
            f"Proposed phases:\n{phase_list}\n\n"
            f"Reply 'approve' to start, or suggest changes.",
            "system"
        )

        # Set phases on the server
        self._set_phases(self.phases)
        print(f"[orchestrator] Proposed {len(self.phases)} phases:")
        for i, p in enumerate(self.phases):
            print(f"  {i+1}. {p['name']}: {p.get('goal','')}")
            for a in p.get("agents", []):
                ro = "RO" if a.get("read_only") else "RW"
                print(f"     {a['id']} [{a['role']}] {a['backend']}/{a.get('model','')} {ro}")

    def run(self):
        """Run the full workflow: start phase 1, monitor, transition, repeat."""
        self.running = True

        while self.running:
            state = self._get_state()

            if state.get("status") in ("complete", "stopped", "error"):
                print(f"[orchestrator] Session ended: {state['status']}")
                break

            phase = state.get("phase", "init")
            phase_index = state.get("phase_index", 0)
            phases = state.get("phases", [])

            if not phases:
                print("[orchestrator] No phases defined, waiting...")
                time.sleep(5)
                continue

            if phase_index >= len(phases):
                print("[orchestrator] All phases complete")
                break

            current = phases[phase_index]
            print(f"\n[orchestrator] Phase {phase_index + 1}/{len(phases)}: {phase}")
            print(f"  Goal: {current.get('goal', '')}")

            # Start agents for this phase if not already running
            if state.get("status") != "running":
                agents = current.get("agents", [])
                print(f"  Starting {len(agents)} agents...")
                self._start_agents(agents)

            # Monitor phase
            check_interval = 30  # check every 30 seconds
            phase_start = time.time()
            max_phase_time = 600  # 10 min per phase max
            last_msg_count = len(state.get("messages", []))

            while True:
                time.sleep(check_interval)
                state = self._get_state()

                if state.get("status") in ("stopped", "error"):
                    print(f"  [orchestrator] Phase interrupted: {state['status']}")
                    break

                # Wait for at least one new agent message before checking completion
                current_msg_count = len(state.get("messages", []))
                if current_msg_count <= last_msg_count:
                    print(f"  [orchestrator] Waiting for agent to post ({int(time.time() - phase_start)}s, {current_msg_count}/{last_msg_count} msgs)...")
                    if time.time() - phase_start > max_phase_time:
                        print(f"  [orchestrator] Phase timeout ({max_phase_time}s), forcing transition")
                        self._transition_phase("Phase timed out — no agent response received")
                        break
                    continue

                print(f"  [orchestrator] New messages detected ({current_msg_count - last_msg_count} new), checking completion...")
                last_msg_count = current_msg_count

                # Check if phase is complete
                is_complete, summary = self.check_phase_completion()

                if is_complete:
                    print(f"  [orchestrator] Phase complete: {summary[:100]}")
                    result = self._transition_phase(summary)
                    print(f"  [orchestrator] Transition result: {result}")
                    break

                print(f"  [orchestrator] Continue: {summary[:100]}")

                # Timeout check
                if time.time() - phase_start > max_phase_time:
                    print(f"  [orchestrator] Phase timeout ({max_phase_time}s), forcing transition")
                    self._transition_phase("Phase timed out — forcing transition")
                    break

        self.running = False
        print("[orchestrator] Workflow complete")

    def stop(self):
        """Stop the orchestrator."""
        self.running = False
        if self.agent:
            self.agent.stop()
        self._post_message("Orchestrator signing off.", "system")
        self._api("POST", "/leave", {"agent_id": "orchestrator"})


def main():
    parser = argparse.ArgumentParser(description="Council Orchestrator")
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument("--bus", default="http://10.0.0.8:8080", help="Server URL")
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--workdir", default=".", help="Working directory")
    parser.add_argument("--config", default="council.yaml", help="Config file")
    args = parser.parse_args()

    orch = Orchestrator(
        session_id=args.session,
        server_url=args.bus,
        workdir=args.workdir,
        task=args.task,
        config_path=args.config,
    )

    try:
        orch.start()
        print("\n[orchestrator] Phases proposed. Starting workflow...")
        print("[orchestrator] Press Ctrl+C to stop\n")
        orch.run()
    except KeyboardInterrupt:
        print("\n[orchestrator] Interrupted")
        orch.stop()


if __name__ == "__main__":
    main()