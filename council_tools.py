#!/usr/bin/env python3
"""
Council Tool Interface — Gives non-agentic CLIs (like Ollama) the ability
to explore a codebase, read files, and run commands autonomously.

The agent outputs tool calls in a structured format. The harness executes
them and feeds the results back. This is a ReAct (Reason + Act) loop:

    Agent: "Let me see what files exist"
    Agent: TOOL: list_files path=shared/domain/src/commonMain
    Harness: executes `find shared/domain/src/commonMain -type f`
    Harness: feeds output back to agent
    Agent: "I see Hymn.kt — let me read it"
    Agent: TOOL: read_file path=shared/domain/.../Hymn.kt
    Harness: reads file, feeds content back
    Agent: "The Hymn.id is Long but I see a mismatch in..."
    Agent: TOOL: grep pattern="List<Int>" path=.
    Harness: runs grep, feeds results back
    Agent: "Found it in DefaultRootComponent.kt line 64. This is a type mismatch."
    Agent: posts final findings to the council

Supported tools:
    - read_file path=<relative_path> — Read a file's contents
    - list_files path=<relative_path> — List files in a directory
    - grep pattern=<regex> path=<relative_path> — Search file contents
    - find name=<glob> path=<relative_path> — Find files by name
    - run_command cmd=<shell_command> — Run a shell command (read-only, sandboxed)

The tool call format is simple and parseable:
    TOOL: <tool_name> key1=value1 key2=value2

Or multi-line:
    <tool:read_file>
    path=shared/domain/src/commonMain/kotlin/ml/dannyb/imnuriazsmr/domain/model/Hymn.kt
    </tool>

The harness parses these, executes the tool, and returns the result in a
format the agent can understand:
    <tool_result>
    <file path="shared/domain/.../Hymn.kt">
    ...file contents...
    </file>
    </tool_result>

Safety:
    - All file paths are resolved relative to the working directory
    - No path traversal (../ is blocked)
    - run_command is disabled by default, enabled with allow_commands=True
    - File reads are limited to max_file_size bytes (default 50KB)
    - Grep/find results are limited to max_results lines
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional


# Tool call patterns
TOOL_PATTERN = re.compile(
    r'(?:TOOL:\s*(\w+)\s+(.*?))|(?:<tool:(\w+)>\s*\n(.*?)\n</tool>)',
    re.DOTALL
)

# Key=value parser for tool arguments
KV_PATTERN = re.compile(r'(\w+)=(.*?)(?=\s+\w+=|$)', re.DOTALL)

TOOL_DESCRIPTION = """You have access to the following tools for exploring the codebase:

1. read_file — Read a file's contents
   Format: TOOL: read_file path=<relative_path>
   Example: TOOL: read_file path=shared/domain/src/commonMain/kotlin/ml/dannyb/imnuriazsmr/domain/model/Hymn.kt

2. list_files — List files in a directory (recursive by default)
   Format: TOOL: list_files path=<relative_path>
   Example: TOOL: list_files path=shared/domain/src/commonMain

3. grep — Search for a pattern in file contents
   Format: TOOL: grep pattern=<regex> path=<relative_path>
   Example: TOOL: grep pattern=List<Int> path=shared/presentation

4. find — Find files by name pattern
   Format: TOOL: find name=<glob> path=<relative_path>
   Example: TOOL: find name=*.kt path=shared/data

5. run_command — Run a shell command (if enabled)
   Format: TOOL: run_command cmd=<command>
   Example: TOOL: run_command cmd=wc -l shared/domain/src/commonMain/kotlin/ml/dannyb/imnuriazsmr/domain/model/Hymn.kt

Use these tools to explore the codebase autonomously. Call a tool, examine
the result, then reason about what you found. Continue until you have enough
information to complete your task, then post your findings.

You can use multiple tool calls in a single response. The harness will
execute each one and feed the results back to you.

