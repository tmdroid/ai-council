#!/usr/bin/env python3
"""
Council Supervisor — Dynamic agent composition and orchestration.

The supervisor (run by Hermes or standalone) does:
  1. Loads council.yaml to see which backends are available
  2. Analyzes the task to determine what kind of team is needed
  3. Spawns the right mix of agents (planner, coder, reviewer, etc.)
  4. Joins the bus as supervisor, posts the task, and monitors
  5. Gates votes, presents results to the human

The supervisor can also be run in "auto" mode where it does initial repo
exploration to inform its team composition decision.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import threading
import textwrap
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from council_backends import BackendRegistry, load_config
from council_bus import BusClient, start_bus_server


# =============================================================================
# Task Analysis — determines team composition from task description
# =============================================================================

def analyze_task(task_description: str, repo_path: str = None) -> dict:
    """
    Analyze a task description and repo to determine what agents are needed.
    
    Returns a dict with:
      complexity: simple | moderate | complex
      needs: list of role names
      rationale: why these roles
    """
    task_lower = task_description.lower()
    needs = []
    rationale = []

    # --- Detect task type from keywords ---

    # Security-related
    security_keywords = ["security", "auth", "authentication", "authorization",
                         "vulnerability", "injection", "xss", "csrf", "token",
                         "password", "encrypt", "ssl", "cors"]
    if any(kw in task_lower for kw in security_keywords):
        needs.append("security")
        rationale.append("Security-sensitive task — adding security reviewer")

    # Architecture-related
    arch_keywords = ["architect", "design", "refactor", "restructure", "migrate",
                     "migration", "system", "framework", "infrastructure"]
    if any(kw in task_lower for kw in arch_keywords):
        needs.append("architect")
        rationale.append("Architecture/refactoring task — adding architect")

    # Testing-related
    test_keywords = ["test", "testing", "coverage", "unit test", "integration test",
                     "tdd", "spec"]
    if any(kw in task_lower for kw in test_keywords):
        needs.append("tester")
        rationale.append("Testing-focused task — adding dedicated tester")

    # Research/exploration
    research_keywords = ["explore", "investigate", "understand", "analyze",
                         "research", "document", "audit"]
    if any(kw in task_lower for kw in research_keywords):
        needs.append("researcher")
        rationale.append("Research/exploration task — adding researcher")

    # Complexity heuristics
    complexity_indicators = 0
    complex_keywords = ["multi-step", "multiple files", "database", "api",
                        "endpoint", "service", "microservice", "concurrent",
                        "async", "real-time", "distributed", "migration",
                        "migrate", "refactor", "restructure", "integration",
                        "deployment", "pipeline", "orchestration", "workflow",
                        "authentication", "authorization", "infrastructure"]
    for kw in complex_keywords:
        if kw in task_lower:
            complexity_indicators += 1

    # Also count the number of role-specific needs detected — more needs = more complex
    complexity_indicators += len([n for n in needs if n not in ("coder", "reviewer")])

    if complexity_indicators >= 4:
        complexity = "complex"
    elif complexity_indicators >= 2:
        complexity = "moderate"
    else:
        complexity = "simple"

    # --- Determine base team ---

    # Every task needs a coder (to implement) and a reviewer (to review)
    if "coder" not in needs:
        needs.append("coder")
        rationale.append("Every task needs at least one coder")

    if "reviewer" not in needs:
        needs.append("reviewer")
        rationale.append("Every task needs at least one reviewer")

    # Moderate+ tasks need a planner
    if complexity in ("moderate", "complex") and "planner" not in needs:
        needs.append("planner")
        rationale.append(f"{complexity} task — adding planner for structured plan")

    # Complex tasks get a second coder
    if complexity == "complex":
        needs.append("coder")  # second coder
        rationale.append("Complex task — adding second coder for parallel work")

    # Repo size check (if path provided)
    if repo_path and os.path.isdir(repo_path):
        try:
            import subprocess
            result = subprocess.run(
                ["find", repo_path, "-name", "*.py", "-o", "-name", "*.kt",
                 "-o", "-name", "*.ts", "-o", "-name", "*.js", "-o", "-name", "*.java",
                 "-o", "-name", "*.go", "-o", "-name", "*.rs"],
                capture_output=True, text=True, timeout=5
            )
            file_count = len([l for l in result.stdout.strip().split("\n") if l])
            if file_count > 50:
                if "researcher" not in needs:
                    needs.append("researcher")
                    rationale.append(f"Large repo ({file_count} source files) — adding researcher")
        except Exception:
            pass

    return {
        "complexity": complexity,
        "needs": needs,
        "rationale": rationale,
    }


# =============================================================================
# Agent Composer — turns analysis into concrete agent specs
# =============================================================================

def compose_team(analysis: dict, config: dict, registry: BackendRegistry,
                 available_backends: list) -> list:
    """
    Turn the task analysis into concrete agent specs with backends and models.
    
    Assigns backends to roles based on:
      1. Role's default_backend from YAML
      2. Availability of that backend
      3. Distribution — try to use different backends for different roles
         (so reviewer and coder aren't the same model)
      4. Never use 'shell' backend when real AI backends are available
    """
    roles_cfg = config.get("roles", {})
    agents = []
    used_backends = set()
    role_counts = {}

    # Filter out shell from available if we have real backends
    real_backends = [b for b in available_backends if b != "shell"]
    backend_pool = real_backends if real_backends else available_backends

    for role in analysis["needs"]:
        count = role_counts.get(role, 0) + 1
        role_counts[role] = count
        agent_id = f"{role}-{count:02d}"

        role_cfg = roles_cfg.get(role, {})

        # Pick backend: prefer role's default, then any unused, then any available
        default_backend = role_cfg.get("default_backend", "")
        
        # Build preference list: default first, then unused real backends, then any
        preferred = [default_backend]
        preferred += [b for b in backend_pool if b not in used_backends and b not in preferred]
        preferred += [b for b in backend_pool if b not in preferred]

        backend_name = None
        for candidate in preferred:
            if candidate and registry.get(candidate) and registry.get(candidate).is_available():
                backend_name = candidate
                break

        if not backend_name:
            print(f"WARNING: No backend available for role {role}, skipping", file=sys.stderr)
            continue

        # Don't mark ollama as "used" — we can run multiple agents on the same Ollama
        # since it supports concurrent requests. Only mark non-ollama backends as used
        # to avoid assigning the same CLI to multiple agents.
        if backend_name not in ("ollama", "shell"):
            used_backends.add(backend_name)

        # Pick model
        backend = registry.get(backend_name)
        model_id = role_cfg.get("default_model", "")
        if model_id and not backend.get_model(model_id):
            models = backend.list_models()
            if models:
                model_id = models[0]
        elif not model_id:
            models = backend.list_models()
            if models:
                model_id = models[0]

        agents.append({
            "id": agent_id,
            "role": role,
            "backend": backend_name,
            "model": model_id,
            "read_only": role_cfg.get("read_only", True),
        })

    return agents


# =============================================================================
# Supervisor — orchestrates the council session
# =============================================================================

def run_session(task, workdir, config_path, explicit_agents=None,
                consensus=None, auto_compose=True, verbose=True):
    """Run a full council session."""
    
    config = load_config(config_path)
    registry = BackendRegistry.from_config(config)
    available = registry.list_available()

    if verbose:
        print("=" * 60)
        print("  AI COUNCIL — Supervisor")
        print("=" * 60)
        print(f"\n  Task: {task}")
        print(f"  Repo: {workdir}")
        print(f"  Available backends: {available}")
        if not available:
            print("\n  FATAL: No backends available. Check council.yaml and install CLIs.")
            return

    # --- Determine team composition ---
    if explicit_agents:
        agents = explicit_agents
        if verbose:
            print(f"\n  Team: explicitly configured ({len(agents)} agents)")
    elif auto_compose:
        if verbose:
            print(f"\n  Analyzing task...")
        analysis = analyze_task(task, workdir)
        agents = compose_team(analysis, config, registry, available)
        if verbose:
            print(f"  Complexity: {analysis['complexity']}")
            print(f"  Team composition rationale:")
            for r in analysis["rationale"]:
                print(f"    - {r}")
            print(f"\n  Team ({len(agents)} agents):")
            for a in agents:
                model_label = f"{a['backend']}/{a['model']}" if a.get("model") else a["backend"]
                ro = "RO" if a["read_only"] else "RW"
                print(f"    {a['id']:15s} [{a['role']:12s}] {model_label:30s} {ro}")
    else:
        # Default: planner + coder + reviewer
        agents = []
        for role, backend_name, model_id in [
            ("planner", "claude-code", "opus"),
            ("coder", "codex", "gpt-5.5"),
            ("reviewer", "claude-code", "sonnet"),
        ]:
            b = registry.get(backend_name)
            if b and b.is_available():
                agents.append({"id": f"{role}-01", "role": role, "backend": backend_name,
                              "model": model_id, "read_only": role not in ("coder", "tester")})

    if not agents:
        print("\n  FATAL: No agents could be composed. Check config and backends.", file=sys.stderr)
        return

    # --- Start the bus ---
    session_cfg = config.get("session", {})
    port = session_cfg.get("bus_port", 0) or 8747
    bus_url = f"http://127.0.0.1:{port}"
    consensus_rule = consensus or config.get("consensus", {}).get("rule", "majority")

    if verbose:
        print(f"\n  Starting bus on port {port} (consensus: {consensus_rule})...")

    # Start bus in a thread
    bus_thread = threading.Thread(
        target=start_bus_server,
        args=(port, "127.0.0.1", consensus_rule),
        daemon=True
    )
    bus_thread.start()
    time.sleep(1)

    # --- Supervisor joins ---
    client = BusClient(bus_url)
    client.join("supervisor", "supervisor", "hermes")

    # Post the task
    task_msg = f"COUNCIL TASK: {task}\n\nWorking directory: {workdir}\nConsensus: {consensus_rule}\n\n---\nThe supervisor has assembled this council. Review the task, discuss the approach, and agree on a plan before implementing. Use PROPOSE_VOTE to call votes.\n"
    client.post_message("supervisor", task_msg, "task")

    if verbose:
        print(f"\n  Supervisor joined and posted task")

    # --- Spawn agents ---
    agent_script = str(Path(__file__).parent / "council_agent.py")
    procs = []

    for agent in agents:
        cmd = [
            sys.executable, agent_script,
            "--config", config_path,
            "--bus", bus_url,
            "--role", agent["role"],
            "--backend", agent["backend"],
            "--agent-id", agent["id"],
            "--workdir", workdir,
        ]
        if agent.get("model"):
            cmd += ["--model", agent["model"]]
        if agent.get("read_only"):
            cmd += ["--read-only"]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        if verbose:
            print(f"  Spawned {agent['id']} (pid={proc.pid})")

    # --- Monitor ---
    if verbose:
        print(f"\n  Council session active. Monitoring...")
        print(f"  (Ctrl+C to end)\n")

    stop_event = threading.Event()
    monitor_thread = threading.Thread(target=monitor_bus, args=(bus_url, stop_event), daemon=True)
    monitor_thread.start()

    try:
        while True:
            for proc in procs:
                if proc.poll() is not None:
                    pass  # agent exited
            if all(p.poll() is not None for p in procs):
                if verbose:
                    print("\n  All agents have exited.")
                break
            time.sleep(2)
    except KeyboardInterrupt:
        if verbose:
            print("\n\n  Ending council session...")

    # Cleanup
    stop_event.set()
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # Final summary
    if verbose:
        print("\n" + "=" * 60)
        print("  COUNCIL SESSION ENDED")
        print("=" * 60)
        try:
            msgs = client.get_messages(since=0)
            print(f"  Total messages: {msgs.get('count', 0)}")
        except Exception:
            pass

    # Leave
    try:
        client.leave("supervisor")
    except Exception:
        pass


def monitor_bus(bus_url, stop_event):
    """Print bus activity to the console."""
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
                if len(content) > 200:
                    content = content[:200] + "..."
                marker = ">>>" if mtype != "message" else "  "
                print(f"  {marker} [{role}] {content}")
        except Exception:
            pass
        time.sleep(1.5)


# =============================================================================
# Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Council Supervisor — dynamic agent orchestration")
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--workdir", default=".", help="Repository path")
    parser.add_argument("--config", default=str(Path(__file__).parent / "council.yaml"),
                       help="Path to council.yaml")
    parser.add_argument("--consensus", default=None,
                       choices=["majority", "supermajority", "unanimous"],
                       help="Override consensus rule from config")
    parser.add_argument("--no-auto", action="store_true",
                       help="Don't auto-compose team — use default planner+coder+reviewer")
    parser.add_argument("--dry-run", action="store_true",
                       help="Analyze task and show team composition without running")
    args = parser.parse_args()

    config = load_config(args.config)
    registry = BackendRegistry.from_config(config)
    available = registry.list_available()

    if args.dry_run:
        print("=" * 60)
        print("  AI COUNCIL — Dry Run (Task Analysis)")
        print("=" * 60)
        print(f"\n  Task: {args.task}")
        print(f"  Repo: {args.workdir}")
        print(f"  Available backends: {available}")

        analysis = analyze_task(args.task, args.workdir)
        agents = compose_team(analysis, config, registry, available)

        print(f"\n  Complexity: {analysis['complexity']}")
        print(f"\n  Rationale:")
        for r in analysis["rationale"]:
            print(f"    - {r}")
        print(f"\n  Proposed team ({len(agents)} agents):")
        for a in agents:
            model_label = f"{a['backend']}/{a['model']}" if a.get("model") else a["backend"]
            ro = "RO" if a["read_only"] else "RW"
            print(f"    {a['id']:15s} [{a['role']:12s}] {model_label:30s} {ro}")
        print()
        return

    run_session(
        task=args.task,
        workdir=args.workdir,
        config_path=args.config,
        consensus=args.consensus,
        auto_compose=not args.no_auto,
    )


if __name__ == "__main__":
    main()