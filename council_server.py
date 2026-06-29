#!/usr/bin/env python3
"""
Council Server — Unified bus + dashboard + session manager.

The server IS the bus. There is no separate bus process. Each session is a
"room" with its own message history, members, and votes. All members (human
via the web UI, AI agents via HTTP) connect to this same server.

Architecture:
    Browser (human)  ─── SSE + HTTP ───┐
                                       ├── Council Server (this file)
    AI Agent (CLI)    ─── HTTP ────────┘
                                       │
                                       ├── Session "room" 1 (messages, votes, members)
                                       ├── Session "room" 2
                                       └── Session "room" N

Every message flows through the server, which:
  1. Validates the sender is a member
  2. Persists the message to disk
  3. Broadcasts to all SSE listeners (human dashboard + any future WS clients)
  4. Processes votes and consensus

AI agents still use the same HTTP API (join, post, vote, etc.) — they don't
need to know about SSE or the frontend. The server handles both.

Usage:
    python3 council_server.py [--port 8080]

Endpoints (API for agents + dashboard):
    GET  /api/sessions               List all sessions
    POST /api/sessions                Create a new session {task, workdir, consensus, auto_start}
    GET  /api/sessions/<id>           Get session state (members, messages, votes, agents)
    GET  /api/sessions/<id>/stream    SSE stream of real-time events
    POST /api/sessions/<id>/join      Join a session as a member {agent_id, role, model}
    POST /api/sessions/<id>/message   Post a message {agent_id, content, type}
    POST /api/sessions/<id>/vote/propose   Propose a vote {agent_id, proposal, options}
    POST /api/sessions/<id>/vote/respond   Respond to a vote {agent_id, vote_id, response, rationale}
    POST /api/sessions/<id>/stop     Stop all agent processes
    POST /api/sessions/<id>/start    Start agents
    DELETE /api/sessions/<id>        Delete a session
    GET  /api/config                  Get council.yaml config
    GET  /                            Serve the frontend
"""

import json
import os
import sys
import time
import uuid
import threading
import subprocess
import queue
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from council_backends import BackendRegistry, load_config

DATA_DIR = Path(__file__).parent / ".council_data"
DATA_DIR.mkdir(exist_ok=True)


# =============================================================================
# Session Room — the bus state for one session, with persistence + SSE
# =============================================================================

