#!/usr/bin/env python3
"""
Council Server — Unified bus + dashboard with Socket.IO.

The server IS the bus. Each session is a "room" (Socket.IO room). All
communication — human dashboard and AI agents — goes through a single
WebSocket connection per client. No SSE, no blocking, no connection limit
issues.

AI agents still use the REST API (HTTP POST/GET) since they're simple
curl/urllib clients. The server supports both:
  - Socket.IO for the browser (real-time, bidirectional, single connection)
  - REST API for AI agents (HTTP POST/GET, stateless)

Usage:
    .venv/bin/python council_server.py [--port 8080]
"""

import json
import os
import sys
import time
import uuid
import threading
import subprocess
from pathlib import Path

# Flask + Socket.IO
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from council_backends import BackendRegistry, load_config

DATA_DIR = Path(__file__).parent / ".council_data"
DATA_DIR.mkdir(exist_ok=True)

# =============================================================================
# Session Room
# =============================================================================

class SessionRoom:
    """A session room: members, messages, votes, persistence."""

    def __init__(self, session_id, task, workdir, config_path, consensus="majority"):
        self.id = session_id
        self.task = task
        self.workdir = workdir
        self.config_path = config_path
        self.consensus = consensus
        self.created_at = time.time()
        self.status = "created"
        self.members = {}
        self.messages = []
        self.votes = {}
        self.vote_order = []
        self.agents = []
        self.agent_procs = []
        self.human_id = "human"
        self.lock = threading.RLock()
        self.consensus_rule = consensus

    def join(self, agent_id, role, model=""):
        with self.lock:
            if agent_id in self.members:
                # Already a member — update info but don't re-announce
                self.members[agent_id] = {"role": role, "model": model, "joined_at": time.time()}
                return
            self.members[agent_id] = {"role": role, "model": model, "joined_at": time.time()}
            self._add_system(f"[{role}] joined the council", agent_id, role)

    def leave(self, agent_id):
        with self.lock:
            member = self.members.pop(agent_id, None)
            if member:
                self._add_system(f"[{member['role']}] left the council", agent_id, member["role"])

    def post_message(self, agent_id, content, msg_type="message"):
        with self.lock:
            member = self.members.get(agent_id)
            if not member:
                return {"error": "not_registered"}, 400
            msg = {
                "id": str(uuid.uuid4()), "agent_id": agent_id, "role": member["role"],
                "content": content, "type": msg_type, "timestamp": time.time(),
            }
            self.messages.append(msg)
            self._persist()
            self._broadcast_event("message", msg)
            return msg, 200

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
                agent_id, member["role", "vote_proposal"])
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
            vote["responses"][agent_id] = {"response": response, "rationale": rationale, "timestamp": time.time()}
            member = self.members.get(agent_id, {})
            role = member.get("role", "unknown")
            self._add_system(
                f"[{role}] voted '{response}' on vote {vote_id}" + (f": {rationale}" if rationale else ""),
                agent_id, role, "vote_response")
            self._check_vote_completion(vote_id)
            return self._vote_result(vote_id), 200

    def _check_vote_completion(self, vote_id):
        vote = self.votes[vote_id]
        if len(vote["responses"]) >= len(self.members):
            vote["status"] = self._tally_result(vote_id)
            self._add_system(
                f"VOTE {vote_id} CLOSED: {vote['status']}\nTally: {json.dumps(self._tally_vote(vote_id))}",
                "system", "system", "vote_closed")

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
            "vote_id": vote_id, "proposal": vote["proposal"], "status": vote["status"],
            "responses": vote["responses"], "tally": counts,
            "total_responses": len(vote["responses"]), "total_members": len(self.members),
            "consensus_rule": self.consensus_rule, "options": vote["options"],
        }

    def get_open_votes(self):
        return [self._vote_result(vid) for vid in self.vote_order if self.votes[vid]["status"] == "open"]

    def get_state(self):
        with self.lock:
            return {
                "id": self.id, "task": self.task, "workdir": self.workdir,
                "consensus": self.consensus, "status": self.status,
                "created_at": self.created_at, "members": dict(self.members),
                "messages": list(self.messages), "open_votes": self.get_open_votes(),
                "agents": self.agents, "message_count": len(self.messages),
            }

    def start_agents(self, agents=None):
        """Start agent processes. Returns immediately — all work in background thread."""
        def _start():
            try:
                from council_supervisor import analyze_task, compose_team
                config = load_config(self.config_path)
                registry = BackendRegistry.from_config(config)
                if agents:
                    self.agents = agents
                else:
                    analysis = analyze_task(self.task, self.workdir)
                    available = registry.list_available()
                    self.agents = compose_team(analysis, config, registry, available)

                if self.human_id not in self.members:
                    self.join(self.human_id, "supervisor", "human")
                if not any(m.get("type") == "task" for m in self.messages):
                    self.post_message(self.human_id,
                        f"COUNCIL TASK: {self.task}\n\nWorking directory: {self.workdir}\n"
                        f"Consensus: {self.consensus}\n\n"
                        "The supervisor has assembled this council. Review the task, discuss, "
                        "and agree on a plan before implementing.", "task")

                agent_script = str(Path(__file__).parent / "council_agent.py")
                for agent in self.agents:
                    if agent["id"] not in self.members:
                        self.join(agent["id"], agent["role"], f"{agent['backend']}/{agent.get('model', 'default')}")
                    # Use the venv python explicitly (symlink resolution breaks venv detection)
                    venv_python = str(Path(__file__).parent / ".venv" / "bin" / "python3")
                    if not Path(venv_python).exists():
                        venv_python = sys.executable  # fallback to whatever is running

                    cmd = [venv_python, agent_script, "--config", self.config_path,
                           "--bus", f"http://{SERVER_HOST}:{SERVER_PORT}", "--session", self.id,
                           "--role", agent["role"], "--backend", agent["backend"],
                           "--agent-id", agent["id"], "--workdir", self.workdir]
                    if agent.get("model"): cmd += ["--model", agent["model"]]
                    if agent.get("read_only"): cmd += ["--read-only"]

                    # Set environment so the agent process can find venv packages
                    # The venv uses symlinks, so Python can't auto-detect the venv.
                    # We explicitly set PYTHONPATH to the venv site-packages.
                    agent_env = os.environ.copy()
                    venv_site = str(Path(__file__).parent / ".venv" / "lib" / "python3.14" / "site-packages")
                    if Path(venv_site).exists():
                        agent_env["PYTHONPATH"] = venv_site + ":" + agent_env.get("PYTHONPATH", "")
                        agent_env["VIRTUAL_ENV"] = str(Path(__file__).parent / ".venv")

                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=agent_env)
                    self.agent_procs.append(proc)
                    print(f"[council] Spawned {agent['id']}: {' '.join(cmd[:4])}... PYTHONPATH={agent_env.get('PYTHONPATH','')[:60]}", file=sys.stderr)
                    # Log agent stderr to a file for debugging
                    stderr_thread = threading.Thread(
                        target=self._log_agent_stderr, args=(proc, agent["id"]), daemon=True)
                    stderr_thread.start()
                self.status = "running"
                self.save_meta()
                self._broadcast_event("state", self.get_state())
            except Exception as e:
                print(f"[council] Error starting agents: {e}", file=sys.stderr)
                self.status = "error"
                self.save_meta()
                self._broadcast_event("state", self.get_state())

        self.status = "starting"
        self.save_meta()
        threading.Thread(target=_start, daemon=True).start()

    def _log_agent_stderr(self, proc, agent_id):
        """Log agent stderr to a file for debugging."""
        log_path = DATA_DIR / f"{self.id}_{agent_id}_stderr.log"
        try:
            with open(log_path, "w") as f:
                while True:
                    line = proc.stderr.readline()
                    if not line and proc.poll() is not None:
                        break
                    if line:
                        f.write(line)
                        f.flush()
        except Exception:
            pass

    def stop_agents(self):
        for proc in self.agent_procs:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill()
        self.agent_procs.clear()
        self.status = "stopped"
        self.save_meta()
        self._broadcast_event("state", self.get_state())

    def _persist(self):
        path = DATA_DIR / f"{self.id}_history.json"
        try:
            with open(path, "w") as f: json.dump(self.messages, f, indent=2)
        except Exception: pass

    def save_meta(self):
        meta = {"id": self.id, "task": self.task, "workdir": self.workdir,
                "consensus": self.consensus, "created_at": self.created_at,
                "status": self.status, "agents": self.agents}
        with open(DATA_DIR / f"{self.id}.json", "w") as f: json.dump(meta, f, indent=2)

    def load_history(self):
        path = DATA_DIR / f"{self.id}_history.json"
        if path.exists():
            with open(path) as f: self.messages = json.load(f)

    def _broadcast_event(self, event_type, data):
        """Broadcast via Socket.IO to all clients in this session's room."""
        socketio.emit(event_type, data, room=self.id)

    def _add_system(self, content, agent_id, role, msg_type="system"):
        msg = {"id": str(uuid.uuid4()), "agent_id": agent_id, "role": role,
               "content": content, "type": msg_type, "timestamp": time.time()}
        self.messages.append(msg)
        self._persist()
        self._broadcast_event(msg_type if msg_type != "system" else "message", msg)
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
        with self.lock: self.sessions[sid] = room
        room.save_meta()
        return room

    def get(self, sid): return self.sessions.get(sid)
    def list_all(self): return list(self.sessions.values())

    def delete(self, sid):
        with self.lock:
            room = self.sessions.pop(sid, None)
            if room:
                room.cleanup()
                for suffix in [".json", "_history.json"]:
                    p = DATA_DIR / f"{sid}{suffix}"
                    if p.exists(): p.unlink()

    def load_persisted(self, config_path):
        for path in DATA_DIR.glob("*.json"):
            if path.name.endswith("_history.json"): continue
            try:
                with open(path) as f: meta = json.load(f)
                sid = meta["id"]
                if sid not in self.sessions:
                    room = SessionRoom(sid, meta["task"], meta["workdir"], config_path, meta.get("consensus", "majority"))
                    room.created_at = meta.get("created_at", time.time())
                    room.status = meta.get("status", "archived")
                    room.agents = meta.get("agents", [])
                    room.load_history()
                    self.sessions[sid] = room
            except Exception: pass


