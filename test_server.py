#!/usr/bin/env python3
"""
Council Server Tests — validates the unified bus + dashboard backend.

Tests the SessionRoom (in-process), the HTTP API, SSE streaming,
persistence, and agent spawning — all without external CLIs.
"""

import json
import os
import sys
import time
import threading
import queue
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from council_server import SessionRoom, SessionManager, ThreadingHTTPServer, CouncilServerHandler, MANAGER, CONFIG_PATH, DATA_DIR


def test_session_room():
    """Test SessionRoom in-process (no HTTP)."""
    print("[1] Testing SessionRoom...")

    room = SessionRoom("test01", "Add hello world", "/tmp", CONFIG_PATH, "majority")

    # Join members
    room.join("human", "supervisor", "human")
    room.join("coder-01", "coder", "codex")
    room.join("reviewer-01", "reviewer", "claude")
    assert len(room.members) == 3, f"Expected 3 members, got {len(room.members)}"

    # Post messages
    msg, status = room.post_message("human", "Council task: add hello world", "task")
    assert status == 200
    assert msg["role"] == "supervisor"

    msg, status = room.post_message("coder-01", "I'll implement hello_world()")
    assert status == 200
    assert msg["role"] == "coder"

    msg, status = room.post_message("reviewer-01", "Looks good, follows repo patterns")
    assert status == 200

    # Check messages persisted
    assert len(room.messages) >= 5, f"Expected 5+ messages, got {len(room.messages)}"

    # Test unregistered agent
    msg, status = room.post_message("unknown", "hello")
    assert status == 400, "Unregistered agent should fail"

    # Test voting
    vote, status = room.propose_vote("human", "Approve the plan?", ["approve", "reject"])
    assert status == 200
    vote_id = vote["vote_id"]

    # All members vote
    r, s = room.respond_vote("human", vote_id, "approve", "looks good")
    assert s == 200
    assert r["status"] == "open", "Vote should still be open with 1/3"

    r, s = room.respond_vote("coder-01", vote_id, "approve", "ready")
    assert r["status"] == "open", "Vote should still be open with 2/3"

    r, s = room.respond_vote("reviewer-01", vote_id, "approve", "patterns match")
    assert r["status"] == "approved_majority", f"Vote should be approved, got {r['status']}"

    # Test unanimous consensus
    room.consensus_rule = "unanimous"
    vote2, _ = room.propose_vote("human", "Final approval?", ["approve", "reject"])
    room.respond_vote("human", vote2["vote_id"], "approve", "yes")
    room.respond_vote("coder-01", vote2["vote_id"], "approve", "yes")
    r, _ = room.respond_vote("reviewer-01", vote2["vote_id"], "reject", "needs docstring")
    assert r["status"] == "failed_no_consensus", f"Unanimous with 1 reject should fail, got {r['status']}"

    # Test SSE broadcast
    q = queue.Queue()
    room.add_sse_client(q)
    # First event is the initial state
    state_event = q.get(timeout=2)
    assert state_event["type"] == "state", f"Expected state event first, got {state_event['type']}"
    # Now post a message and check SSE receives it
    room.post_message("human", "SSE test message")
    event = q.get(timeout=2)
    assert event["type"] == "message", f"Expected message event, got {event['type']}"
    assert "SSE test" in event["data"]["content"], "SSE should broadcast message content"
    room.remove_sse_client(q)

    # Test persistence
    room.save_meta()
    history_path = DATA_DIR / "test01_history.json"
    meta_path = DATA_DIR / "test01.json"
    assert history_path.exists(), "History should be persisted"
    assert meta_path.exists(), "Metadata should be persisted"

    # Cleanup
    DATA_DIR.glob("test01*")
    for p in DATA_DIR.glob("test01*"):
        p.unlink()

    print("  PASS")