class SessionRoom:
    """A session room: members, messages, votes, persistence, SSE clients."""

    def __init__(self, session_id, task, workdir, config_path, consensus="majority"):
        self.id = session_id
        self.task = task
        self.workdir = workdir
        self.config_path = config_path
        self.consensus = consensus
        self.created_at = time.time()
        self.status = "created"  # created -> running -> stopped -> archived
        self.members = {}       # agent_id -> {role, model, joined_at}
        self.messages = []      # [{id, agent_id, role, content, type, timestamp}]
        self.votes = {}          # vote_id -> {proposal, options, responses, status, ...}
        self.vote_order = []
        self.agents = []        # agent specs for spawning
        self.agent_procs = []   # subprocess.Popen objects
        self.human_id = "human"
        self.lock = threading.RLock()
        self.sse_clients = []   # list of queue.Queue for SSE streaming
        self.consensus_rule = consensus

    # --- Membership ---

    def join(self, agent_id, role, model=""):
        with self.lock:
            self.members[agent_id] = {
                "role": role, "model": model,
                "joined_at": time.time(),
            }
            self._add_system(f"[{role}] joined the council", agent_id, role)

    def leave(self, agent_id):
        with self.lock:
            member = self.members.pop(agent_id, None)
            if member:
                self._add_system(f"[{member['role']}] left the council", agent_id, member["role"])

    # --- Messages ---

    def post_message(self, agent_id, content, msg_type="message"):
        with self.lock:
            member = self.members.get(agent_id)
            if not member:
                return {"error": "not_registered"}, 400
            msg = {
                "id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "role": member["role"],
                "content": content,
                "type": msg_type,
                "timestamp": time.time(),
            }
            self.messages.append(msg)
            self._persist()
            self._broadcast("message", msg)
            return msg, 200

    def get_messages(self, since=0):
        with self.lock:
            if since > 0:
                return [m for m in self.messages if m["timestamp"] > since]
            return list(self.messages)

    # --- Voting ---

    def propose_vote(self, agent_id, proposal, options=None):
        with self.lock:
            member = self.members.get(agent_id)
            if not member:
                return {"error": "not_registered"}, 400
            if options is None:
                options = ["approve", "reject", "request_changes"]
            vote_id = str(uuid.uuid4())[:8]
            vote = {
                "vote_id": vote_id, "proposal": proposal, "options": options,
                "responses": {}, "status": "open",
                "proposed_by": agent_id, "proposed_by_role": member["role"],
                "timestamp": time.time(),
            }
            self.votes[vote_id] = vote
            self.vote_order.append(vote_id)
            self._add_system(
                f"VOTE called by [{member['role']}]: {proposal}\n"
                f"Options: {', '.join(options)}\nVote ID: {vote_id}",
                agent_id, member["role"], "vote_proposal"
            )
            return vote, 200

    def respond_vote(self, agent_id, vote_id, response, rationale=""):
        with self.lock:
            vote = self.votes.get(vote_id)
            if not vote:
                return {"error": "vote_not_found"}, 404
            if vote["status"] != "open":
                return {"error": "vote_closed", "status": vote["status"]}, 400
            if response not in vote["options"]:
                return {"error": f"invalid_response, must be one of {vote['options']}"}, 400
            vote["responses"][agent_id] = {
                "response": response, "rationale": rationale, "timestamp": time.time(),
            }
            member = self.members.get(agent_id, {})
            role = member.get("role", "unknown")
            self._add_system(
                f"[{role}] voted '{response}' on vote {vote_id}"
                + (f": {rationale}" if rationale else ""),
                agent_id, role, "vote_response"
            )
            self._check_vote_completion(vote_id)
            return self._vote_result(vote_id), 200

    def _check_vote_completion(self, vote_id):
        vote = self.votes[vote_id]
        if len(vote["responses"]) >= len(self.members):
            vote["status"] = self._tally_result(vote_id)
            self._add_system(
                f"VOTE {vote_id} CLOSED: {vote['status']}\n"
                f"Tally: {json.dumps(self._tally_vote(vote_id))}",
                "system", "system", "vote_closed"
            )

    def _tally_vote(self, vote_id):
        vote = self.votes[vote_id]
        counts = {}
        for r in vote["responses"].values():
            counts[r["response"]] = counts.get(r["response"], 0) + 1
        return counts

    def _tally_result(self, vote_id):
        vote = self.votes[vote_id]
        counts = self._tally_vote(vote_id)
        total = len(vote["responses"])
        rule = self.consensus_rule
        if rule == "unanimous":
            return "approved_unanimous" if len(counts) == 1 and "approve" in counts else "failed_no_consensus"
        elif rule == "supermajority":
            return "approved_supermajority" if counts.get("approve", 0) > int(total * 0.67) else "failed_no_consensus"
        else:
            return "approved_majority" if counts.get("approve", 0) > total / 2 else "failed_no_consensus"

    def _vote_result(self, vote_id):
        vote = self.votes[vote_id]
        counts = self._tally_vote(vote_id)
        return {
            "vote_id": vote_id, "proposal": vote["proposal"],
            "status": vote["status"], "responses": vote["responses"],
            "tally": counts, "total_responses": len(vote["responses"]),
            "total_members": len(self.members),
            "consensus_rule": self.consensus_rule,
            "options": vote["options"],
        }

    def get_open_votes(self):
        return [self._vote_result(vid) for vid in self.vote_order
                if self.votes[vid]["status"] == "open"]

    # --- State ---

    def get_state(self):
        with self.lock:
            return {
                "id": self.id, "task": self.task, "workdir": self.workdir,
                "consensus": self.consensus, "status": self.status,
                "created_at": self.created_at,
                "members": dict(self.members),
                "messages": list(self.messages),
                "open_votes": self.get_open_votes(),
                "agents": self.agents,
                "message_count": len(self.messages),
            }

    # --- Agent Management ---

    def start_agents(self, agents=None):
        from council_supervisor import analyze_task, compose_team
        config = load_config(self.config_path)
        registry = BackendRegistry.from_config(config)

        if agents:
            self.agents = agents
        else:
            analysis = analyze_task(self.task, self.workdir)
            available = registry.list_available()
            self.agents = compose_team(analysis, config, registry, available)

        # Join human as supervisor
        self.join(self.human_id, "supervisor", "human")

        # Post the task
        self.post_message(self.human_id,
            f"COUNCIL TASK: {self.task}\n\nWorking directory: {self.workdir}\n"
            f"Consensus: {self.consensus}\n\n"
            "The supervisor has assembled this council. Review the task, discuss, "
            "and agree on a plan before implementing.", "task")

        # Spawn agent processes
        agent_script = str(Path(__file__).parent / "council_agent.py")
        for agent in self.agents:
            # Join the agent to the room first (so they can post)
            self.join(agent["id"], agent["role"], f"{agent['backend']}/{agent.get('model', 'default')}")

            cmd = [
                sys.executable, agent_script,
                "--config", self.config_path,
                "--bus", f"http://127.0.0.1:{SERVER_PORT}/api/sessions/{self.id}",
                "--role", agent["role"],
                "--backend", agent["backend"],
                "--agent-id", agent["id"],
                "--workdir", self.workdir,
            ]
            if agent.get("model"):
                cmd += ["--model", agent["model"]]
            if agent.get("read_only"):
                cmd += ["--read-only"]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.agent_procs.append(proc)

        self.status = "running"
        self.save_meta()

    def stop_agents(self):
        for proc in self.agent_procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.agent_procs.clear()
        self.status = "stopped"
        self.save_meta()

    # --- Persistence ---

    def _persist(self):
        """Persist messages to disk (called on every new message)."""
        path = DATA_DIR / f"{self.id}_history.json"
        try:
            with open(path, "w") as f:
                json.dump(self.messages, f, indent=2)
        except Exception:
            pass

    def save_meta(self):
        """Save session metadata."""
        meta = {
            "id": self.id, "task": self.task, "workdir": self.workdir,
            "consensus": self.consensus, "created_at": self.created_at,
            "status": self.status, "agents": self.agents,
        }
        path = DATA_DIR / f"{self.id}.json"
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)

    def load_history(self):
        """Load message history from disk."""
        path = DATA_DIR / f"{self.id}_history.json"
        if path.exists():
            with open(path) as f:
                self.messages = json.load(f)

    # --- SSE ---

    def add_sse_client(self, q):
        with self.lock:
            self.sse_clients.append(q)
            # Send current state immediately
            try:
                q.put_nowait({"type": "state", "data": self.get_state()})
            except:
                pass

    def remove_sse_client(self, q):
        with self.lock:
            if q in self.sse_clients:
                self.sse_clients.remove(q)

    def _broadcast(self, event_type, data):
        """Push event to all SSE clients."""
        for q in self.sse_clients:
            try:
                q.put_nowait({"type": event_type, "data": data, "timestamp": time.time()})
            except:
                pass

    def _add_system(self, content, agent_id, role, msg_type="system"):
        msg = {
            "id": str(uuid.uuid4()), "agent_id": agent_id, "role": role,
            "content": content, "type": msg_type, "timestamp": time.time(),
        }
        self.messages.append(msg)
        self._persist()
        self._broadcast(msg_type if msg_type != "system" else "message", msg)
        return msg

    def cleanup(self):
        self.stop_agents()
        self.save_meta()


