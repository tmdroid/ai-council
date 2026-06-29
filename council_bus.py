#!/usr/bin/env python3
"""
Council Bus — Shared message bus + voting + client utilities.

Provides:
  - HTTP message bus server (agents post/read messages, call votes)
  - BusClient for programmatic access
  - build_prompt() — builds the agent prompt from conversation + role config
  - parse_response() — extracts VOTE/PROPOSE_VOTE/DONE actions from agent output
"""

import json
import time
import uuid
import threading
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error


# =============================================================================
# Bus Server (same as v1, kept here for self-containment)
# =============================================================================

class CouncilBus:
    def __init__(self):
        self._lock = threading.RLock()
        self.members = {}
        self.messages = []
        self.votes = {}
        self.vote_order = []
        self.consensus_rule = "majority"

    def join(self, agent_id, role, model=""):
        with self._lock:
            self.members[agent_id] = {
                "role": role, "model": model,
                "joined_at": time.time(), "last_seen": time.time(),
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
                "id": str(uuid.uuid4()), "agent_id": agent_id,
                "role": member["role"], "content": content,
                "type": msg_type, "timestamp": time.time(),
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
                "vote_id": vote_id, "proposal": proposal, "options": options,
                "responses": {}, "status": "open",
                "proposed_by": agent_id, "proposed_by_role": member["role"],
                "timestamp": time.time(),
            }
            self.votes[vote_id] = vote
            self.vote_order.append(vote_id)
            self._add_system_message(
                f"VOTE called by [{member['role']}]: {proposal}\n"
                f"Options: {', '.join(options)}\nVote ID: {vote_id}",
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
                "response": response, "rationale": rationale, "timestamp": time.time(),
            }
            member = self.members.get(agent_id, {})
            role = member.get("role", "unknown")
            self._add_system_message(
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
            self._add_system_message(
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

    def _vote_tally(self, vote_id):
        return self._tally_vote(vote_id)

    def _tally_result(self, vote_id):
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
        else:
            if counts.get("approve", 0) > total / 2:
                return "approved_majority"
            return "failed_no_consensus"

    def _vote_result(self, vote_id):
        vote = self.votes[vote_id]
        counts = self._tally_vote(vote_id)
        return {
            "vote_id": vote_id, "proposal": vote["proposal"],
            "status": vote["status"], "responses": vote["responses"],
            "tally": counts, "total_responses": len(vote["responses"]),
            "total_members": len(self.members),
            "consensus_rule": self.consensus_rule,
        }

    def get_room_state(self):
        with self._lock:
            open_votes = [self._vote_result(vid) for vid in self.vote_order
                         if self.votes[vid]["status"] == "open"]
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
            "id": str(uuid.uuid4()), "agent_id": agent_id, "role": role,
            "content": content, "type": msg_type, "timestamp": time.time(),
        }
        self.messages.append(msg)
        return msg


BUS = CouncilBus()


class BusHandler(BaseHTTPRequestHandler):
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

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send_json({"status": "ok"})
        elif parsed.path == "/room":
            self._send_json(BUS.get_room_state())
        elif parsed.path == "/messages":
            since = float(qs.get("since", ["0"])[0])
            agent = qs.get("agent", [None])[0]
            msgs = BUS.get_messages(since=since, agent_id=agent)
            self._send_json({"messages": msgs, "count": len(msgs)})
        elif parsed.path.startswith("/vote/status/"):
            vote_id = parsed.path.split("/")[-1]
            if vote_id in BUS.votes:
                self._send_json(BUS._vote_result(vote_id))
            else:
                self._send_json({"error": "not_found"}, 404)
        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_body()
        except json.JSONDecodeError:
            self._send_json({"error": "invalid_json"}, 400)
            return
        if parsed.path == "/join":
            agent_id = body.get("agent_id", str(uuid.uuid4())[:8])
            BUS.join(agent_id, body.get("role", "observer"), body.get("model", ""))
            self._send_json({"agent_id": agent_id, "status": "joined", "room": BUS.get_room_state()})
        elif parsed.path == "/leave":
            BUS.leave(body.get("agent_id", ""))
            self._send_json({"status": "left"})
        elif parsed.path == "/message":
            msg, status = BUS.post_message(body.get("agent_id", ""), body.get("content", ""), body.get("type", "message"))
            self._send_json(msg, status)
        elif parsed.path == "/vote/propose":
            vote, status = BUS.propose_vote(body.get("agent_id", ""), body.get("proposal", ""), body.get("options"))
            self._send_json(vote, status)
        elif parsed.path == "/vote/respond":
            result, status = BUS.respond_vote(body.get("agent_id", ""), body.get("vote_id", ""), body.get("response", ""), body.get("rationale", ""))
            self._send_json(result, status)
        elif parsed.path == "/reset":
            BUS.reset()
            self._send_json({"status": "reset"})
        elif parsed.path == "/consensus":
            if BUS.set_consensus_rule(body.get("rule", "majority")):
                self._send_json({"status": "set"})
            else:
                self._send_json({"error": "invalid_rule"}, 400)
        else:
            self._send_json({"error": "not_found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[bus] {self.address_string()} {fmt % args}\n")


class ThreadingHTTPServer(HTTPServer):
    daemon_threads = True


# =============================================================================
# Bus Client (used by agents and the supervisor)
# =============================================================================

class BusClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _request(self, method, path, body=None, timeout=30):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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


# =============================================================================
# Prompt Builder (config-driven, role description passed as parameter)
# =============================================================================

def build_prompt(role, role_description, conversation, open_votes, agent_id,
                 round_num, workdir, extra_context="", can_vote=True,
                 can_propose_vote=True, anonymous_review=False):
    """Build the prompt to send to the agent CLI."""

    # Format conversation history
    conv_lines = []
    for msg in conversation:
        if anonymous_review and msg["type"] in ("message",) and msg.get("role") not in ("system",):
            # Hide agent identity during anonymous review
            prefix = "[anonymous]"
        else:
            prefix = f"[{msg['role']}]"
        if msg["type"] == "system":
            conv_lines.append(f"  *{prefix} {msg['content']}*")
        elif msg["type"] == "vote_proposal":
            conv_lines.append(f"  {prefix} PROPOSED VOTE: {msg['content']}")
        elif msg["type"] == "vote_response":
            conv_lines.append(f"  {prefix} {msg['content']}")
        elif msg["type"] == "vote_closed":
            conv_lines.append(f"  {prefix} {msg['content']}")
        else:
            conv_lines.append(f"  {prefix} {msg['content']}")

    conversation_text = "\n".join(conv_lines[-80:]) if conv_lines else "  (no messages yet)"

    # Format open votes
    votes_text = ""
    if open_votes:
        vote_lines = []
        for v in open_votes:
            vote_lines.append(
                f"  Vote {v['vote_id']}: {v['proposal']}\n"
                f"    Options: {', '.join(v.get('options', ['approve', 'reject', 'request_changes']))}\n"
                f"    Responses: {v['total_responses']}/{v['total_members']}\n"
                f"    Tally: {v.get('tally', {})}"
            )
        votes_text = "\n".join(vote_lines)

    prompt = f"""You are the {role.upper()} of the AI Council.

{role_description}

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
"""
    if can_propose_vote:
        prompt += "- Call a vote: PROPOSE_VOTE: <proposal> | options: <opt1, opt2, ...>\n"
    if can_vote:
        prompt += "- Respond to an open vote: VOTE: <vote_id> <option> -- <rationale>\n"
    prompt += "- Ask another member a question: ASK @<role>: <question>\n"
    prompt += "- Signal completion: DONE: <summary>\n\n"
    prompt += "Keep your response focused and actionable.\n"
    return prompt


# =============================================================================
# Response Parser
# =============================================================================

def parse_response(response_text):
    """Parse the agent's response for structured actions."""
    actions = {"message": None, "votes": [], "propose_vote": None, "done": False}
    lines = response_text.strip().split("\n")
    message_lines = []
    in_message = True

    for line in lines:
        stripped = line.strip()
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
        elif stripped.upper().startswith("DONE:"):
            actions["done"] = True
            if in_message:
                message_lines.append(stripped)
        else:
            if in_message:
                message_lines.append(line)

    actions["message"] = "\n".join(message_lines).strip() if message_lines else response_text.strip()
    return actions


# =============================================================================
# Bus Server Entry Point
# =============================================================================

def start_bus_server(port=8747, host="127.0.0.1", consensus="majority"):
    """Start the bus server (blocking)."""
    BUS.consensus_rule = consensus
    server = ThreadingHTTPServer((host, port), BusHandler)
    print(f"Council Bus running on http://{host}:{port}")
    print(f"Consensus rule: {consensus}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8747)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--consensus", default="majority",
                       choices=["majority", "supermajority", "unanimous"])
    args = parser.parse_args()
    start_bus_server(args.port, args.host, args.consensus)