"""Generic CLI agent for any tool with a custom command."""

from cli_agent import CLIAgent


class GenericCLIAgent(CLIAgent):
    """Generic CLI agent for any tool with a custom command.

    Use this for CLIs not explicitly supported (Copilot, OpenCode, etc.)
    or for custom shell-based agents.

    You must provide the command string and prompt patterns explicitly.
    """

    def __init__(
        self,
        name: str,
        command: str,
        prompt_patterns: list[str],
        workdir: str = ".",
        startup_wait: float = 5.0,
        **kwargs,
    ):
        self._command = command
        self._prompt_patterns = prompt_patterns
        self._startup_wait = startup_wait
        super().__init__(name=name, workdir=workdir, **kwargs)

    def _build_command(self) -> str:
        return self._command

    def _get_prompt_patterns(self) -> list[str]:
        return self._prompt_patterns

    def _get_startup_wait(self) -> float:
        return self._startup_wait