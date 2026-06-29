#!/usr/bin/env python3
"""
Council Integration Test — validates config-driven backends, dynamic
team composition, the bus, voting, and the agent harness.
"""

import json
import os
import sys
import time
import threading
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from council_backends import BackendRegistry, load_config, Backend
from council_bus import BusClient, start_bus_server, build_prompt, parse_response, CouncilBus, ThreadingHTTPServer, BusHandler
from council_supervisor import analyze_task, compose_team


def test_config_loading():
    """Test that council.yaml loads correctly."""
    print("[1] Loading council.yaml...")
    config_path = str(Path(__file__).parent / "council.yaml")
    config = load_config(config_path)
    
    assert "backends" in config, "Missing backends section"
    assert "roles" in config, "Missing roles section"
    assert "consensus" in config, "Missing consensus section"
    
    backends = config["backends"]
    assert "claude-code" in backends, "Missing claude-code backend"
    assert "codex" in backends, "Missing codex backend"
    assert "ollama" in backends, "Missing ollama backend"
    assert "copilot" in backends, "Missing copilot backend"
    assert "claude-ollama" in backends, "Missing claude-ollama backend"
    
    roles = config["roles"]
    assert "supervisor" in roles, "Missing supervisor role"
    assert "planner" in roles, "Missing planner role"
    assert "coder" in roles, "Missing coder role"
    assert "reviewer" in roles, "Missing reviewer role"
    assert "architect" in roles, "Missing architect role"
    assert "tester" in roles, "Missing tester role"
    assert "researcher" in roles, "Missing researcher role"
    assert "security" in roles, "Missing security role"
    
    print(f"  Config loaded: {len(backends)} backends, {len(roles)} roles")
    print("  PASS")


def test_backend_registry():
    """Test backend registry."""
    print("\n[2] Testing backend registry...")
    config_path = str(Path(__file__).parent / "council.yaml")
    config = load_config(config_path)
    registry = BackendRegistry.from_config(config)
    
    # Check claude-code backend
    claude = registry.get("claude-code")
    assert claude is not None, "claude-code backend not found"
    assert claude.prompt_mode == "argument", f"Expected argument, got {claude.prompt_mode}"
    assert len(claude.models) >= 2, f"Expected 2+ models, got {len(claude.models)}"
    
    # Check model lookup
    opus = claude.get_model("opus")
    assert opus is not None, "opus model not found"
    assert "planning" in opus.get("best_for", []), f"opus should be best for planning"
    
    # Check ollama backend
    ollama = registry.get("ollama")
    assert ollama is not None, "ollama backend not found"
    assert ollama.prompt_mode == "stdin", f"Expected stdin, got {ollama.prompt_mode}"
    
    # Check claude-ollama (Claude Code with Ollama model)
    claude_ollama = registry.get("claude-ollama")
    assert claude_ollama is not None, "claude-ollama backend not found"
    assert "ANTHROPIC_BASE_URL" in claude_ollama.env, "claude-ollama should set ANTHROPIC_BASE_URL"
    
    # Available backends (only shell should be available since no CLIs installed)
    available = registry.list_available()
    assert "shell" in available, "shell backend should always be available"
    
    print(f"  Available backends: {available}")
    print(f"  claude-code models: {claude.list_models()}")
    print(f"  ollama models: {ollama.list_models()}")
    print(f"  claude-ollama env: {claude_ollama.env}")
    print("  PASS")


def test_task_analysis():
    """Test dynamic task analysis."""
    print("\n[3] Testing task analysis...")
    
    # Simple task
    simple = analyze_task("Fix typo in README")
    assert simple["complexity"] == "simple", f"Expected simple, got {simple['complexity']}"
    assert "coder" in simple["needs"]
    assert "reviewer" in simple["needs"]
    assert "planner" not in simple["needs"]
    print(f"  Simple task: {simple['complexity']}, team size: {len(simple['needs'])}")
    
    # Moderate task (1-2 complex keywords)
    moderate = analyze_task("Add a new API endpoint with error handling")
    assert moderate["complexity"] == "moderate", f"Expected moderate, got {moderate['complexity']}"
    assert "planner" in moderate["needs"], "Moderate task should have planner"
    print(f"  Moderate task: {moderate['complexity']}, team size: {len(moderate['needs'])}")
    
    # Complex task
    complex_task = analyze_task("Migrate authentication from session to JWT, refactor for distributed deployment, add security review and integration tests")
    assert complex_task["complexity"] == "complex", f"Expected complex, got {complex_task['complexity']}"
    assert "security" in complex_task["needs"], "Complex auth task should have security"
    assert "architect" in complex_task["needs"], "Complex refactor should have architect"
    assert "tester" in complex_task["needs"], "Complex test task should have tester"
    assert complex_task["needs"].count("coder") == 2, "Complex task should have 2 coders"
    print(f"  Complex task: {complex_task['complexity']}, team size: {len(complex_task['needs'])}")
    
    # Security task
    security_task = analyze_task("Fix the authentication vulnerability in the login flow")
    assert "security" in security_task["needs"], "Security task should have security reviewer"
    print(f"  Security task: detected security reviewer")
    
    print("  PASS")


