"""Ollama CLI agent — ollama run <model> in interactive tmux session."""

import re
from cli_agent import CLIAgent


class OllamaCLIAgent(CLIAgent):
    """Ollama CLI agent — ollama run <model> in interactive mode.

    Free (local) or pay-per-use (cloud). No subscription needed.
    Keeps conversation context in the interactive session.

    Prompt indicator: ">>> Send a message (/? for help)"
    Startup time: ~3 seconds
    """

    def __init__(self, name: str, model: str, workdir: str = ".", **kwargs):
        self.model = model
        super().__init__(name=name, workdir=workdir, **kwargs)

    def _build_command(self) -> str:
        return f"ollama run {self.model}"

    def _get_prompt_patterns(self) -> list[str]:
        return [r">>>\s+Send a message", r">>>\s*$"]

    def _get_startup_wait(self) -> float:
        return 3.0