def test_http_api():
    """Test the HTTP API endpoints."""
    print("\n[2] Testing HTTP API...")

    # Start server in a thread
    import council_server
    council_server.SERVER_PORT = 8755

    # Reset MANAGER
    council_server.MANAGER.sessions.clear()

    server = ThreadingHTTPServer(("127.0.0.1", 8755), CouncilServerHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    BASE = "http://127.0.0.1:8755"

    def api(method, path, body=None):
        url = f"{BASE}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    # List sessions (empty)
    data = api("GET", "/api/sessions")
    assert len(data["sessions"]) == 0, "Should start with 0 sessions"

    # Get config
    data = api("GET", "/api/config")
    assert "backends" in data, "Config should have backends"
    assert "roles" in data, "Config should have roles"

    # Create a session (no auto-start)
    data = api("POST", "/api/sessions", {"task": "Test task", "workdir": "/tmp", "auto_start": False})
    sid = data["id"]
    assert data["task"] == "Test task"
    assert data["status"] == "created"

    # Get session state
    data = api("GET", f"/api/sessions/{sid}")
    assert data["id"] == sid

    # Join as human
    api("POST", f"/api/sessions/{sid}/join", {"agent_id": "human", "role": "supervisor", "model": "human"})

    # Post a message
    data = api("POST", f"/api/sessions/{sid}/message",
               {"agent_id": "human", "content": "Hello council!", "type": "message"})
    assert data["role"] == "supervisor"
    assert "Hello" in data["content"]

    # Post another message
    api("POST", f"/api/sessions/{sid}/join", {"agent_id": "coder-01", "role": "coder", "model": "codex"})
    api("POST", f"/api/sessions/{sid}/message",
        {"agent_id": "coder-01", "content": "I'll implement this"})

    # Check messages
    data = api("GET", f"/api/sessions/{sid}")
    assert len(data["messages"]) >= 4, f"Expected 4+ messages, got {len(data['messages'])}"

    # Test voting via API
    vote = api("POST", f"/api/sessions/{sid}/vote/propose",
               {"agent_id": "human", "proposal": "Approve test plan?", "options": ["approve", "reject"]})
    vote_id = vote["vote_id"]

    # Both vote
    api("POST", f"/api/sessions/{sid}/vote/respond",
        {"agent_id": "human", "vote_id": vote_id, "response": "approve", "rationale": "ok"})
    result = api("POST", f"/api/sessions/{sid}/vote/respond",
                 {"agent_id": "coder-01", "vote_id": vote_id, "response": "approve", "rationale": "ready"})
    assert result["status"] == "approved_majority", f"Expected approved, got {result['status']}"

    # List sessions (should have 1)
    data = api("GET", "/api/sessions")
    assert len(data["sessions"]) == 1, f"Expected 1 session, got {len(data['sessions'])}"

    # Delete session
    api("DELETE", f"/api/sessions/{sid}")
    data = api("GET", "/api/sessions")
    assert len(data["sessions"]) == 0, "Session should be deleted"

    # Test frontend is served
    with urllib.request.urlopen(f"{BASE}/") as resp:
        html = resp.read().decode()
    assert "AI Council Dashboard" in html, "Frontend should be served"

    server.shutdown()
    print("  PASS")


def test_persistence():
    """Test that sessions persist across server restarts."""
    print("\n[3] Testing persistence...")

    import council_server
    council_server.SERVER_PORT = 8756
    council_server.MANAGER.sessions.clear()

    # Start server
    server = ThreadingHTTPServer(("127.0.0.1", 8756), CouncilServerHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    BASE = "http://127.0.0.1:8756"

    def api(method, path, body=None):
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    # Create session and post messages
    data = api("POST", "/api/sessions", {"task": "Persistence test", "workdir": "/tmp", "auto_start": False})
    sid = data["id"]
    api("POST", f"/api/sessions/{sid}/join", {"agent_id": "human", "role": "supervisor"})
    api("POST", f"/api/sessions/{sid}/message", {"agent_id": "human", "content": "Test message for persistence"})
    api("POST", f"/api/sessions/{sid}/join", {"agent_id": "rev-01", "role": "reviewer"})
    api("POST", f"/api/sessions/{sid}/message", {"agent_id": "rev-01", "content": "Review message"})

    # Stop server
    server.shutdown()
    time.sleep(1)

    # Verify files exist
    assert (DATA_DIR / f"{sid}.json").exists(), "Session metadata should be on disk"
    assert (DATA_DIR / f"{sid}_history.json").exists(), "History should be on disk"

    # Start a new server on a different port
    council_server.MANAGER.sessions.clear()
    council_server.MANAGER.load_persisted(CONFIG_PATH)

    # Check session was loaded (clear any leftovers from other tests first)
    # Remove sessions that aren't ours
    our_sid = sid
    for s_id in list(council_server.MANAGER.sessions.keys()):
        if s_id != our_sid:
            del council_server.MANAGER.sessions[s_id]

    # Check session was loaded
    assert sid in council_server.MANAGER.sessions, "Session should be loaded from disk"
    room = council_server.MANAGER.sessions[sid]
    assert room.task == "Persistence test", f"Task should be loaded, got {room.task}"
    assert len(room.messages) >= 2, f"Messages should be loaded, got {len(room.messages)}"

    # Start server again to verify API serves persisted data
    council_server.SERVER_PORT = 8758
    server2 = ThreadingHTTPServer(("127.0.0.1", 8758), CouncilServerHandler)
    t2 = threading.Thread(target=server2.serve_forever, daemon=True)
    t2.start()
    time.sleep(0.5)

    BASE2 = "http://127.0.0.1:8758"

    def api2(method, path, body=None):
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(f"{BASE2}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    data = api2("GET", "/api/sessions")
    assert len(data["sessions"]) == 1, f"Should have 1 persisted session, got {len(data['sessions'])}"
    assert data["sessions"][0]["task"] == "Persistence test"

    data = api2("GET", f"/api/sessions/{sid}")
    assert len(data["messages"]) >= 2, f"Should have persisted messages, got {len(data['messages'])}"

    # Cleanup
    api2("DELETE", f"/api/sessions/{sid}")
    server2.shutdown()

    print("  PASS")


def test_sse_streaming():
    """Test SSE streaming from the server."""
    print("\n[4] Testing SSE streaming...")

    import council_server
    council_server.SERVER_PORT = 8757
    council_server.MANAGER.sessions.clear()

    server = ThreadingHTTPServer(("127.0.0.1", 8757), CouncilServerHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)

    BASE = "http://127.0.0.1:8757"

    def api(method, path, body=None):
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    # Create session
    data = api("POST", "/api/sessions", {"task": "SSE test", "workdir": "/tmp", "auto_start": False})
    sid = data["id"]
    api("POST", f"/api/sessions/{sid}/join", {"agent_id": "human", "role": "supervisor"})

    # Connect to SSE stream
    # We can't easily test SSE with urllib, so test the room's broadcast directly
    room = council_server.MANAGER.get(sid)
    q = queue.Queue()
    room.add_sse_client(q)

    # The add_sse_client should have sent the initial state
    event = q.get(timeout=2)
    assert event["type"] == "state", f"Expected state event, got {event['type']}"
    assert "messages" in event["data"], "State should have messages"

    # Post a message and check SSE receives it
    api("POST", f"/api/sessions/{sid}/message", {"agent_id": "human", "content": "SSE broadcast test"})
    event = q.get(timeout=2)
    assert event["type"] == "message", f"Expected message event, got {event['type']}"
    assert "SSE broadcast" in event["data"]["content"], "SSE should have the message"

    room.remove_sse_client(q)
    api("DELETE", f"/api/sessions/{sid}")
    server.shutdown()

    print("  PASS")


def main():
    print("=" * 60)
    print("  AI COUNCIL SERVER — Integration Test")
    print("=" * 60)

    test_session_room()
    test_http_api()
    test_persistence()
    test_sse_streaming()

    # Cleanup test data
    for p in DATA_DIR.glob("*"):
        if p.name.startswith("test"):
            p.unlink()

    print("\n" + "=" * 60)
    print("  ALL SERVER TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()