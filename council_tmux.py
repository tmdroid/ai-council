#!/usr/bin/env python3
"""
Council Tmux Backend — Run agents in persistent tmux sessions.

Each agent gets its own tmux session. The harness:
1. Creates a tmux session on first use
2. Types the prompt via `tmux send-keys`
3. Waits for the CLI to finish (polls capture-pane for the prompt indicator)
4. Reads the output via `tmux capture-pane`
5. The session persists — the agent keeps context across turns

This works with ALL CLIs:
- Claude Code (interactive mode, uses subscription not API tokens)
- Codex (interactive mode, uses ChatGPT subscription)
- Ollama (interactive mode, free/local)
- Any other CLI that reads from stdin and produces output

Usage from the agent harness:
    from council_tmux import TmuxSession
    session = TmuxSession("agent-01", "ollama run glm-5.2:cloud", workdir="/repo")
    output = session.send("Review this code for bugs")
    # ... later, context preserved ...
    output = session.send("Now check the tests")
    session.close()
"""

import subprocess
import time
import re
import os
from typing import Optional


class TmuxSession:
    """A persistent tmux session for running an AI agent CLI interactively."""

    def __init__(self, session_name: str, command: str, workdir: str = ".",
                 width: int = 200, height: int = 50,
                 prompt_indicator: str = "$",  # shell prompt that indicates "ready for input"
                 startup_wait: float = 3.0):
        """
        Args:
            session_name: Unique tmux session name (e.g., "council-agent-01")
            command: The CLI command to run (e.g., "ollama run glm-5.2:cloud")
            workdir: Working directory for the agent
            width/height: tmux pane dimensions
            prompt_indicator: String that appears when the CLI is ready for input.
                              For shell-based agents, this is "$".
                              For Claude Code, it's ">" or "❯".
                              For Ollama interactive, it's ">>>".
            startup_wait: Seconds to wait for the CLI to start up
        """
        self.session_name = session_name
        self.command = command
        self.workdir = workdir
        self.width = width
        self.height = height
        self.prompt_indicator = prompt_indicator
        self.startup_wait = startup_wait
        self.created = False
        self.last_output_line = 0  # track how many lines we've seen

    def create(self):
        """Create the tmux session and start the CLI inside it."""
        # Kill any existing session with this name
        self.close()

        # Create a new detached tmux session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session_name,
             "-x", str(self.width), "-y", str(self.height)],
            capture_output=True, check=True
        )

        # Set the working directory
        if self.workdir and self.workdir != ".":
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, f"cd {self.workdir}", "Enter"],
                capture_output=True, check=True
            )
            time.sleep(0.5)

        # Start the CLI command
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, self.command, "Enter"],
            capture_output=True, check=True
        )

        # Wait for the CLI to start up
        time.sleep(self.startup_wait)
        self.created = True
        self.last_output_line = 0

    def send(self, prompt: str, timeout: int = 120, poll_interval: float = 1.0) -> str:
        """
        Send a prompt to the agent and wait for the response.

        Args:
            prompt: The text to type into the CLI
            timeout: Maximum seconds to wait for a response
            poll_interval: How often to check if the CLI is done

        Returns:
            The agent's response text (cleaned)
        """
        if not self.created:
            self.create()

        # Capture current pane state to know where new output starts
        before = self._capture_pane()
        before_lines = before.strip().split("\n")
        baseline_count = len(before_lines)

        # Type the prompt into the tmux session
        # Use send-keys with the text, then Enter
        # For multi-line prompts, we send each line separately
        for line in prompt.split("\n"):
            # Escape special tmux characters
            safe_line = line.replace("'", "'\\''")
            subprocess.run(
                ["tmux", "send-keys", "-t", self.session_name, line, "Enter"],
                capture_output=True, check=True
            )

        # Wait for the CLI to process and return to ready state
        # We poll capture-pane and look for the prompt indicator
        # at the bottom of the pane
        start_time = time.time()
        stable_count = 0
        last_pane = ""

        while time.time() - start_time < timeout:
            time.sleep(poll_interval)
            pane = self._capture_pane()

            # Check if the output has stabilized (no change in 3 polls)
            if pane == last_pane:
                stable_count += 1
                if stable_count >= 3:
                    break
            else:
                stable_count = 0
                last_pane = pane

            # Also check for prompt indicator at the bottom
            last_line = pane.strip().split("\n")[-1].strip() if pane.strip() else ""
            if self.prompt_indicator in last_line and len(last_line) < 20:
                # Prompt indicator found — CLI is ready
                break

        # Capture the final output
        after = self._capture_pane()
        after_lines = after.strip().split("\n")

        # Extract only the new lines (after our baseline)
        # Find where the new output starts by looking for the last prompt line
        new_lines = []
        found_baseline = False
        for i, line in enumerate(after_lines):
            if not found_baseline:
                # Skip lines we already had
                if i < baseline_count:
                    continue
                found_baseline = True
            new_lines.append(line)

        # Clean the output: remove the echoed prompt, prompt indicators, etc.
        cleaned = self._clean_output("\n".join(new_lines))
        return cleaned

    def send_oneshot(self, prompt: str, timeout: int = 120) -> str:
        """
        Send a one-shot command (not to the interactive CLI, but to the shell).
        Useful for running shell commands inside the same tmux session.

        This sends Ctrl+C first (to interrupt any running CLI), then runs
        the command as a shell command, then captures output.
        """
        if not self.created:
            self.create()

        # Send Ctrl+C to interrupt any running process
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, "C-c", ""],
            capture_output=True, check=True
        )
        time.sleep(1)

        # Now send the shell command
        before = self._capture_pane()
        before_lines = before.strip().split("\n")
        baseline_count = len(before_lines)

        subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, prompt, "Enter"],
            capture_output=True, check=True
        )

        # Wait for the shell to return
        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(0.5)
            pane = self._capture_pane()
            last_line = pane.strip().split("\n")[-1].strip() if pane.strip() else ""
            if "$" in last_line or ">" in last_line:
                break

        after = self._capture_pane()
        after_lines = after.strip().split("\n")

        # Extract new lines
        new_lines = after_lines[baseline_count:]
        return "\n".join(new_lines).strip()

    def is_alive(self) -> bool:
        """Check if the tmux session is still running."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            capture_output=True
        )
        return result.returncode == 0

    def close(self):
        """Kill the tmux session."""
        subprocess.run(
            ["tmux", "kill-session", "-t", self.session_name],
            capture_output=True
        )
        self.created = False

    def _capture_pane(self, lines: int = 200) -> str:
        """Capture the current content of the tmux pane."""
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self.session_name, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True
        )
        return result.stdout

    def _clean_output(self, text: str) -> str:
        """Clean the captured output: remove spinners, thinking blocks, echoed input."""
        lines = text.split("\n")
        cleaned = []
        in_thinking = False
        skip_echo = True  # skip the first few lines (echoed prompt)

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Skip progress spinner lines
            if any(c in stripped for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
                continue

            # Skip "Thinking..." and "...done thinking." markers (Ollama)
            if stripped == "Thinking...":
                in_thinking = True
                continue
            if stripped.startswith("...done thinking"):
                in_thinking = False
                continue
            if in_thinking:
                continue

            # Skip shell prompt lines at the end
            if stripped.endswith("$") and len(stripped) < 50:
                continue

            # Skip empty trailing lines
            if stripped == "" and i >= len(lines) - 3:
                continue

            cleaned.append(line)

        return "\n".join(cleaned).strip()


# =============================================================================
# Backend integration — wraps TmuxSession for use with the agent harness
# =============================================================================

def build_tmux_command(backend_name: str, model_id: str, config: dict) -> str:
    """Build the CLI command to run inside a tmux session.

    Args:
        backend_name: e.g., "ollama", "claude-code", "codex"
        model_id: e.g., "glm-5.2:cloud", "opus", "gpt-5.5"
        config: The backend config from council.yaml

    Returns:
        The shell command string to start the CLI in interactive mode
    """
    command = config.get("command", backend_name)

    if backend_name == "ollama":
        # ollama run <model> — interactive mode
        return f"{command} run {model_id}"

    elif backend_name == "claude-code":
        # claude --model <model> — interactive mode (uses subscription)
        model_flag = f"--model {model_id}" if model_id else ""
        return f"{command} {model_flag}".strip()

    elif backend_name == "codex":
        # codex --model <model> — interactive mode (uses subscription)
        model_flag = f"--model {model_id}" if model_id else ""
        return f"{command} {model_flag}".strip()

    elif backend_name == "opencode":
        # opencode — interactive TUI
        return f"{command}"

    elif backend_name == "copilot":
        # copilot — interactive
        return f"{command}"

    else:
        # Generic: just run the command with model flag if available
        models = config.get("models", [])
        model_flag = ""
        for m in models:
            if m["id"] == model_id and m.get("flag"):
                model_flag = m["flag"]
                break
        return f"{command} {model_flag}".strip()


def get_prompt_indicator(backend_name: str) -> str:
    """Get the prompt indicator string for each CLI (what shows when ready for input)."""
    indicators = {
        "ollama": ">>>",       # Ollama interactive prompt
        "claude-code": "❯",   # Claude Code TUI prompt
        "codex": "❯",         # Codex TUI prompt
        "opencode": "❯",
        "copilot": "❯",
    }
    return indicators.get(backend_name, "$")