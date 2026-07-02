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
import json
from cli_agent import CLIAgent, AgentStatus


class OllamaClaudeCodeAgent(CLIAgent):
    """Claude Code CLI agent backed by Ollama models.

    Uses Claude Code's agentic tools (Read, Bash, Grep, Glob, Edit) with
    Ollama providing the model intelligence. No Anthropic API tokens needed.

    Context is preserved across send() calls using --continue flag:
    First call: claude --model X -p "prompt"
    Subsequent: claude --model X --continue -p "prompt"

    This gives us the agentic capabilities of Claude Code (file reading,
    command execution, code search) with Ollama models as the brain, and
    the conversation context persists between rounds.

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
        self._first_call = True
        super().__init__(name=name, workdir=workdir, **kwargs)

    def _build_command(self) -> str:
        # Not used for tmux — we use subprocess directly
        parts = ["claude", "--model", self.model]
        if self.skip_permissions:
            parts.append("--dangerously-skip-permissions")
        return " ".join(parts)

    def _get_prompt_patterns(self) -> list[str]:
        return [r"❯\s*$", r">\s*$"]

    def _get_startup_wait(self) -> float:
        return 2.0  # No tmux startup needed

    def _build_env(self) -> dict:
        """Build environment variables for Ollama + Claude Code."""
        env = os.environ.copy()
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""
        env["ANTHROPIC_BASE_URL"] = self.ollama_host
        local_bin = os.path.expanduser("~/.local/bin")
        env["PATH"] = local_bin + ":" + env.get("PATH", "")
        return env

    def start(self) -> bool:
        """No tmux session needed — we use subprocess -p mode with --continue."""
        self._set_status(AgentStatus.READY)
        self._first_call = True
        return True

    def send(self, prompt: str, timeout: int = 300, on_progress=None) -> str:
        """Send a prompt to Claude Code and get a response.

        Uses -p (print) mode with --continue for context preservation.
        First call starts a new session; subsequent calls continue it.

        Args:
            prompt: The text to send to Claude Code
            timeout: Max seconds to wait
            on_progress: Optional callback(event_type, event_data) for streaming.
                     If provided, uses --output-format stream-json --verbose
                     and calls on_progress for each event (tool_use, tool_result,
                     thinking, etc.). The final text result is still returned.
        """
        import subprocess

        self._set_status(AgentStatus.THINKING)

        env = self._build_env()
        cmd = ["claude", "--model", self.model, "-p"]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if not self._first_call:
            cmd.append("--continue")
        cmd.append(prompt)
        self._first_call = False

        try:
            if on_progress:
                # Streaming mode — capture events as they happen
                cmd += ["--output-format", "stream-json", "--verbose"]
                result = subprocess.run(
                    cmd, cwd=self.workdir, capture_output=True, text=True,
                    timeout=timeout, env=env
                )
                # Parse stream-json lines and call on_progress for each
                final_text = ""
                for line in result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        event_type = event.get("type", "")
                        if event_type == "assistant":
                            msg = event.get("message", {})
                            content = msg.get("content", [])
                            for block in content:
                                if block.get("type") == "tool_use":
                                    tool_name = block.get("name", "?")
                                    tool_input = block.get("input", {})
                                    on_progress("tool_use", {
                                        "tool": tool_name,
                                        "input": tool_input,
                                    })
                                elif block.get("type") == "text":
                                    final_text = block.get("text", "")
                        elif event_type == "user":
                            msg = event.get("message", {})
                            content = msg.get("content", [])
                            for block in content:
                                if block.get("type") == "tool_result":
                                    on_progress("tool_result", {
                                        "content": str(block.get("content", ""))[:200],
                                    })
                        elif event_type == "result":
                            final_text = event.get("result", final_text)
                    except json.JSONDecodeError:
                        pass

                self._set_status(AgentStatus.READY)
                return self._clean_output(final_text)
            else:
                # Non-streaming mode — just get the final output
                result = subprocess.run(
                    cmd, cwd=self.workdir, capture_output=True, text=True,
                    timeout=timeout, env=env
                )
                self._set_status(AgentStatus.READY)
                output = result.stdout.strip()
                return self._clean_output(output)
        except subprocess.TimeoutExpired:
            self._set_status(AgentStatus.ERROR)
            return "<error>Timeout</error>"
        except Exception as e:
            self._set_status(AgentStatus.ERROR)
            return f"<error>{e}</error>"

    def stop(self):
        """Nothing to stop — no persistent process."""
        self._set_status(AgentStatus.STOPPED)

    def is_alive(self) -> bool:
        """Always alive — we spawn a new process per call."""
        return self.status not in (AgentStatus.STOPPED, AgentStatus.ERROR)