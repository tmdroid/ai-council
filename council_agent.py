#!/usr/bin/env python3
"""
Council Agent — Config-driven agent harness.

Connects any CLI agent to the Council Bus. The agent's role, backend,
model, and read/write permissions are all driven by council.yaml.

Each agent is a long-running process that:
  1. Polls the bus for new messages and open votes
  2. Builds a prompt from: role description + conversation + pending votes
  3. Runs the configured backend CLI with the prompt
  4. Parses the response for VOTE/PROPOSE_VOTE/DONE actions
  5. Posts results back to the bus
  6. Loops
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from council_backends import BackendRegistry, load_config
from council_bus import BusClient, build_prompt, parse_response


def main():
    parser = argparse.ArgumentParser(description="Council Agent — config-driven")
    parser.add_argument("--config", default=str(Path(__file__).parent / "council.yaml"),
                       help="Path to council.yaml config")
    parser.add_argument("--bus", default="http://127.0.0.1:8747", help="Server URL")
    parser.add_argument("--session", default=None, help="Session ID (for unified server mode)")
    parser.add_argument("--role", required=True, help="Agent role (from council.yaml roles)")
    parser.add_argument("--backend", default=None, help="CLI backend (overrides config default)")
    parser.add_argument("--model", default=None, help="Model ID for the backend (overrides config default)")
    parser.add_argument("--agent-id", default=None, help="Unique agent ID (auto-generated if not set)")
    parser.add_argument("--workdir", default=None, help="Working directory (overrides config)")
    parser.add_argument("--max-rounds", type=int, default=None, help="Max conversation rounds")
    parser.add_argument("--max-turns", type=int, default=None, help="Max turns for the CLI agent")
    parser.add_argument("--read-only", action="store_true", default=None,
                       help="Force read-only mode (overrides role config)")
    parser.add_argument("--context", default="", help="Extra context to inject into every prompt")
    parser.add_argument("--poll-interval", type=float, default=None, help="Seconds between bus polls")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout per CLI invocation (seconds)")
    parser.add_argument("--tmux", action="store_true",
                        help="Run agent in a persistent tmux session (interactive mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually run the CLI — just print the prompt and exit")
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)
    registry = BackendRegistry.from_config(config)

    # Resolve role config
    roles_cfg = config.get("roles", {})
    role_cfg = roles_cfg.get(args.role)
    if not role_cfg:
        print(f"ERROR: Unknown role '{args.role}'. Available: {list(roles_cfg.keys())}", file=sys.stderr)
        sys.exit(1)

    # Resolve backend
    backend_name = args.backend or role_cfg.get("default_backend", "shell")
    backend = registry.get(backend_name)
    if not backend or not backend.is_available():
        # Try to find any available backend
        available = registry.list_available()
        if not available:
            print(f"ERROR: No available backends. Check council.yaml.", file=sys.stderr)
            sys.exit(1)
        print(f"WARNING: Backend '{backend_name}' not available, using: {available[0]}", file=sys.stderr)
        backend = registry.get(available[0])
        backend_name = available[0]

    # Resolve model
    model_id = args.model or role_cfg.get("default_model", "")
    if model_id and not backend.get_model(model_id):
        # Model not found, use first available
        models = backend.list_models()
        if models:
            model_id = models[0]
            print(f"WARNING: Model not found for {backend_name}, using: {model_id}", file=sys.stderr)

    # Resolve other settings
    read_only = args.read_only if args.read_only is not None else role_cfg.get("read_only", True)
    max_rounds = args.max_rounds or config.get("session", {}).get("max_rounds", 30)
    poll_interval = args.poll_interval or config.get("session", {}).get("poll_interval", 2.0)
    workdir = args.workdir or config.get("session", {}).get("workdir", ".")
    max_turns = args.max_turns or 10

    agent_id = args.agent_id or f"{args.role}-{uuid.uuid4().hex[:6]}"

    # Build the bus client — if session is provided, use the unified server API
    if args.session:
        bus = BusClient(f"{args.bus}/api/sessions/{args.session}")
    else:
        bus = BusClient(args.bus)

    # Join the council
    join_result = bus.join(agent_id, args.role, f"{backend_name}:{model_id}")
    if "error" in join_result:
        print(f"Failed to join council: {join_result['error']}", file=sys.stderr)
        sys.exit(1)

    model_label = f"{backend_name}/{model_id}" if model_id else backend_name
    print(f"Joined council as [{args.role}] agent_id={agent_id}")
    print(f"  Backend: {backend_name} | Model: {model_label} | Read-only: {read_only}")
    print(f"  Workdir: {workdir} | Max rounds: {max_rounds} | Max turns: {max_turns}")
    print(f"  Mode: {'tmux (interactive)' if args.tmux else 'one-shot (subprocess)'}")
    sys.stdout.flush()

    # Initialize tmux session if needed
    tmux_session = None
    if args.tmux:
        from council_tmux import TmuxSession, build_tmux_command, get_prompt_indicator
        backend_config = config.get("backends", {}).get(backend_name, {})
        tmux_cmd = build_tmux_command(backend_name, model_id, backend_config)
        prompt_ind = get_prompt_indicator(backend_name)
        tmux_session = TmuxSession(
            session_name=f"council-{agent_id}",
            command=tmux_cmd,
            workdir=workdir,
            prompt_indicator=prompt_ind,
        )
        print(f"  Tmux command: {tmux_cmd}")
        print(f"  Creating tmux session council-{agent_id}...")
        tmux_session.create()
        print(f"  Tmux session ready")
        sys.stdout.flush()

    last_seen_timestamp = 0
    round_num = 0

    try:
        while round_num < max_rounds:
            round_num += 1

            # Poll for new messages
            msgs_result = bus.get_messages(since=last_seen_timestamp)
            new_messages = msgs_result.get("messages", [])
            if new_messages:
                last_seen_timestamp = new_messages[-1]["timestamp"]

            # Get room state
            room = bus.get_room()
            open_votes = room.get("open_votes", [])

            # Skip if nothing new
            if not new_messages and not open_votes:
                time.sleep(poll_interval)
                round_num -= 1
                continue

            # Get full conversation
            all_msgs = bus.get_messages(since=0).get("messages", [])

            # Build prompt
            prompt = build_prompt(
                role=args.role,
                role_description=role_cfg.get("description", ""),
                conversation=all_msgs,
                open_votes=open_votes,
                agent_id=agent_id,
                round_num=round_num,
                workdir=workdir,
                extra_context=args.context,
                can_vote=role_cfg.get("can_vote", True),
                can_propose_vote=role_cfg.get("can_propose_vote", True),
            )

            print(f"\n--- Round {round_num} ---")
            print(f"  New msgs: {len(new_messages)} | Open votes: {len(open_votes)}")
            sys.stdout.flush()

            if args.dry_run:
                print(f"\n=== PROMPT ({len(prompt)} chars) ===")
                print(prompt[:500] + "..." if len(prompt) > 500 else prompt)
                print("=== END PROMPT (dry run, not executing) ===")
                continue

            # Run the backend
            result = backend.run(
                prompt=prompt,
                workdir=workdir,
                read_only=read_only,
                model_id=model_id,
                timeout=args.timeout,
            )

            if not result.success:
                print(f"  Backend error: {result.error[:200]}", file=sys.stderr)
                time.sleep(poll_interval)
                continue

            response = result.output
            if not response or not response.strip():
                print("  Empty response, skipping")
                time.sleep(poll_interval)
                continue

            # Parse response
            actions = parse_response(response)

            # Post message
            if actions["message"]:
                bus.post_message(agent_id, actions["message"])
                print(f"  Posted message ({len(actions['message'])} chars)")

            # Handle votes
            if role_cfg.get("can_vote", True):
                for vote_resp in actions["votes"]:
                    result = bus.respond_vote(
                        agent_id, vote_resp["vote_id"],
                        vote_resp["response"], vote_resp["rationale"]
                    )
                    print(f"  Vote {vote_resp['vote_id']}: {vote_resp['response']} -> {result.get('status', 'error')}")

            if role_cfg.get("can_propose_vote", True):
                if actions["propose_vote"]:
                    result = bus.propose_vote(
                        agent_id,
                        actions["propose_vote"]["proposal"],
                        actions["propose_vote"]["options"]
                    )
                    print(f"  Proposed vote: {result.get('vote_id', 'error')}")

            if actions["done"]:
                print("  Agent signaled DONE — exiting")
                break

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\nInterrupted, leaving council...")
    finally:
        bus.leave(agent_id)
        print(f"Left council (agent_id={agent_id})")


if __name__ == "__main__":
    main()