"""Ollama Claude Code agent — Claude Code CLI with Ollama as the model provider.

This gives us the best of both worlds:
- Claude Code's agentic capabilities (read files, run commands, grep, edit)
- Ollama's models (free local or cloud pay-per-use, no API tokens)

Claude Code connects to Ollama through its Anthropic-compatible API:
    ANTHROPIC_AUTH_TOKEN=ollama
    ANTHROPIC_API_KEY=""  (empty, not used)
    ANTHROPIC_BASE_URL=http://localhost:11434

This means the agent can autonomously explore a codebase, read files,
run shell commands, and edit code — all powered by Ollama models.

For non-interactive (one-shot) mode:
    claude --model glm-5.2:cloud --dangerously-skip-permissions -p "review this code"

For interactive mode (tmux):
    Start claude in a tmux session, send prompts via send-keys, capture output.

Note: --dangerously-skip-permissions is needed for autonomous operation.
In a council context, agents run in a controlled environment and need to
act without human approval prompts.
"""

import os
import re
from cli_agent import CLIAgent, AgentStatus


class OllamaClaudeCodeAgent(CLIAgent):
    """Claude Code CLI agent backed by Ollama models.

    Uses Claude Code's agentic tools (Read, Bash, Grep, Glob, Edit) with
    Ollama providing the model intelligence. No Anthropic API tokens needed.

    The agent can:
    - Read files autonomously (Read tool)
    - Run shell commands (Bash tool)
    - Search codebase (Grep/Glob tools)
    - Edit files (Edit tool)
    - Use subagents for parallel work

    Billing: Ollama cloud models are pay-per-use (not subscription).
             Local Ollama models are free.
             No Anthropic API tokens are used.
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
        super().__init__(name=name, workdir=workdir, **kwargs)

    def _build_command(self) -> str:
        parts = ["claude", "--model", self.model]
        if self.skip_permissions:
            parts.append("--dangerously-skip-permissions")
        return " ".join(parts)

    def _get_prompt_patterns(self) -> list[str]:
        # Claude Code interactive: "❯" or ">" when ready
        return [r"❯\s*$", r">\s*$"]

    def _get_startup_wait(self) -> float:
        return 8.0  # Claude Code + Ollama connection takes a moment

    def _build_env(self) -> dict:
        """Build environment variables for Ollama + Claude Code."""
        env = os.environ.copy()
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""
        env["ANTHROPIC_BASE_URL"] = self.ollama_host
        # Ensure claude is in PATH
        local_bin = os.path.expanduser("~/.local/bin")
        env["PATH"] = local_bin + ":" + env.get("PATH", "")
        return env

    def start(self) -> bool:
        """Start Claude Code with Ollama env vars in a tmux session."""
        import subprocess, time

        self._set_status(AgentStatus.STARTING)
        self._kill_tmux()

        try:
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

            # Set env vars and start Claude Code
            env = self._build_env()
            env_prefix = (
                f"ANTHROPIC_AUTH_TOKEN={env['ANTHROPIC_AUTH_TOKEN']} "
                f"ANTHROPIC_API_KEY={env['ANTHROPIC_API_KEY']} "
                f"ANTHROPIC_BASE_URL={env['ANTHROPIC_BASE_URL']} "
                f"PATH={env['PATH']} "
            )

            command = env_prefix + self._build_command()
            subprocess.run(
                ["tmux", "send-keys", "-t", self.tmux_session,
                 command, "Enter"],
                capture_output=True, check=True, timeout=5
            )

            time.sleep(self.startup_wait)

            if not self._tmux_alive():
                self._set_status(AgentStatus.ERROR)
                return False

            self._last_capture = self._capture_pane()
            self._set_status(AgentStatus.READY)
            return True

        except Exception:
            self._set_status(AgentStatus.ERROR)
            return False

    def send_oneshot(self, prompt: str, timeout: int = 300) -> str:
        """Run Claude Code in one-shot mode (-p flag) with Ollama.

        This is simpler than tmux for single-turn tasks. The agent reads files,
        runs commands, and returns a text response.
        """
        import subprocess

        env = self._build_env()
        cmd = ["claude", "--model", self.model, "-p"]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        cmd.append(prompt)

        try:
            result = subprocess.run(
                cmd, cwd=self.workdir, capture_output=True, text=True,
                timeout=timeout, env=env
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return "<error>Timeout</error>"
        except Exception as e:
            return f"<error>{e}</error>"

    def _clean_output(self, text: str) -> str:
        """Claude Code output is clean — minimal processing needed."""
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # Skip prompt indicators
            if stripped.endswith("❯") and len(stripped) < 30:
                continue
            if stripped == "" and len(cleaned) > 0 and cleaned[-1].strip() == "":
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()