# =============================================================================
# Flask + Socket.IO Server
# =============================================================================

MANAGER = SessionManager()
CONFIG_PATH = str(Path(__file__).parent / "council.yaml")
FRONTEND_DIR = Path(__file__).parent / "dashboard"
SERVER_PORT = 8080
SERVER_HOST = "127.0.0.1"

app = Flask(__name__, static_folder=str(FRONTEND_DIR))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# --- REST API (for AI agents + frontend data loading) ---

@app.route("/api/sessions")
def api_list_sessions():
    sessions = [s.get_state() if s.status == "running" else
                {"id": s.id, "task": s.task, "status": s.status, "created_at": s.created_at,
                 "agents": s.agents, "members": {}, "messages": s.messages, "open_votes": [],
                 "message_count": len(s.messages), "workdir": s.workdir, "consensus": s.consensus}
                for s in MANAGER.list_all()]
    return jsonify({"sessions": sessions})


@app.route("/api/sessions/<sid>")
def api_get_session(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    return jsonify(room.get_state())


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    body = request.get_json()
    task = body.get("task", "")
    workdir = body.get("workdir", ".")
    consensus = body.get("consensus", "majority")
    if not task: return jsonify({"error": "task required"}), 400
    room = MANAGER.create(task, workdir, CONFIG_PATH, consensus)
    room.join(room.human_id, "supervisor", "human")
    room.post_message(room.human_id,
        f"COUNCIL TASK: {task}\n\nWorking directory: {workdir}\nConsensus: {consensus}\n\n"
        "Session created. The supervisor will assess the task and compose a team. "
        "Click 'Start Agents' when ready.", "task")
    if body.get("auto_start", False):
        room.start_agents()
    return jsonify(room.get_state())


@app.route("/api/sessions/<sid>/join", methods=["POST"])
def api_join(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    body = request.get_json()
    room.join(body.get("agent_id", str(uuid.uuid4())[:8]), body.get("role", "observer"), body.get("model", ""))
    return jsonify({"status": "joined", "room": room.get_state()})


@app.route("/api/sessions/<sid>/leave", methods=["POST"])
def api_leave(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    body = request.get_json()
    room.leave(body.get("agent_id", ""))
    return jsonify({"status": "left"})


@app.route("/api/sessions/<sid>/message", methods=["POST"])
def api_message(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    body = request.get_json()
    msg, status = room.post_message(body.get("agent_id", ""), body.get("content", ""), body.get("type", "message"))
    return jsonify(msg), status


@app.route("/api/sessions/<sid>/vote/propose", methods=["POST"])
def api_vote_propose(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    body = request.get_json()
    vote, status = room.propose_vote(body.get("agent_id", ""), body.get("proposal", ""), body.get("options"))
    return jsonify(vote), status


@app.route("/api/sessions/<sid>/vote/respond", methods=["POST"])
def api_vote_respond(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    body = request.get_json()
    result, status = room.respond_vote(body.get("agent_id", ""), body.get("vote_id", ""), body.get("response", ""), body.get("rationale", ""))
    return jsonify(result), status


@app.route("/api/sessions/<sid>/stop", methods=["POST"])
def api_stop(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    room.stop_agents()
    return jsonify({"status": "stopped"})


@app.route("/api/sessions/<sid>/start", methods=["POST"])
def api_start(sid):
    room = MANAGER.get(sid)
    if not room: return jsonify({"error": "not_found"}), 404
    body = request.get_json() or {}
    room.start_agents(body.get("agents"))
    return jsonify(room.get_state())


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete(sid):
    MANAGER.delete(sid)
    return jsonify({"status": "deleted"})


@app.route("/api/config")
def api_config():
    config = load_config(CONFIG_PATH)
    registry = BackendRegistry.from_config(config)
    return jsonify({
        "backends": {n: {"enabled": b.enabled, "available": b.is_available(), "models": b.list_models()}
                     for n, b in registry.backends.items()},
        "roles": list(config.get("roles", {}).keys()),
        "consensus": config.get("consensus", {}),
    })


@app.route("/")
def index():
    return send_from_directory(str(FRONTEND_DIR), "index.html")


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory(str(FRONTEND_DIR), path)


# --- Socket.IO events (for browser real-time) ---

@socketio.on("connect")
def on_connect():
    print(f"[socketio] client connected: {request.sid}")


@socketio.on("disconnect")
def on_disconnect():
    print(f"[socketio] client disconnected: {request.sid}")


@socketio.on("join_session")
def on_join_session(data):
    """Browser joins a session room to receive real-time events."""
    sid = data.get("session_id")
    room = MANAGER.get(sid)
    if not room:
        emit("error", {"error": "session_not_found"})
        return
    join_room(sid)
    # Send current state immediately
    emit("state", room.get_state())
    print(f"[socketio] {request.sid} joined session {sid}")


@socketio.on("leave_session")
def on_leave_session(data):
    sid = data.get("session_id")
    if sid:
        leave_room(sid)
        print(f"[socketio] {request.sid} left session {sid}")


@socketio.on("post_message")
def on_post_message(data):
    """Browser posts a message via Socket.IO."""
    sid = data.get("session_id")
    room = MANAGER.get(sid)
    if not room:
        emit("error", {"error": "session_not_found"})
        return
    content = data.get("content", "")
    # post_message() already broadcasts via _broadcast_event, no need to emit again
    msg, status = room.post_message(room.human_id, content, data.get("type", "message"))
    if status != 200:
        emit("error", msg)


@socketio.on("cast_vote")
def on_cast_vote(data):
    """Browser casts a vote via Socket.IO."""
    sid = data.get("session_id")
    room = MANAGER.get(sid)
    if not room:
        emit("error", {"error": "session_not_found"})
        return
    result, status = room.respond_vote(
        room.human_id, data.get("vote_id", ""), data.get("response", ""), data.get("rationale", ""))
    if status == 200:
        socketio.emit("vote", result, room=sid)
    else:
        emit("error", result)


@socketio.on("start_agents")
def on_start_agents(data):
    """Browser starts agents via Socket.IO."""
    sid = data.get("session_id")
    room = MANAGER.get(sid)
    if not room:
        emit("error", {"error": "session_not_found"})
        return
    room.start_agents(data.get("agents"))
    emit("state", room.get_state())


@socketio.on("stop_agents")
def on_stop_agents(data):
    """Browser stops agents via Socket.IO."""
    sid = data.get("session_id")
    room = MANAGER.get(sid)
    if not room:
        emit("error", {"error": "session_not_found"})
        return
    room.stop_agents()
    emit("state", room.get_state())


# =============================================================================
# Host detection + main
# =============================================================================

def detect_bind_host():
    import socket
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for addr in addrs:
            ip = addr[4][0]
            if ip.startswith("10."): return ip
    except Exception: pass
    for test_target in ["10.0.0.1", "10.255.255.255", "172.16.0.1", "192.168.1.1"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((test_target, 1))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith("10.") or ip.startswith("172.16.") or ip.startswith("192.168."): return ip
        except Exception: pass
    try:
        with open("/proc/net/fib_trie") as f:
            for line in f:
                line = line.strip()
                if line.startswith("10.") and "." in line:
                    ip = line.split()[0]
                    if ip.count(".") == 3 and ip != "10.0.0.0": return ip
    except Exception: pass
    return "127.0.0.1"


def main():
    global SERVER_PORT, SERVER_HOST
    import argparse
    parser = argparse.ArgumentParser(description="Council Server — Socket.IO")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="auto")
    args = parser.parse_args()
    SERVER_PORT = args.port

    if args.host == "auto":
        bind_host = detect_bind_host()
    else:
        bind_host = args.host
    SERVER_HOST = bind_host

    MANAGER.load_persisted(CONFIG_PATH)

    print(f"Council Server running on http://{bind_host}:{args.port}")
    if bind_host == "127.0.0.1":
        print("  (localhost only)")
    elif bind_host.startswith("10."):
        print(f"  (VPN only — accessible from 10.0.0.0/24)")
    print(f"  UI:  http://{bind_host}:{args.port}/")
    print(f"  API: http://{bind_host}:{args.port}/api/sessions")
    print(f"  Socket.IO: ws://{bind_host}:{args.port}")
    print(f"  Data: {DATA_DIR}")
    print(f"  Loaded {len(MANAGER.sessions)} persisted sessions")

    socketio.run(app, host=bind_host, port=args.port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()