#!/usr/bin/env python3
"""
Council Bus — Shared message bus for multi-agent collaboration.

A simple HTTP server that acts as a chat room / message bus where AI agents
(coordinator, reviewer, coder) can:
  - Register with a role
  - Post messages visible to all
  - Read the full conversation history
  - Call votes on proposals and respond to votes
  - See who's in the room and their roles

The bus is stateless from the agents' perspective — they interact via HTTP
endpoints using curl. This makes it trivially compatible with any agent CLI
(Claude Code, Codex, OpenCode, Hermes subagents) since they all can run curl.

Usage:
    python3 council_bus.py [--port 8747] [--host 127.0.0.1]

Endpoints:
    GET  /health                          Health check
    GET  /room                             Get room state (members, messages, active votes)
    POST /join                             Join the room {agent_id, role, model}
    POST /leave                            Leave the room {agent_id}
    POST /message                          Post a message {agent_id, content, type}
    POST /vote/propose                      Propose a vote {agent_id, proposal, options}
    POST /vote/respond                      Respond to a vote {agent_id, vote_id, response, rationale}
    GET  /vote/status/<vote_id>             Get vote status
    GET  /messages?since=<timestamp>        Get messages since timestamp
    GET  /messages?agent=<agent_id>         Get messages from a specific agent
    POST /reset                             Clear everything (coordinator only)
"""

import json
import time
import uuid
import threading
import argparse
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class CouncilBus:
    """Thread-safe shared state for the council."""

    def __init__(self):
        self._lock = threading.RLock()
        self.members = {}       # agent_id -> {role, model, joined_at, last_seen}
        self.messages = []      # [{id, agent_id, role, content, type, timestamp}]
        self.votes = {}          # vote_id -> {proposal, options, responses, status, proposed_by, timestamp}
        self.vote_order = []     # ordered list of vote_ids
        self.consensus_rule = "majority"  # "majority", "supermajority", "unanimous"

    def join(self, agent_id, role, model=""):
        with self._lock:
            self.members[agent_id] = {
                "role": role,
                "model": model,
                "joined_at": time.time(),
                "last_seen": time.time(),
            }
            self._add_system_message(f"[{role}] joined the council", agent_id, role)

    def leave(self, agent_id):
        with self._lock:
            member = self.members.pop(agent_id, None)
            if member:
                self._add_system_message(f"[{member['role']}] left the council", agent_id, member["role"])

    def post_message(self, agent_id, content, msg_type="message"):
        with self._lock:
            member = self.members.get(agent_id)
            if not member:
                return {"error": "not_registered"}, 400
            member["last_seen"] = time.time()
            msg = {
                "id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "role": member["role"],
                "content": content,
                "type": msg_type,
                "timestamp": time.time(),
            }
            self.messages.append(msg)
            return msg, 200

    def get_messages(self, since=0, agent_id=None):
        with self._lock:
            msgs = self.messages
            if since > 0:
                msgs = [m for m in msgs if m["timestamp"] > since]
            if agent_id:
                msgs = [m for m in msgs if m["agent_id"] == agent_id]
            return list(msgs)

    def propose_vote(self, agent_id, proposal, options=None):
        with self._lock:
            member = self.members.get(agent_id)
            if not member:
                return {"error": "not_registered"}, 400
            if options is None:
                options = ["approve", "reject", "request_changes"]
            vote_id = str(uuid.uuid4())[:8]
            vote = {
                "vote_id": vote_id,
                "proposal": proposal,
                "options": options,
                "responses": {},
                "status": "open",
                "proposed_by": agent_id,
                "proposed_by_role": member["role"],
                "timestamp": time.time(),
            }
            self.votes[vote_id] = vote
            self.vote_order.append(vote_id)
            self._add_system_message(
                f"VOTE called by [{member['role']}]: {proposal}\n"
                f"Options: {', '.join(options)}\n"
                f"Vote ID: {vote_id}",
                agent_id, member["role"], "vote_proposal"
            )
            return vote, 200

    def respond_vote(self, agent_id, vote_id, response, rationale=""):
        with self._lock:
            vote = self.votes.get(vote_id)
            if not vote:
                return {"error": "vote_not_found"}, 404
            if vote["status"] != "open":
                return {"error": "vote_closed", "status": vote["status"]}, 400
            if response not in vote["options"]:
                return {"error": f"invalid_response, must be one of {vote['options']}"}, 400
            vote["responses"][agent_id] = {
                "response": response,
                "rationale": rationale,
                "timestamp": time.time(),
            }
            member = self.members.get(agent_id, {})
            role = member.get("role", "unknown")
            self._add_system_message(
                f"[{role}] voted '{response}' on vote {vote_id}"
                + (f": {rationale}" if rationale else ""),
                agent_id, role, "vote_response"
            )
            # Check if all active members have voted
            self._check_vote_completion(vote_id)
            return self._vote_result(vote_id), 200

    def _check_vote_completion(self, vote_id):
        vote = self.votes[vote_id]
        # All members must vote (coordinator included) for the vote to close
        if len(vote["responses"]) >= len(self.members):
            vote["status"] = self._tally_result(vote_id)
            self._add_system_message(
                f"VOTE {vote_id} CLOSED: {vote['status']}\n"
                f"Tally: {json.dumps(self._vote_tally(vote_id))}",
                "system", "system", "vote_closed"
            )

    def _tally_vote(self, vote_id):
        vote = self.votes[vote_id]
        counts = {}
        for r in vote["responses"].values():
            counts[r["response"]] = counts.get(r["response"], 0) + 1
        return counts

    def _vote_tally(self, vote_id):
        """Alias for _tally_vote, used in system messages."""
        return self._tally_vote(vote_id)

    def _tally_result(self, vote_id):
        """Determine the final status of a vote based on consensus rule."""
        vote = self.votes[vote_id]
        counts = self._tally_vote(vote_id)
        total = len(vote["responses"])
        rule = self.consensus_rule

        if rule == "unanimous":
            if len(counts) == 1 and "approve" in counts:
                return "approved_unanimous"
            return "failed_no_consensus"

        elif rule == "supermajority":
            threshold = int(total * 0.67)
            if counts.get("approve", 0) > threshold:
                return "approved_supermajority"
            return "failed_no_consensus"

        else:  # majority
            if counts.get("approve", 0) > total / 2:
                return "approved_majority"
            return "failed_no_consensus"

    def _vote_result(self, vote_id):
        vote = self.votes[vote_id]
        responses = list(vote["responses"].values())
        counts = {}
        for r in responses:
            counts[r["response"]] = counts.get(r["response"], 0) + 1
        return {
            "vote_id": vote_id,
            "proposal": vote["proposal"],
            "status": vote["status"],
            "responses": vote["responses"],
            "tally": counts,
            "total_responses": len(responses),
            "total_members": len(self.members),
            "consensus_rule": self.consensus_rule,
        }

    def get_room_state(self):
        with self._lock:
            open_votes = [
                self._vote_result(vid) for vid in self.vote_order
                if self.votes[vid]["status"] == "open"
            ]
            return {
                "members": dict(self.members),
                "message_count": len(self.messages),
                "open_votes": open_votes,
                "consensus_rule": self.consensus_rule,
            }

    def set_consensus_rule(self, rule):
        with self._lock:
            if rule in ("majority", "supermajority", "unanimous"):
                self.consensus_rule = rule
                self._add_system_message(f"Consensus rule changed to: {rule}", "system", "system")
                return True
            return False

    def reset(self):
        with self._lock:
            self.members.clear()
            self.messages.clear()
            self.votes.clear()
            self.vote_order.clear()

    def _add_system_message(self, content, agent_id, role, msg_type="system"):
        msg = {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "role": role,
            "content": content,
            "type": msg_type,
            "timestamp": time.time(),
        }
        self.messages.append(msg)
        return msg


