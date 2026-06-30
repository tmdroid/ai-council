#!/usr/bin/env python3
"""
CLI Agent Interface — Manages the lifecycle of an interactive coding CLI.

A clean abstraction layer between the council bus and the actual CLI tool
(Claude Code, Codex, Ollama, etc.). This module knows nothing about the
council, sessions, votes, or the bus. It only knows how to:

1. Start a CLI in a tmux session
2. Send text to it
3. Capture its output
4. Detect when it's ready for more input
5. Report its status
6. Shut it down cleanly

Usage:
    from cli_agent import CLIAgent

    agent = CLIAgent(
        name="reviewer-01",
        command="ollama run glm-5.2:cloud",
        workdir="/home/user/myproject",
    )
    agent.start()

    response = agent.send("Review src/auth.py for security issues")
    print(response)

    response = agent.send("Now check the tests for coverage gaps")
    print(response)  # context preserved from previous turn

    agent.stop()

The agent keeps context across turns because the CLI runs in a persistent
tmux session — it never exits between messages.
"""

import subprocess
import time
import threading
import re
import os
from enum import Enum
from typing import Optional, Callable


class AgentStatus(Enum):
    """Lifecycle states of a CLI agent."""
    CREATED = "created"       # Object exists but CLI not started
    STARTING = "starting"     # CLI is launching in tmux
    READY = "ready"           # CLI is waiting for input
    THINKING = "thinking"     # CLI is processing a prompt
    ERROR = "error"           # CLI crashed or returned an error
    STOPPED = "stopped"       # CLI was stopped by the user


