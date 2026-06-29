#!/usr/bin/env python3
"""
Council test — validates the bus, voting, and agent harness end-to-end
without needing Claude Code, Codex, or any external CLI installed.

Runs a simulated council session with 3 Python-based dummy agents
that talk through the bus, call votes, and reach consensus.
"""

import json
import threading
import time
import sys
import os
import urllib.request

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from council_bus import CouncilBus, BusHandler, ThreadingHTTPServer
from council_agent import BusClient, build_prompt, parse_response


def start_bus(port=8750, consensus="majority"):
    """Start bus in a background thread."""
    bus = CouncilBus()
    bus.consensus_rule = consensus
    # Monkey-patch the singleton
    import council_bus
    council_bus.BUS = bus

    server = ThreadingHTTPServer(("127.0.0.1", port), BusHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, bus, f"http://127.0.0.1:{port}"


def dummy_agent(bus_url, agent_id, role, responses, delay=0.5):
    """Simulate an agent that posts messages and responds to votes."""
    client = BusClient(bus_url)
    client.join(agent_id, role, f"dummy-{role}")

    for resp in responses:
        time.sleep(delay)

        # Post message
        if resp.get("message"):
            client.post_message(agent_id, resp["message"])

        # Respond to votes
        if resp.get("vote_id") and resp.get("vote_response"):
            client.respond_vote(agent_id, resp["vote_id"], resp["vote_response"], resp.get("rationale", ""))

        # Propose vote
        if resp.get("propose_vote"):
            result = client.propose_vote(agent_id, resp["propose_vote"], resp.get("options"))
            # Store the vote_id for others to use
            resp["actual_vote_id"] = result.get("vote_id")


def run_test():
    print("=" * 60)
    print("  AI COUNCIL — Integration Test")
    print("=" * 60)

    # Start bus
    port = 8750
    server, bus, bus_url = start_bus(port, "majority")
    print(f"\n  Bus started on port {port}")

    # Wait for bus
    time.sleep(0.5)

    client = BusClient(bus_url)

    # 1. Join all three agents
    print("\n[1] Joining agents...")
    for aid, role in [("coord", "coordinator"), ("rev1", "reviewer"), ("cod1", "coder")]:
        r = client.join(aid, role, f"test-{role}")
        assert "error" not in r, f"Join failed: {r}"
    room = client.get_room()
    assert len(room["members"]) == 3, f"Expected 3 members, got {len(room['members'])}"
    print(f"  3 agents joined: {list(room['members'].keys())}")

    # 2. Post task message
    print("\n[2] Posting task...")
    msg = client.post_message("coord", "TASK: Add hello world function to src/main.py", "task")
    assert msg.get("id"), "Message post failed"
    print(f"  Task posted: {msg['id'][:8]}")

    # 3. Reviewer posts analysis
    print("\n[3] Reviewer analyzes repo...")
    msg = client.post_message("rev1", "I checked the repo. src/main.py exists and uses snake_case. The function should follow that pattern.")
    print(f"  Reviewer message posted")

    # 4. Coordinator proposes vote
    print("\n[4] Coordinator proposes plan vote...")
    vote = client.propose_vote("coord", "Plan: add hello_world() to src/main.py with test", ["approve", "reject", "request_changes"])
    vote_id = vote["vote_id"]
    print(f"  Vote: {vote_id}")

    # 5. All agents vote
    print("\n[5] Agents vote...")
    for aid, resp, rationale in [
        ("coord", "approve", "I proposed it"),
        ("rev1", "approve", "Plan follows repo patterns"),
        ("cod1", "approve", "Ready to implement"),
    ]:
        r = client.respond_vote(aid, vote_id, resp, rationale)
        print(f"  {aid}: {resp} -> {r['status']}")

    # Check vote closed
    room = client.get_room()
    assert len(room["open_votes"]) == 0, "Vote should be closed"
    print(f"  Vote closed! Open votes: {len(room['open_votes'])}")

    # 6. Coder posts implementation
    print("\n[6] Coder implements...")
    client.post_message("cod1", "IMPLEMENTATION_COMPLETE: Added hello_world() to src/main.py and test_hello.py. All tests pass.")
    print(f"  Implementation posted")

    # 7. Coordinator proposes acceptance vote
    print("\n[7] Coordinator proposes acceptance vote...")
    vote2 = client.propose_vote("coord", "Accept the implementation?", ["approve", "reject", "request_changes"])
    vote2_id = vote2["vote_id"]
    print(f"  Vote: {vote2_id}")

    # 8. Reviewer requests changes
    print("\n[8] Reviewer requests changes...")
    r = client.respond_vote("rev1", vote2_id, "request_changes", "Need to add type hints")
    print(f"  Reviewer: request_changes -> {r['status']}")

    # Coder and coord approve
    client.respond_vote("cod1", vote2_id, "approve", "I'll add type hints")
    r = client.respond_vote("coord", vote2_id, "approve", "Acceptable")
    print(f"  Vote result: {r['status']}, tally: {r.get('tally')}")

    # 9. Check consensus rules
    print("\n[9] Testing unanimous consensus...")
    import council_bus
    council_bus.BUS.consensus_rule = "unanimous"
    vote3 = client.propose_vote("coord", "Final acceptance with type hints?", ["approve", "reject"])
    vote3_id = vote3["vote_id"]
    client.respond_vote("coord", vote3_id, "approve", "yes")
    client.respond_vote("cod1", vote3_id, "approve", "yes")
    r = client.respond_vote("rev1", vote3_id, "reject", "Still needs docstring")
    print(f"  Unanimous vote (1 reject): {r['status']}")
    assert r["status"] == "failed_no_consensus", f"Expected failed_no_consensus, got {r['status']}"

    # Now all approve
    vote4 = client.propose_vote("coord", "Final acceptance with type hints and docstring?", ["approve", "reject"])
    vote4_id = vote4["vote_id"]
    client.respond_vote("coord", vote4_id, "approve", "yes")
    client.respond_vote("cod1", vote4_id, "approve", "yes")
    r = client.respond_vote("rev1", vote4_id, "approve", "Looks good now")
    print(f"  Unanimous vote (all approve): {r['status']}")
    assert r["status"] == "approved_unanimous", f"Expected approved_unanimous, got {r['status']}"

    # 10. Verify conversation
    print("\n[10] Verifying conversation...")
    msgs = client.get_messages(since=0)
    total = msgs.get("count", 0)
    print(f"  Total messages: {total}")
    assert total > 10, f"Expected >10 messages, got {total}"

    # Show conversation
    print("\n  === Conversation log ===")
    for m in msgs["messages"]:
        role = m["role"]
        content = m["content"]
        if len(content) > 100:
            content = content[:100] + "..."
        mtype = m.get("type", "message")
        marker = ">>>" if mtype != "message" else "  "
        print(f"  {marker} [{role}] {content}")

    # Test agent harness parsing
    print("\n[11] Testing agent response parser...")
    test_response = """
I reviewed the plan. It looks good overall.

VOTE: abc12345 approve -- The plan follows repo patterns correctly.

PROPOSE_VOTE: Should we add error handling? | options: yes, no, defer

DONE: review complete
"""
    actions = parse_response(test_response)
    assert actions["votes"][0]["vote_id"] == "abc12345", f"Vote parse failed: {actions['votes']}"
    assert actions["votes"][0]["response"] == "approve", f"Vote response parse failed"
    assert actions["propose_vote"]["proposal"] == "Should we add error handling?", f"Propose vote parse failed"
    assert actions["done"] == True, "Done flag not parsed"
    print(f"  Parse OK: {len(actions['votes'])} vote(s), propose={actions['propose_vote'] is not None}, done={actions['done']}")

    print("\n" + "=" * 60)
    print("  ALL TESTS PASSED")
    print("=" * 60)

    # Cleanup
    server.shutdown()


if __name__ == "__main__":
    run_test()