# Singleton bus
BUS = CouncilBus()


class BusHandler(BaseHTTPRequestHandler):
    """HTTP handler that exposes the CouncilBus via simple JSON endpoints."""

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
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"status": "ok", "uptime": time.time()})

        elif path == "/room":
            self._send_json(BUS.get_room_state())

        elif path == "/messages":
            since = float(qs.get("since", ["0"])[0])
            agent = qs.get("agent", [None])[0]
            msgs = BUS.get_messages(since=since, agent_id=agent)
            self._send_json({"messages": msgs, "count": len(msgs)})

        elif path.startswith("/vote/status/"):
            vote_id = path.split("/")[-1]
            if vote_id in BUS.votes:
                self._send_json(BUS._vote_result(vote_id))
            else:
                self._send_json({"error": "not_found"}, 404)

        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_body()
        except json.JSONDecodeError:
            self._send_json({"error": "invalid_json"}, 400)
            return

        if path == "/join":
            agent_id = body.get("agent_id", str(uuid.uuid4())[:8])
            role = body.get("role", "observer")
            model = body.get("model", "")
            BUS.join(agent_id, role, model)
            self._send_json({"agent_id": agent_id, "status": "joined", "room": BUS.get_room_state()})

        elif path == "/leave":
            agent_id = body.get("agent_id", "")
            BUS.leave(agent_id)
            self._send_json({"status": "left"})

        elif path == "/message":
            agent_id = body.get("agent_id", "")
            content = body.get("content", "")
            msg_type = body.get("type", "message")
            msg, status = BUS.post_message(agent_id, content, msg_type)
            self._send_json(msg, status)

        elif path == "/vote/propose":
            agent_id = body.get("agent_id", "")
            proposal = body.get("proposal", "")
            options = body.get("options", None)
            vote, status = BUS.propose_vote(agent_id, proposal, options)
            self._send_json(vote, status)

        elif path == "/vote/respond":
            agent_id = body.get("agent_id", "")
            vote_id = body.get("vote_id", "")
            response = body.get("response", "")
            rationale = body.get("rationale", "")
            result, status = BUS.respond_vote(agent_id, vote_id, response, rationale)
            self._send_json(result, status)

        elif path == "/reset":
            BUS.reset()
            self._send_json({"status": "reset"})

        elif path == "/consensus":
            rule = body.get("rule", "majority")
            if BUS.set_consensus_rule(rule):
                self._send_json({"status": "set", "rule": rule})
            else:
                self._send_json({"error": "invalid_rule"}, 400)

        else:
            self._send_json({"error": "not_found"}, 404)

    def log_message(self, fmt, *args):
        # Minimal logging to stderr
        sys.stderr.write(f"[bus] {self.address_string()} {fmt % args}\n")


class ThreadingHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="Council Bus — multi-agent message bus")
    parser.add_argument("--port", type=int, default=8747, help="Port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--consensus", default="majority",
                       choices=["majority", "supermajority", "unanimous"],
                       help="Consensus rule for votes")
    args = parser.parse_args()

    BUS.consensus_rule = args.consensus

    server = ThreadingHTTPServer((args.host, args.port), BusHandler)
    print(f"Council Bus running on http://{args.host}:{args.port}")
    print(f"Consensus rule: {args.consensus}")
    print(f"Endpoints: /health /room /join /leave /message /vote/propose /vote/respond /messages")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()