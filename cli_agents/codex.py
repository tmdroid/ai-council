"""OpenAI Codex CLI agent — codex --model <model> in interactive tmux session."""

import re
from cli_agent import CLIAgent


class CodexCLIAgent(CLIAgent):
    """OpenAI Codex CLI agent — codex --model <model> in interactive mode.

    Uses ChatGPT Plus/Pro subscription (NOT API tokens). The `codex exec`
    one-shot mode also uses the subscription, but interactive mode keeps
    conversation context across turns.

    Can read/write files, run shell commands, and manage git workflows.

    Prompt indicator: "❯" or ">" when ready for input
    Startup time: ~8 seconds
    """

    def __init__(self, name: str, model: str = "", workdir: str = ".", **kwargs):
        self.model = model
        super().__init__(name=name, workdir=workdir, **kwargs)

    def _build_command(self) -> str:
        if self.model:
            return f"codex --model {self.model}"
        return "codex"

    def _get_prompt_patterns(self) -> list[str]:
        return [r"❯\s*$", r">\s*$"]

    def _get_startup_wait(self) -> float:
        return 8.0