"""Claude Code CLI agent — claude --model <model> in interactive tmux session."""

import re
from cli_agent import CLIAgent


class ClaudeCLIAgent(CLIAgent):
    """Claude Code CLI agent — claude --model <model> in interactive mode.

    Uses Claude Pro/Max subscription (NOT API tokens). This is the key
    advantage over `claude -p` which bills to API credits as of June 15, 2026.

    Interactive mode keeps conversation context and can read/write files,
    run shell commands, and manage git workflows autonomously.

    Prompt indicator: "❯" or ">" when ready for input
    Startup time: ~10 seconds (Claude Code has a longer startup)
    """

    def __init__(self, name: str, model: str = "", workdir: str = ".", **kwargs):
        self.model = model
        super().__init__(name=name, workdir=workdir, **kwargs)

    def _build_command(self) -> str:
        if self.model:
            return f"claude --model {self.model}"
        return "claude"

    def _get_prompt_patterns(self) -> list[str]:
        return [r"❯\s*$", r">\s*$"]

    def _get_startup_wait(self) -> float:
        return 10.0

    def _clean_output(self, text: str) -> str:
        """Claude Code output is cleaner than Ollama — minimal cleaning needed."""
        lines = text.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # Skip empty trailing lines and prompt indicators
            if stripped.endswith("❯") and len(stripped) < 30:
                continue
            if stripped == "" and len(cleaned) > 0 and cleaned[-1].strip() == "":
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()