def test_team_composition():
    """Test team composition with config."""
    print("\n[4] Testing team composition...")
    config_path = str(Path(__file__).parent / "council.yaml")
    config = load_config(config_path)
    registry = BackendRegistry.from_config(config)
    available = registry.list_available()
    
    analysis = analyze_task("Complex migration with security review and testing")
    agents = compose_team(analysis, config, registry, available)
    
    assert len(agents) > 0, "Should have composed at least 1 agent"
    assert all("id" in a for a in agents), "All agents should have IDs"
    assert all("role" in a for a in agents), "All agents should have roles"
    assert all("backend" in a for a in agents), "All agents should have backends"
    
    print(f"  Composed {len(agents)} agents:")
    for a in agents:
        ro = "RO" if a["read_only"] else "RW"
        print(f"    {a['id']:15s} [{a['role']:12s}] {a['backend']:15s} {ro}")
    print("  PASS")


def test_bus():
    """Test bus server."""
    print("\n[5] Testing bus...")
    
    # Start bus in a thread
    bus = CouncilBus()
    import council_bus
    council_bus.BUS = bus
    
    server = ThreadingHTTPServer(("127.0.0.1", 8751), BusHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    
    client = BusClient("http://127.0.0.1:8751")
    
    # Join agents
    for aid, role, model in [("sup", "supervisor", "hermes"), ("plan1", "planner", "opus"),
                              ("cod1", "coder", "codex"), ("rev1", "reviewer", "sonnet")]:
        r = client.join(aid, role, model)
        assert "error" not in r, f"Join failed: {r}"
    
    room = client.get_room()
    assert len(room["members"]) == 4, f"Expected 4 members, got {len(room['members'])}"
    print(f"  4 agents joined")
    
    # Post messages
    client.post_message("sup", "Council task: implement feature X", "task")
    client.post_message("plan1", "I'll analyze the repo and create a plan")
    print(f"  Messages posted")
    
    # Vote
    vote = client.propose_vote("sup", "Approve plan for feature X", ["approve", "reject", "request_changes"])
    vote_id = vote["vote_id"]
    
    for aid, resp in [("sup", "approve"), ("plan1", "approve"), ("cod1", "approve"), ("rev1", "approve")]:
        r = client.respond_vote(aid, vote_id, resp, "ok")
    
    room = client.get_room()
    assert len(room["open_votes"]) == 0, "Vote should be closed"
    print(f"  Vote closed with consensus")
    
    # Test prompt builder with role description
    msgs = client.get_messages(since=0)
    prompt = build_prompt(
        role="reviewer",
        role_description="You are a code reviewer. Check for bugs and patterns.",
        conversation=msgs["messages"],
        open_votes=[],
        agent_id="rev1",
        round_num=1,
        workdir="/tmp",
    )
    assert "REVIEWER" in prompt, f"Role should be in prompt: {prompt[:200]}"
    assert "You are a code reviewer" in prompt, "Role description should be in prompt"
    assert "COUNCIL CONVERSATION" in prompt, "Conversation section should be in prompt"
    print(f"  Prompt builder works ({len(prompt)} chars)")
    
    # Test parse_response
    actions = parse_response("""
I reviewed the code. It looks good.

VOTE: abc12345 approve -- follows repo patterns

DONE: review complete
""")
    assert actions["votes"][0]["vote_id"] == "abc12345"
    assert actions["done"] == True
    print(f"  Response parser works: {len(actions['votes'])} vote(s), done={actions['done']}")
    
    server.shutdown()
    print("  PASS")


def test_backend_command_building():
    """Test that backend commands are built correctly."""
    print("\n[6] Testing backend command building...")
    config_path = str(Path(__file__).parent / "council.yaml")
    config = load_config(config_path)
    registry = BackendRegistry.from_config(config)
    
    # Claude Code read-only command
    claude = registry.get("claude-code")
    cmd = claude.build_command("review this code", read_only=True, model_id="sonnet")
    assert "claude" in cmd, f"Expected claude in cmd: {cmd}"
    assert "-p" in cmd, f"Expected -p flag: {cmd}"
    assert "Read" in cmd, f"Expected Read in allowedTools: {cmd}"
    assert "--model" in cmd and "sonnet" in cmd, f"Expected model sonnet: {cmd}"
    print(f"  claude-code RO: {' '.join(cmd[:6])}...")
    
    # Claude Code write command
    cmd = claude.build_command("implement this", read_only=False, model_id="")
    assert "Read,Edit,Write,Bash" in cmd, f"Expected write tools: {cmd}"
    print(f"  claude-code RW: {' '.join(cmd[:6])}...")
    
    # Codex command
    codex = registry.get("codex")
    cmd = codex.build_command("implement this", read_only=False, model_id="")
    assert "codex" in cmd, f"Expected codex in cmd: {cmd}"
    assert "exec" in cmd, f"Expected exec: {cmd}"
    assert "--full-auto" in cmd, f"Expected --full-auto: {cmd}"
    print(f"  codex RW: {' '.join(cmd)}")
    
    # Ollama command (stdin mode)
    ollama = registry.get("ollama")
    cmd = ollama.build_command("review this", read_only=True, model_id="glm-5.2")
    assert "ollama" in cmd, f"Expected ollama in cmd: {cmd}"
    assert "glm-5.2" in cmd, f"Expected glm-5.2 model: {cmd}"
    print(f"  ollama: {' '.join(cmd)}")
    
    print("  PASS")


def main():
    print("=" * 60)
    print("  AI COUNCIL — Integration Test")
    print("=" * 60)
    
    test_config_loading()
    test_backend_registry()
    test_task_analysis()
    test_team_composition()
    test_bus()
    test_backend_command_building()
    
    print("\n" + "=" * 60)
    print("  ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()