# =============================================================================
# Session Manager
# =============================================================================

class SessionManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def create(self, task, workdir, config_path, consensus="majority"):
        sid = str(uuid.uuid4())[:8]
        room = SessionRoom(sid, task, workdir, config_path, consensus)
        with self.lock:
            self.sessions[sid] = room
        room.save_meta()
        return room

    def get(self, sid):
        return self.sessions.get(sid)

    def list_all(self):
        return list(self.sessions.values())

    def delete(self, sid):
        with self.lock:
            room = self.sessions.pop(sid, None)
            if room:
                room.cleanup()
                for suffix in [".json", "_history.json"]:
                    p = DATA_DIR / f"{sid}{suffix}"
                    if p.exists():
                        p.unlink()

    def load_persisted(self, config_path):
        """Load sessions from disk on startup."""
        for path in DATA_DIR.glob("*.json"):
            if path.name.endswith("_history.json"):
                continue
            try:
                with open(path) as f:
                    meta = json.load(f)
                sid = meta["id"]
                if sid not in self.sessions:
                    room = SessionRoom(sid, meta["task"], meta["workdir"],
                                      config_path, meta.get("consensus", "majority"))
                    room.created_at = meta.get("created_at", time.time())
                    room.status = meta.get("status", "archived")
                    room.agents = meta.get("agents", [])
                    room.load_history()
                    self.sessions[sid] = room
            except Exception:
                pass