class CLIAgent:
    """
    Manages a single interactive coding CLI in a tmux session.

    The CLI runs persistently — it keeps its conversation context across
    multiple send() calls. This is the key advantage over one-shot mode:
    the agent remembers what was said and what files it already read.

    Attributes:
        name: Unique identifier for this agent (used as tmux session name)
        command: The shell command that starts the CLI
        workdir: Working directory for the CLI
        status: Current AgentStatus
        on_status_change: Optional callback called when status changes
    """

    def __init__(
        self,
        name: str,
        command: str,
        workdir: str = ".",
        width: int = 200,
        height: int = 60,
        prompt_patterns: Optional[list[str]] = None,
        startup_wait: float = 5.0,
        response_timeout: int = 300,
        on_status_change: Optional[Callable[[AgentStatus, AgentStatus], None]] = None,
    ):
        """
        Args:
            name: Unique name (becomes the tmux session name, prefixed with "cli-")
            command: Shell command to start the CLI (e.g., "ollama run glm-5.2:cloud")
            workdir: Directory where the CLI runs
            width/height: tmux pane dimensions (wide enough for code output)
            prompt_patterns: List of regex patterns that match the CLI's ready prompt.
                             If None, auto-detected based on the command.
            startup_wait: Seconds to wait after starting the CLI before first send
            response_timeout: Default timeout for send() in seconds
            on_status_change: Callback(old_status, new_status) when status transitions
        """
        self.name = name
        self.tmux_session = f"cli-{name}"
        self.command = command
        self.workdir = workdir
        self.width = width
        self.height = height
        self.startup_wait = startup_wait
        self.response_timeout = response_timeout
        self.on_status_change = on_status_change
        self.status = AgentStatus.CREATED
        self._last_capture = ""
        self._last_capture_time = 0
        self._pane_history = []  # lines we've already processed

        # Auto-detect prompt patterns based on the command
        if prompt_patterns is None:
            self.prompt_patterns = self._detect_prompt_patterns(command)
        else:
            self.prompt_patterns = prompt_patterns

    def _detect_prompt_patterns(self, command: str) -> list[str]:
        """Auto-detect what the CLI's ready-for-input prompt looks like."""
        cmd_lower = command.lower()
        if "ollama" in cmd_lower:
            # Ollama interactive: ">>> Send a message (/? for help)"
            return [r">>>\s+Send a message", r">>>\s*$"]
        elif "claude" in cmd_lower:
            # Claude Code: shows "❯" or ">" when ready
            return [r"❯\s*$", r">\s*$"]
        elif "codex" in cmd_lower:
            return [r"❯\s*$", r">\s*$"]
        elif "opencode" in cmd_lower:
            return [r"❯\s*$", r">\s*$"]
        elif "copilot" in cmd_lower:
            return [r"❯\s*$", r">\s*$"]
        else:
            return [r"\$\s*$", r"#\s*$", r">\s*$"]

    def _set_status(self, new_status: AgentStatus):
        """Update status and fire callback."""
        old = self.status
        if old != new_status:
            self.status = new_status
            if self.on_status_change:
                try:
                    self.on_status_change(old, new_status)
                except Exception:
                    pass

    # ===================================================================
    # Lifecycle
    # ===================================================================

    def start(self) -> bool:
        """
        Start the CLI in a tmux session.

        Returns:
            True if the CLI started successfully, False otherwise.
        """
        self._set_status(AgentStatus.STARTING)

        # Kill any existing session with this name
        self._kill_tmux()

        try:
            # Create detached tmux session
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.tmux_session,
                 "-x", str(self.width), "-y", str(self.height)],
                capture_output=True, check=True, timeout=10
            )

            # Set working directory
            if self.workdir and os.path.isdir(self.workdir):
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.tmux_session,
                     f"cd {self.workdir}", "Enter"],
                    capture_output=True, check=True, timeout=5
                )
                time.sleep(0.5)

            # Start the CLI
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session,
                 self.command, "Enter"],
                capture_output=True, check=True, timeout=5
            )

            # Wait for startup
            time.sleep(self.startup_wait)

            # Check if the session is still alive
            if not self._tmux_alive():
                self._set_status(AgentStatus.ERROR)
                return False

            # Capture initial state
            self._last_capture = self._capture_pane()
            self._pane_history = self._last_capture.strip().split("\n")

            self._set_status(AgentStatus.READY)
            return True

        except Exception as e:
            self._set_status(AgentStatus.ERROR)
            if self.on_status_change:
                self.on_status_change(self.status, self.status)
            return False

    def stop(self):
        """Stop the CLI and kill the tmux session."""
        # Try to exit gracefully first
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session, "C-c", ""],
                capture_output=True, timeout=3
            )
            time.sleep(1)
        except Exception:
            pass

        self._kill_tmux()
        self._set_status(AgentStatus.STOPPED)

    def is_alive(self) -> bool:
        """Check if the tmux session (and thus the CLI) is still running."""
        return self._tmux_alive()

    def restart(self) -> bool:
        """Restart the CLI (loses conversation context)."""
        self.stop()
        time.sleep(1)
        return self.start()

    # ===================================================================
    # Communication
    # ===================================================================

    def send(self, prompt: str, timeout: Optional[int] = None) -> str:
        """
        Send a prompt to the CLI and wait for its response.

        The CLI keeps context from previous send() calls because it runs
        in a persistent tmux session.

        Args:
            prompt: The text to type into the CLI
            timeout: Max seconds to wait (defaults to self.response_timeout)

        Returns:
            The CLI's response text, cleaned of spinners and artifacts.
            Empty string if the CLI produced no output or timed out.
        """
        if not self.is_alive():
            self._set_status(AgentStatus.ERROR)
            return ""

        if self.status == AgentStatus.STOPPED:
            return ""

        timeout = timeout or self.response_timeout
        self._set_status(AgentStatus.THINKING)

        # Capture baseline (what's on screen now, before we type)
        baseline = self._capture_pane()
        baseline_lines = baseline.strip().split("\n")
        baseline_count = len(baseline_lines)

        # Type the prompt into the tmux session
        # For multi-line prompts, send each line followed by Enter
        # But for the last line, send Enter to submit
        lines = prompt.split("\n")
        for i, line in enumerate(lines):
            # Send the line text
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session, line],
                capture_output=True, check=True, timeout=5
            )
            # Send Enter after each line
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session, "Enter"],
                capture_output=True, check=True, timeout=5
            )

        # Wait for the CLI to process and return to ready state
        response_text = self._wait_for_response(baseline_count, timeout)

        self._set_status(AgentStatus.READY)
        return response_text

    def send_shell(self, command: str, timeout: int = 30) -> str:
        """
        Run a shell command in the tmux session (not through the CLI).

        This sends Ctrl+C first to interrupt the CLI, then runs the command
        as a shell command, captures output, then restarts the CLI.

        Useful for reading files or running tests that the CLI itself can't
        execute (e.g., Ollama which is not an agentic CLI).

        Args:
            command: Shell command to run
            timeout: Max seconds to wait

        Returns:
            The shell command's output.
        """
        if not self.is_alive():
            return ""

        # Interrupt whatever is running
        subprocess.run(
            ["tmux", "send-keys", "-t", self.tmux_session, "C-c", ""],
            capture_output=True, timeout=3
        )
        time.sleep(1)

        # Capture baseline
        baseline = self._capture_pane()
        baseline_count = len(baseline.strip().split("\n"))

        # Send the shell command
        subprocess.run(
            ["tmux", "send-keys", "-t", self.tmux_session, command, "Enter"],
            capture_output=True, check=True, timeout=5
        )

        # Wait for shell to return
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(0.5)
            pane = self._capture_pane()
            last_line = pane.strip().split("\n")[-1].strip() if pane.strip() else ""
            if re.search(r"\$\s*$", last_line):
                break

        after = self._capture_pane()
        after_lines = after.strip().split("\n")
        new_lines = after_lines[baseline_count:]
        return "\n".join(new_lines).strip()

    # ===================================================================
    # Output capture and processing
    # ===================================================================

    def capture(self) -> str:
        """Capture the current pane content (raw, unfiltered)."""
        return self._capture_pane()

    def get_status(self) -> AgentStatus:
        """Return the current status."""
        return self.status

    def get_status_string(self) -> str:
        """Return status as a human-readable string."""
        return self.status.value

    # ===================================================================
    # Internal helpers
    # ===================================================================

    def _wait_for_response(self, baseline_count: int, timeout: int) -> str:
        """Wait for the CLI to finish processing and capture the new output."""
        start = time.time()
        stable_count = 0
        last_pane = ""
        poll_interval = 2.0  # Ollama cloud is slow, poll every 2s

        while time.time() - start < timeout:
            time.sleep(poll_interval)
            pane = self._capture_pane()

            # Check if output has stabilized (same content for 3 consecutive polls)
            # Only count as stable if there's no spinner character on the last line
            lines = pane.strip().split("\n")
            last_line = lines[-1].strip() if lines else ""
            has_spinner = any(c in last_line for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

            if not has_spinner:
                if pane == last_pane:
                    stable_count += 1
                    if stable_count >= 2:  # 2 consecutive stable polls = done
                        break
                else:
                    stable_count = 0
                    last_pane = pane

                # Check for prompt indicator (CLI is ready for input)
                for pattern in self.prompt_patterns:
                    if re.search(pattern, last_line):
                        # Prompt found — CLI is ready
                        break

        # Capture final output
        final_pane = self._capture_pane()
        final_lines = final_pane.strip().split("\n")

        # Extract only new lines (after baseline)
        # But we need to find where the new content starts — it's after the
        # line that contains our echoed prompt. Look for the first line that
        # appears after the baseline that isn't empty.
        new_lines = []
        past_baseline = False
        for i, line in enumerate(final_lines):
            if not past_baseline:
                if i >= baseline_count:
                    past_baseline = True
                else:
                    continue
            # Skip the echoed prompt line (the line that matches what we typed)
            # and the ">>> Send a message" ready prompt at the end
            if re.search(r">>>\s+Send a message", line):
                continue
            new_lines.append(line)

        # Clean the output
        cleaned = self._clean_output("\n".join(new_lines))
        return cleaned

    def _clean_output(self, text: str) -> str:
        """Clean captured output: remove spinners, thinking blocks, echoes, prompts."""
        lines = text.split("\n")
        cleaned = []
        in_thinking = False

        for line in lines:
            stripped = line.strip()

            # Skip progress spinner characters (Ollama, Claude Code)
            if any(c in stripped for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
                continue

            # Skip Ollama "Thinking..." blocks
            if stripped == "Thinking...":
                in_thinking = True
                continue
            if stripped.startswith("...done thinking"):
                in_thinking = False
                continue
            if in_thinking:
                continue

            # Skip trailing shell prompts
            if re.match(r"^.*[\$#❯>]\s*$", stripped) and len(stripped) < 60:
                continue

            # Skip Ollama ">>>" prompt
            if stripped == ">>>" or stripped == ">>> ":
                continue

            cleaned.append(line)

        # Remove leading/trailing empty lines
        result = "\n".join(cleaned).strip()
        return result

    def _capture_pane(self, history_lines: int = 300) -> str:
        """Capture the current tmux pane content."""
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", self.tmux_session,
                 "-p", "-S", f"-{history_lines}"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout
        except Exception:
            return ""

    def _tmux_alive(self) -> bool:
        """Check if the tmux session exists."""
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", self.tmux_session],
                capture_output=True, timeout=3
            )
            return result.returncode == 0
        except Exception:
            return False

    def _kill_tmux(self):
        """Kill the tmux session if it exists."""
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", self.tmux_session],
                capture_output=True, timeout=3
            )
        except Exception:
            pass

    def __del__(self):
        """Cleanup on garbage collection."""
        try:
            self.stop()
        except Exception:
            pass

    def __repr__(self):
        return f"CLIAgent(name={self.name!r}, command={self.command!r}, status={self.status.value!r})"