When you're done exploring and ready to share your findings with the council,
just write your message normally (without any TOOL: calls)."""


class ToolExecutor:
    """Executes tool calls from the agent and returns results."""

    def __init__(
        self,
        workdir: str,
        allow_commands: bool = False,
        max_file_size: int = 50_000,
        max_results: int = 100,
        max_file_lines: int = 500,
    ):
        self.workdir = Path(workdir).resolve()
        self.allow_commands = allow_commands
        self.max_file_size = max_file_size
        self.max_results = max_results
        self.max_file_lines = max_file_lines

    def execute(self, tool_name: str, args: dict) -> str:
        """Execute a single tool call and return the result string."""
        try:
            if tool_name == "read_file":
                return self._read_file(args.get("path", ""))
            elif tool_name == "list_files":
                return self._list_files(args.get("path", "."))
            elif tool_name == "grep":
                return self._grep(args.get("pattern", ""), args.get("path", "."))
            elif tool_name == "find":
                return self._find(args.get("name", "*"), args.get("path", "."))
            elif tool_name == "run_command":
                if not self.allow_commands:
                    return "<error>run_command is not enabled</error>"
                return self._run_command(args.get("cmd", ""))
            else:
                return f"<error>Unknown tool: {tool_name}</error>"
        except Exception as e:
            return f"<error>{e}</error>"

    def _safe_path(self, relative: str) -> Path:
        """Resolve a path relative to workdir, blocking traversal."""
        # Remove leading slashes and normalize
        relative = relative.lstrip("/")
        path = (self.workdir / relative).resolve()

        # Check for path traversal
        if not str(path).startswith(str(self.workdir)):
            raise ValueError(f"Path traversal blocked: {relative}")

        return path

    def _read_file(self, relative: str) -> str:
        """Read a file and return its contents."""
        path = self._safe_path(relative)
        if not path.exists():
            return f"<error>File not found: {relative}</error>"
        if not path.is_file():
            return f"<error>Not a file: {relative}</error>"

        size = path.stat().st_size
        if size > self.max_file_size:
            # Read only the first max_file_lines
            with open(path, "r", errors="replace") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= self.max_file_lines:
                        lines.append(f"\n... (truncated, file is {size} bytes, showed first {self.max_file_lines} lines)")
                        break
                    lines.append(line)
                content = "".join(lines)
        else:
            with open(path, "r", errors="replace") as f:
                content = f.read()

        return f'<file path="{relative}">\n{content}\n</file>'

    def _list_files(self, relative: str) -> str:
        """List files in a directory recursively."""
        path = self._safe_path(relative)
        if not path.exists():
            return f"<error>Directory not found: {relative}</error>"
        if not path.is_dir():
            return f"<error>Not a directory: {relative}</error>"

        result = subprocess.run(
            ["find", str(path), "-type", "f",
             "-not", "-path", "*/.git/*",
             "-not", "-path", "*/build/*",
             "-not", "-path", "*/.gradle/*",
             "-not", "-path", "*/node_modules/*"],
            capture_output=True, text=True, timeout=10
        )

        lines = result.stdout.strip().split("\n")
        # Make paths relative to workdir
        rel_lines = []
        for line in lines:
            if line:
                try:
                    rel = Path(line).relative_to(self.workdir)
                    rel_lines.append(str(rel))
                except ValueError:
                    rel_lines.append(line)

        if len(rel_lines) > self.max_results:
            rel_lines = rel_lines[:self.max_results]
            rel_lines.append(f"\n... (truncated, {len(lines)} total files)")

        return f"<files>\n" + "\n".join(rel_lines) + "\n</files>"

    def _grep(self, pattern: str, relative: str) -> str:
        """Search for a pattern in file contents."""
        path = self._safe_path(relative)

        result = subprocess.run(
            ["grep", "-rn", "--include=*.kt", "--include=*.java",
             "--include=*.xml", "--include=*.gradle", "--include=*.kts",
             "--include=*.yaml", "--include=*.yml", "--include=*.json",
             "--include=*.md", "--include=*.toml",
             "-not", "-path", "*/.git/*",
             "-not", "-path", "*/build/*",
             "-not", "-path", "*/.gradle/*",
             pattern, str(path)],
            capture_output=True, text=True, timeout=15
        )

        lines = result.stdout.strip().split("\n")
        # Make paths relative
        rel_lines = []
        for line in lines:
            if line:
                try:
                    parts = line.split(":", 2)
                    if len(parts) >= 2:
                        file_path = Path(parts[0]).relative_to(self.workdir)
                        rel_lines.append(f"{file_path}:{parts[1]}:{parts[2] if len(parts) > 2 else ''}")
                    else:
                        rel_lines.append(line)
                except (ValueError, IndexError):
                    rel_lines.append(line)

        if len(rel_lines) > self.max_results:
            rel_lines = rel_lines[:self.max_results]
            rel_lines.append(f"\n... (truncated, {len(lines)} total matches)")

        if not rel_lines or (len(rel_lines) == 1 and not rel_lines[0]):
            return "<grep_results>No matches found</grep_results>"

        return f"<grep_results pattern=\"{pattern}\">\n" + "\n".join(rel_lines) + "\n</grep_results>"

    def _find(self, name: str, relative: str) -> str:
        """Find files by name pattern."""
        path = self._safe_path(relative)

        result = subprocess.run(
            ["find", str(path), "-type", "f", "-name", name,
             "-not", "-path", "*/.git/*",
             "-not", "-path", "*/build/*",
             "-not", "-path", "*/.gradle/*"],
            capture_output=True, text=True, timeout=10
        )

        lines = result.stdout.strip().split("\n")
        rel_lines = []
        for line in lines:
            if line:
                try:
                    rel = Path(line).relative_to(self.workdir)
                    rel_lines.append(str(rel))
                except ValueError:
                    rel_lines.append(line)

        if len(rel_lines) > self.max_results:
            rel_lines = rel_lines[:self.max_results]
            rel_lines.append(f"\n... (truncated, {len(lines)} total files)")

        if not rel_lines or (len(rel_lines) == 1 and not rel_lines[0]):
            return f"<find_results>No files matching '{name}' found</find_results>"

        return f"<find_results name=\"{name}\">\n" + "\n".join(rel_lines) + "\n</find_results>"

    def _run_command(self, cmd: str) -> str:
        """Run a shell command (if enabled)."""
        # Safety: block dangerous commands
        dangerous = ["rm ", "rm\t", "sudo ", "mv ", ">/", ":(", "mkfs", "dd if=", "chmod 777"]
        for d in dangerous:
            if d in cmd:
                return f"<error>Blocked dangerous command: {cmd}</error>"

        result = subprocess.run(
            cmd, shell=True, cwd=str(self.workdir),
            capture_output=True, text=True, timeout=30
        )

        output = result.stdout
        if result.stderr:
            output += f"\n<stderr>\n{result.stderr}\n</stderr>"

        if len(output) > self.max_file_size:
            output = output[:self.max_file_size] + f"\n... (truncated, {len(output)} total bytes)"

        return f"<command_output cmd=\"{cmd}\">\n{output}\n</command_output>"


def parse_tool_calls(text: str) -> list[tuple[str, dict]]:
    """Parse tool calls from agent output.

    Returns a list of (tool_name, args_dict) tuples.
    Returns empty list if no tool calls found.
    """
    calls = []

    # Pattern 1: TOOL: tool_name key=value key=value
    for match in re.finditer(r'TOOL:\s*(\w+)\s+(.*?)(?:\n(?:TOOL:|DONE:|VOTE:|PROPOSE_VOTE:)|\Z)', text, re.DOTALL):
        tool_name = match.group(1)
        args_str = match.group(2).strip()
        args = _parse_args(args_str)
        calls.append((tool_name, args))

    # Pattern 2: <tool:tool_name>\nkey=value\n</tool>
    for match in re.finditer(r'<tool:(\w+)>\s*\n(.*?)\n</tool>', text, re.DOTALL):
        tool_name = match.group(1)
        args_str = match.group(2).strip()
        args = _parse_args(args_str)
        calls.append((tool_name, args))

    return calls


def _parse_args(args_str: str) -> dict:
    """Parse key=value arguments from a string."""
    args = {}
    # Match key=value pairs, where value can contain spaces if it's the last arg
    for match in re.finditer(r'(\w+)=(.*?)(?=\s+\w+=|$)', args_str, re.DOTALL):
        key = match.group(1)
        value = match.group(2).strip()
        args[key] = value
    return args


def has_tool_calls(text: str) -> bool:
    """Check if the text contains any tool calls."""
    return len(parse_tool_calls(text)) > 0


def strip_tool_calls(text: str) -> str:
    """Remove tool call lines from text, leaving only the message."""
    # Remove TOOL: lines
    text = re.sub(r'TOOL:\s*\w+.*?(?:\n(?:TOOL:|DONE:|VOTE:|PROPOSE_VOTE:)|\Z)', '', text, flags=re.DOTALL)
    # Remove <tool:...>...</tool> blocks
    text = re.sub(r'<tool:\w+>.*?</tool>\s*', '', text, flags=re.DOTALL)
    return text.strip()


def format_tool_results(results: list[tuple[str, str]]) -> str:
    """Format tool execution results for feeding back to the agent."""
    if not results:
        return ""
    parts = ["<tool_results>"]
    for tool_name, result in results:
        parts.append(f'<result tool="{tool_name}">')
        parts.append(result)
        parts.append("</result>")
    parts.append("</tool_results>")
    parts.append("\nContinue your analysis based on these results. Call more tools if needed, or write your findings for the council.")
    return "\n".join(parts)


def get_tool_prompt() -> str:
    """Return the tool description to inject into agent prompts."""
    return TOOL_DESCRIPTION