# =============================================================================
# HTTP Server — unified bus + dashboard API + static file serving
# =============================================================================

MANAGER = SessionManager()
CONFIG_PATH = str(Path(__file__).parent / "council.yaml")
FRONTEND_DIR = Path(__file__).parent / "dashboard"
SERVER_PORT = 8080  # set in main()


class CouncilServerHandler(BaseHTTPRequestHandler):

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    # --- GET ---

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API: list sessions
        if path == "/api/sessions":
            sessions = [s.get_state() if s.status == "running" else
                       {"id": s.id, "task": s.task, "status": s.status,
                        "created_at": s.created_at, "agents": s.agents,
                        "members": {}, "messages": s.messages, "open_votes": [],
                        "message_count": len(s.messages), "workdir": s.workdir,
                        "consensus": s.consensus}
                       for s in MANAGER.list_all()]
            self._send_json({"sessions": sessions})
            return

        # API: config
        if path == "/api/config":
            config = load_config(CONFIG_PATH)
            registry = BackendRegistry.from_config(config)
            self._send_json({
                "backends": {n: {"enabled": b.enabled, "available": b.is_available(),
                                "models": b.list_models()}
                            for n, b in registry.backends.items()},
                "roles": list(config.get("roles", {}).keys()),
                "consensus": config.get("consensus", {}),
            })
            return

        # API: SSE stream
        if path.startswith("/api/sessions/") and path.endswith("/stream"):
            sid = path.split("/")[3]
            room = MANAGER.get(sid)
            if not room:
                self._send_json({"error": "not_found"}, 404)
                return
            self._handle_sse(room)
            return

        # API: session state
        if path.startswith("/api/sessions/"):
            sid = path.split("/")[3]
            room = MANAGER.get(sid)
            if not room:
                self._send_json({"error": "not_found"}, 404)
                return
            self._send_json(room.get_state())
            return

        # Static: frontend
        if path == "/" or path == "/index.html":
            self._serve_file(FRONTEND_DIR / "index.html", "text/html")
            return

        if path.startswith("/static/"):
            ext_map = {".js": "application/javascript", ".css": "text/css",
                      ".svg": "image/svg+xml", ".png": "image/png"}
            ct = ext_map.get(Path(path).suffix, "application/octet-stream")
            self._serve_file(FRONTEND_DIR / path[len("/static/"):], ct)
            return

        self.send_error(404)

    # --- POST ---

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_body()
        except json.JSONDecodeError:
            self._send_json({"error": "invalid_json"}, 400)
            return

        # Create session
        if path == "/api/sessions":
            task = body.get("task", "")
            workdir = body.get("workdir", ".")
            consensus = body.get("consensus", "majority")
            if not task:
                self._send_json({"error": "task required"}, 400)
                return
            room = MANAGER.create(task, workdir, CONFIG_PATH, consensus)
            auto_start = body.get("auto_start", True)
            if auto_start:
                room.start_agents()
            self._send_json(room.get_state())
            return

        # All other session endpoints
        if path.startswith("/api/sessions/"):
            parts = path.split("/")
            sid = parts[3]
            room = MANAGER.get(sid)
            if not room:
                self._send_json({"error": "not_found"}, 404)
                return

            action = parts[4] if len(parts) > 4 else ""

            if action == "join":
                room.join(body.get("agent_id", str(uuid.uuid4())[:8]),
                         body.get("role", "observer"), body.get("model", ""))
                self._send_json({"status": "joined", "room": room.get_state()})
                return

            if action == "message":
                msg, status = room.post_message(body.get("agent_id", ""),
                                                body.get("content", ""),
                                                body.get("type", "message"))
                self._send_json(msg, status)
                return

            if action == "vote" and len(parts) > 5 and parts[5] == "propose":
                vote, status = room.propose_vote(body.get("agent_id", ""),
                                                body.get("proposal", ""),
                                                body.get("options"))
                self._send_json(vote, status)
                return

            if action == "vote" and len(parts) > 5 and parts[5] == "respond":
                result, status = room.respond_vote(body.get("agent_id", ""),
                                                   body.get("vote_id", ""),
                                                   body.get("response", ""),
                                                   body.get("rationale", ""))
                self._send_json(result, status)
                return

            if action == "stop":
                room.stop_agents()
                self._send_json({"status": "stopped"})
                return

            if action == "start":
                room.start_agents(body.get("agents"))
                self._send_json(room.get_state())
                return

        self.send_error(404)

    # --- DELETE ---

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/sessions/"):
            sid = path.split("/")[3]
            MANAGER.delete(sid)
            self._send_json({"status": "deleted"})
            return
        self.send_error(404)

    # --- SSE ---

    def _handle_sse(self, room):
        q = queue.Queue()
        room.add_sse_client(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            while True:
                try:
                    event = q.get(timeout=30)
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {json.dumps(event['data'], default=str)}\n\n".encode()
                    )
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            room.remove_sse_client(q)

    # --- Static files ---

    def _serve_file(self, path, content_type):
        if not path.exists():
            self.send_error(404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[council] {self.address_string()} {fmt % args}\n")


class ThreadingHTTPServer(HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# =============================================================================
# Main
# =============================================================================

def detect_bind_host():
    """Auto-detect the best host to bind to.
    Prefers the WireGuard VPN IP (10.0.0.x) if the VPN is up,
    otherwise falls back to 127.0.0.1 (localhost only).
    Never binds to 0.0.0.0 unless explicitly requested.
    """
    import socket
    # Check if the WireGuard interface is up by looking for a 10.0.0.x address
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for addr in addrs:
            ip = addr[4][0]
            if ip.startswith("10.0.0."):
                return ip  # VPN address — accessible from VPN peers
    except Exception:
        pass

    # Fallback: check common WireGuard IPs
    for test_ip in ["10.0.0.8", "10.0.0.1"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((test_ip, 0))
            s.close()
            return test_ip
        except OSError:
            continue

    # No VPN — localhost only
    return "127.0.0.1"
    global SERVER_PORT
    import argparse
    parser = argparse.ArgumentParser(description="Council Server — unified bus + dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="auto",
                        help="Host to bind to (default: auto — detects VPN IP, falls back to 127.0.0.1)")
    args = parser.parse_args()
    SERVER_PORT = args.port

    if args.host == "auto":
        bind_host = detect_bind_host()
    else:
        bind_host = args.host

    MANAGER.load_persisted(CONFIG_PATH)

    server = ThreadingHTTPServer((bind_host, args.port), CouncilServerHandler)
    print(f"Council Server running on http://{bind_host}:{args.port}")
    if bind_host == "127.0.0.1":
        print("  (localhost only — not accessible from other machines)")
    elif bind_host.startswith("10.0.0."):
        print(f"  (VPN only — accessible from 10.0.0.0/24)")
    print(f"  UI:  http://{bind_host}:{args.port}/")
    print(f"  API: http://{bind_host}:{args.port}/api/sessions")
    print(f"  Data: {DATA_DIR}")
    print(f"  Loaded {len(MANAGER.sessions)} persisted sessions")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        for s in MANAGER.list_all():
            if s.status == "running":
                s.save_meta()
        server.shutdown()


if __name__ == "__main__":
    main()