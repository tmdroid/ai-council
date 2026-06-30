# cli_agents — concrete CLIAgent implementations
from .ollama import OllamaCLIAgent
from .claude import ClaudeCLIAgent
from .codex import CodexCLIAgent
from .generic import GenericCLIAgent
from .ollama_claude_code import OllamaClaudeCodeAgent

__all__ = [
    "OllamaCLIAgent",
    "ClaudeCLIAgent",
    "CodexCLIAgent",
    "GenericCLIAgent",
    "OllamaClaudeCodeAgent",
]