# =============================================================================
# Factory function — build a CLIAgent from council.yaml backend config
# =============================================================================

def create_agent_from_config(
    agent_id: str,
    backend_name: str,
    model_id: str,
    config: dict,
    workdir: str = ".",
    on_status_change: Optional[Callable] = None,
) -> CLIAgent:
    """
    Create a CLIAgent from the council.yaml backend configuration.

    Args:
        agent_id: Unique agent identifier
        backend_name: Backend name from council.yaml (e.g., "ollama", "claude-code")
        model_id: Model ID (e.g., "glm-5.2:cloud", "opus")
        config: The full council.yaml config dict
        workdir: Working directory
        on_status_change: Status change callback

    Returns:
        A CLIAgent configured for the specified backend and model.
    """
    backends = config.get("backends", {})
    backend = backends.get(backend_name, {})
    command_base = backend.get("command", backend_name)

    # Build the interactive command for each backend type
    if backend_name == "ollama":
        command = f"{command_base} run {model_id}"
        prompt_patterns = [r">>>\s*$", r"\$\s*$"]
        startup_wait = 3.0

    elif backend_name == "claude-code":
        model_flag = f"--model {model_id}" if model_id else ""
        command = f"{command_base} {model_flag}".strip()
        prompt_patterns = [r"❯\s*$", r">\s*$"]
        startup_wait = 10.0  # Claude Code takes longer to start

    elif backend_name == "codex":
        model_flag = f"--model {model_id}" if model_id else ""
        command = f"{command_base} {model_flag}".strip()
        prompt_patterns = [r"❯\s*$", r">\s*$"]
        startup_wait = 8.0

    elif backend_name == "opencode":
        command = f"{command_base}"
        prompt_patterns = [r"❯\s*$", r">\s*$"]
        startup_wait = 5.0

    elif backend_name == "copilot":
        command = f"{command_base}"
        prompt_patterns = [r"❯\s*$", r">\s*$"]
        startup_wait = 5.0

    else:
        # Generic: use the command with model flag if available
        models = backend.get("models", [])
        model_flag = ""
        for m in models:
            if m.get("id") == model_id and m.get("flag"):
                model_flag = m["flag"]
                break
        command = f"{command_base} {model_flag}".strip()
        prompt_patterns = [r"\$\s*$", r">\s*$"]
        startup_wait = 5.0

    return CLIAgent(
        name=agent_id,
        command=command,
        workdir=workdir,
        prompt_patterns=prompt_patterns,
        startup_wait=startup_wait,
        on_status_change=on_status_change,
    )