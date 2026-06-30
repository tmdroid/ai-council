#!/usr/bin/env python3
"""
CLI Agent Interface — Abstract base and concrete implementations.

A clean abstraction layer between the council bus and coding CLIs. Each
implementation manages the lifecycle of a specific CLI tool (Claude Code,
Codex, Ollama, etc.) in a persistent tmux session.

The base class defines the interface:
    - start(): Launch the CLI
    - stop(): Kill the CLI
    - send(prompt): Send a message and get a response (context preserved)
    - send_shell(command): Run a shell command in the same session
    - is_alive(): Check if the CLI is running
    - get_status(): Return current status
    - capture(): Get raw pane content

Concrete implementations:
    - OllamaCLIAgent: ollama run <model> (interactive, free/local)
    - ClaudeCLIAgent: claude --model <model> (interactive, uses subscription)
    - CodexCLIAgent: codex --model <model> (interactive, uses subscription)
    - GenericCLIAgent: Any CLI with custom command and prompt patterns

Usage:
    from cli_agent import OllamaCLIAgent

    agent = OllamaCLIAgent(name="reviewer-01", model="glm-5.2:cloud", workdir="/repo")
    agent.start()
    response = agent.send("Review src/auth.py for security issues")
    agent.stop()

The agent keeps context across turns because the CLI runs in a persistent
tmux session — it never exits between messages.
"""

import subprocess
import time
import threading
import re
import os
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Callable


class AgentStatus(Enum):
    """Lifecycle states of a CLI agent."""
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    THINKING = "thinking"
    ERROR = "error"
    STOPPED = "stopped"


class CLIAgent(ABC):
    """
    Abstract base class for managing a single interactive coding CLI.

    The CLI runs persistently in a tmux session — it keeps conversation
    context across multiple send() calls. This is the key advantage over
    one-shot mode: the agent remembers what was said and what files it
    already read.

    Subclasses must implement:
        - _build_command(): Return the shell command to start the CLI
        - _get_prompt_patterns(): Return regex patterns for the CLI's ready prompt
        - _get_startup_wait(): Return seconds to wait after starting

    Subclasses may override:
        - _clean_output(): Backend-specific output cleaning
        - _get_startup_wait(): Different startup times per CLI
    """

    def __init__(
        self,
        name: str,
        workdir: str = ".",
        width: int = 200,
        height: int = 60,
        startup_wait: Optional[float] = None,
        response_timeout: int = 300,
        on_status_change: Optional[Callable[[AgentStatus, AgentStatus], None]] = None,
    ):
        self.name = name
        self.tmux_session = f"cli-{name}"
        self.workdir = workdir
        self.width = width
        self.height = height
        self.response_timeout = response_timeout
        self.on_status_change = on_status_change
        self.status = AgentStatus.CREATED
        self._last_capture = ""

        self.prompt_patterns = self._get_prompt_patterns()
        self.startup_wait = startup_wait if startup_wait is not None else self._get_startup_wait()

    # --- Abstract methods ---

    @abstractmethod
    def _build_command(self) -> str:
        """Return the shell command to start the CLI in interactive mode."""
        ...

    @abstractmethod
    def _get_prompt_patterns(self) -> list[str]:
        """Return regex patterns that match the CLI's ready-for-input prompt."""
        ...

    def _get_startup_wait(self) -> float:
        """Return seconds to wait after starting the CLI before first send."""
        return 5.0

    # --- Concrete methods (shared by all implementations) ---

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

            # Start the CLI (build command from subclass)
            command = self._build_command()
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session,
                 command, "Enter"],
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

            self._set_status(AgentStatus.READY)
            return True

        except Exception:
            self._set_status(AgentStatus.ERROR)
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
        return f"{self.__class__.__name__}(name={self.name!r}, status={self.status.value!r})"


# =============================================================================
# Factory — create the right agent type from config
# =============================================================================

def create_agent(
    agent_id: str,
    backend_name: str,
    model_id: str,
    config: dict,
    workdir: str = ".",
    on_status_change: Optional[Callable] = None,
) -> CLIAgent:
    """
    Create a CLIAgent from the council.yaml backend configuration.

    Returns the appropriate concrete implementation based on backend_name:
    - ollama -> OllamaCLIAgent
    - claude-code -> ClaudeCLIAgent
    - codex -> CodexCLIAgent
    - anything else -> GenericCLIAgent (built from config)
    """
    # Import here to avoid circular imports if implementations import from cli_agent
    from cli_agents import OllamaCLIAgent, ClaudeCLIAgent, CodexCLIAgent, GenericCLIAgent

    if backend_name == "ollama":
        return OllamaCLIAgent(
            name=agent_id, model=model_id, workdir=workdir,
            on_status_change=on_status_change,
        )
    elif backend_name == "claude-code":
        return ClaudeCLIAgent(
            name=agent_id, model=model_id, workdir=workdir,
            on_status_change=on_status_change,
        )
    elif backend_name == "codex":
        return CodexCLIAgent(
            name=agent_id, model=model_id, workdir=workdir,
            on_status_change=on_status_change,
        )
    else:
        # Generic fallback — build command from config
        backend = config.get("backends", {}).get(backend_name, {})
        command_base = backend.get("command", backend_name)
        models = backend.get("models", [])
        model_flag = ""
        for m in models:
            if m.get("id") == model_id and m.get("flag"):
                model_flag = m["flag"]
                break
        command = f"{command_base} {model_flag}".strip()
        return GenericCLIAgent(
            name=agent_id, command=command,
            prompt_patterns=[r"\$\s*$", r">\s*$"],
            workdir=workdir,
            on_status_change=on_status_change,
        )