"""Ollama Claude Code agent — Claude Code CLI with Ollama in tmux interactive mode.

Uses Claude Code's agentic tools (Read, Bash, Grep, Glob, Edit) with
Ollama providing the model intelligence. No Anthropic API tokens needed.

Runs in a persistent tmux session — the CLI stays interactive between
send() calls, preserving full conversation context. This is critical:
- Works with Ollama (free/local or cloud)
- Will work with real Claude Code (subscription, NOT -p which burns API tokens)
- Same architecture for all backends

The tmux flow:
1. Create tmux session, start Claude Code with Ollama env vars
2. Accept the "dangerously-skip-permissions" dialog (Down + Enter)
3. Wait for the ❯ ready prompt
4. For each send(): type prompt with -l (literal), wait for ❯ to reappear
5. Capture-pane to read the response

Billing: Ollama cloud = pay-per-use. Local Ollama = free.
         No Anthropic API tokens used (routed through Ollama API).
         For real Claude Code: uses subscription (interactive mode).
"""

import os
import re
import json
import subprocess
import time
from cli_agent import CLIAgent, AgentStatus


class OllamaClaudeCodeAgent(CLIAgent):
    """Claude Code CLI agent backed by Ollama models, in tmux interactive mode.

    Context is preserved across send() calls because Claude Code runs
    persistently in a tmux session — it never exits between messages.
    """

    def __init__(
        self,
        name: str,
        model: str,
        workdir: str = ".",
        ollama_host: str = "http://localhost:11434",
        skip_permissions: bool = True,
        **kwargs,
    ):
        self.model = model
        self.ollama_host = ollama_host
        self.skip_permissions = skip_permissions
        self._first_call = True
        self._last_pane_lines = 0
        super().__init__(name=name, workdir=workdir, **kwargs)

    def _build_command(self) -> str:
        parts = ["claude", "--model", self.model]
        if self.skip_permissions:
            parts.append("--dangerously-skip-permissions")
        return " ".join(parts)

    def _get_prompt_patterns(self) -> list[str]:
        # Claude Code: ❯ on its own line (not the status bar)
        return [r"❯\s*$"]

    def _get_startup_wait(self) -> float:
        return 10.0

    def _build_env(self) -> dict:
        env = os.environ.copy()
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""
        env["ANTHROPIC_BASE_URL"] = self.ollama_host
        local_bin = os.path.expanduser("~/.local/bin")
        env["PATH"] = local_bin + ":" + env.get("PATH", "")
        return env

    def _is_ready(self, pane: str) -> bool:
        """Check if Claude Code is ready for input (❯ on a line, no spinner)."""
        lines = pane.strip().split("\n")
        for line in lines[-5:]:  # check last 5 lines
            stripped = line.strip()
            if re.search(r"❯\s*$", stripped):
                # Make sure there's no spinner on this line or nearby
                if not any(c in stripped for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
                    return True
        return False

    def start(self) -> bool:
        """Start Claude Code in a tmux session with Ollama env vars."""
        self._set_status(AgentStatus.STARTING)
        self._kill_tmux()

        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.tmux_session,
                 "-x", str(self.width), "-y", str(self.height)],
                capture_output=True, check=True, timeout=10
            )

            # cd to workdir
            if self.workdir and os.path.isdir(self.workdir):
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.tmux_session, "-l",
                     f"cd {self.workdir}"],
                    capture_output=True, check=True, timeout=5
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.tmux_session, "Enter"],
                    capture_output=True, check=True, timeout=5
                )
                time.sleep(0.5)

            # Start Claude Code with Ollama env vars
            env = self._build_env()
            env_prefix = (
                f"ANTHROPIC_AUTH_TOKEN={env['ANTHROPIC_AUTH_TOKEN']} "
                f'ANTHROPIC_API_KEY={env["ANTHROPIC_API_KEY"]!r} '
                f"ANTHROPIC_BASE_URL={env['ANTHROPIC_BASE_URL']} "
                f"PATH={env['PATH']} "
            )
            command = env_prefix + self._build_command()
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session, "-l", command],
                capture_output=True, check=True, timeout=5
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session, "Enter"],
                capture_output=True, check=True, timeout=5
            )

            time.sleep(5)

            # Accept the permission dialog if present
            pane = self._capture_pane()
            if "Yes, I accept" in pane or "accept" in pane.lower():
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.tmux_session, "Down"],
                    capture_output=True, check=True, timeout=5
                )
                time.sleep(0.5)
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.tmux_session, "Enter"],
                    capture_output=True, check=True, timeout=5
                )
                time.sleep(5)

            # Wait for ready prompt
            for i in range(15):
                time.sleep(2)
                pane = self._capture_pane()
                if self._is_ready(pane):
                    self._set_status(AgentStatus.READY)
                    self._last_pane_lines = len(pane.strip().split("\n"))
                    return True

            # Timeout waiting for ready
            self._set_status(AgentStatus.ERROR)
            return False

        except Exception:
            self._set_status(AgentStatus.ERROR)
            return False

    def send(self, prompt: str, timeout: int = 300, on_progress=None) -> str:
        """Send a prompt to Claude Code and get a response.

        Uses tmux send-keys -l (literal) to type the entire prompt as one
        string, then waits for the ❯ ready prompt to reappear.
        Context is preserved — Claude Code stays interactive between calls.

        For long prompts (conversation history), writes to a temp file
        and tells Claude Code to read it, avoiding tmux length limits.
        """
        if not self.is_alive():
            self._set_status(AgentStatus.ERROR)
            return ""

        self._set_status(AgentStatus.THINKING)

        # Capture baseline (current pane state)
        baseline = self._capture_pane()
        baseline_lines = baseline.strip().split("\n")
        baseline_count = len(baseline_lines)

        # For long prompts, write to a temp file and tell Claude Code to read it
        if len(prompt) > 500:
            import tempfile
            prompt_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', prefix=f'council_{self.name}_',
                dir='/tmp', delete=False
            )
            prompt_file.write(prompt)
            prompt_file.close()
            short_prompt = f"Read the file {prompt_file.name} and follow the instructions in it. That file contains your task and the full council conversation context."
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session, "-l", short_prompt],
                capture_output=True, check=True, timeout=5
            )
        else:
            # Short prompt — send directly
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session, "-l", prompt],
                capture_output=True, check=True, timeout=5
            )

        subprocess.run(
            ["tmux", "send-keys", "-t", self.tmux_session, "Enter"],
            capture_output=True, check=True, timeout=5
        )

        # Wait for response — poll for ❯ ready prompt
        start_time = time.time()
        last_pane = ""
        stable_count = 0

        while time.time() - start_time < timeout:
            time.sleep(2)
            pane = self._capture_pane()

            # Check if ready (❯ prompt visible, no spinner)
            if self._is_ready(pane):
                # Check output has stabilized (same pane for 2 polls)
                if pane == last_pane:
                    stable_count += 1
                    if stable_count >= 1:
                        break
                else:
                    stable_count = 0
                    last_pane = pane

        # Capture final output
        final_pane = self._capture_pane()
        final_lines = final_pane.strip().split("\n")

        # Extract new lines (after baseline)
        new_lines = final_lines[baseline_count:] if len(final_lines) > baseline_count else []

        # Clean and return
        response = self._clean_output("\n".join(new_lines))
        self._set_status(AgentStatus.READY)
        return response

    def stop(self):
        """Kill the tmux session."""
        self._kill_tmux()
        self._set_status(AgentStatus.STOPPED)

    def is_alive(self) -> bool:
        """Check if the tmux session is running."""
        return self._tmux_alive()

    def _clean_output(self, text: str) -> str:
        """Clean captured tmux output: remove UI elements, prompt indicators, etc."""
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # Skip prompt indicators
            if re.match(r"^❯\s*$", stripped):
                continue
            # Skip status bar
            if "bypass permissions" in stripped:
                continue
            if "shift+tab" in stripped:
                continue
            if "for agents" in stripped:
                continue
            # Skip separator lines
            if re.match(r"^─+$", stripped):
                continue
            if re.match(r"^╭[─╰╮╯│]+$", stripped):
                continue
            # Skip "Read N file" / "Edited N file" status lines
            if re.match(r"^  (Read|Edited|Wrote|Created) \d+ file", stripped):
                continue
            # Skip "Brewed for Xs" / "Cogitated for Xs"
            if re.match(r"^✻ (Brewed|Cogitated|Pondered) for \d+s", stripped):
                continue
            # Skip empty trailing lines
            if stripped == "" and len(cleaned) > 0 and cleaned[-1].strip() == "":
                continue
            cleaned.append(line)

        return "\n".join(cleaned).strip()