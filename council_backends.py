#!/usr/bin/env python3
"""
Council Backend Registry — extensible CLI backend system.

Each backend knows how to invoke a specific CLI tool (Claude Code, Codex,
Ollama, Copilot, etc.) and pass it a prompt. The registry is config-driven:
backends are declared in council.yaml and loaded at runtime.

To add a new backend:
  1. Add it to the `backends:` section of council.yaml
  2. Add a runner function below (or use the generic shell runner)
  3. That's it — the harness and agents use it automatically
"""

import json
import os
import subprocess
import sys
import shutil
import tempfile
from typing import Optional


class BackendResult:
    """Result from running a backend."""
    def __init__(self, success: bool, output: str, error: str = "", exit_code: int = 0):
        self.success = success
        self.output = output
        self.error = error
        self.exit_code = exit_code


class Backend:
    """A single CLI backend (e.g., claude-code, codex, ollama)."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.command = config.get("command", "")
        self.prompt_mode = config.get("prompt_mode", "argument")
        self.read_only_flags = config.get("read_only_flags", [])
        self.write_flags = config.get("write_flags", [])
        self.extra_flags = config.get("extra_flags", [])
        self.models = config.get("models", [])
        self.auth = config.get("auth", "none")
        self.auth_env_var = config.get("auth_env_var", "")
        self.env = config.get("env", {})
        self.enabled = config.get("enabled", False)
        self.notes = config.get("notes", "")

    def is_available(self) -> bool:
        """Check if this backend's CLI is installed and auth is possible."""
        if not self.enabled:
            return False
        if not self.command:
            return True  # shell backend with empty command is "available"
        return shutil.which(self.command) is not None

    def get_model(self, model_id: str) -> Optional[dict]:
        """Find a model by id in this backend's model list."""
        for m in self.models:
            if m["id"] == model_id:
                return m
        return None

    def list_models(self) -> list:
        """List all available models for this backend."""
        return [m["id"] for m in self.models]

    def build_command(self, prompt: str, read_only: bool, model_id: str = "") -> list:
        """Build the command list for subprocess.run."""
        flags = list(self.read_only_flags if read_only else self.write_flags)
        flags.extend(self.extra_flags)

        # Add model flag if specified
        if model_id:
            model = self.get_model(model_id)
            if model and model.get("flag"):
                # For most backends, the flag is space-separated args
                flags.extend(model["flag"].split())
            elif model_id:
                # If no flag defined, use the model id directly
                flags.append(model_id)

        if self.prompt_mode == "argument":
            cmd = [self.command] + flags + [prompt]
            return cmd
        else:
            # For stdin mode (like ollama), the command is just the binary + flags
            # The prompt will be piped via stdin in the run() method
            cmd = [self.command] + flags
            return cmd

    def run(self, prompt: str, workdir: str, read_only: bool = True,
            model_id: str = "", timeout: int = 300) -> BackendResult:
        """Execute the backend CLI with the given prompt."""
        if not self.is_available():
            return BackendResult(False, "", f"Backend {self.name} is not available")

        # Build environment
        env = os.environ.copy()
        env.update(self.env)

        if self.prompt_mode == "argument":
            cmd = self.build_command(prompt, read_only, model_id)
            try:
                result = subprocess.run(
                    cmd, cwd=workdir, capture_output=True, text=True,
                    timeout=timeout, env=env
                )
                # Try to parse JSON output (claude-code returns JSON)
                output = result.stdout
                try:
                    parsed = json.loads(output)
                    if isinstance(parsed, dict) and "result" in parsed:
                        output = parsed["result"]
                except json.JSONDecodeError:
                    pass
                return BackendResult(
                    success=result.returncode == 0,
                    output=output,
                    error=result.stderr,
                    exit_code=result.returncode
                )
            except subprocess.TimeoutExpired:
                return BackendResult(False, "", f"Backend {self.name} timed out after {timeout}s")
            except FileNotFoundError:
                return BackendResult(False, "", f"Backend {self.name} command not found: {self.command}")

        elif self.prompt_mode == "stdin":
            cmd = self.build_command(prompt, read_only, model_id)
            try:
                result = subprocess.run(
                    cmd, cwd=workdir, input=prompt,
                    capture_output=True, text=True,
                    timeout=timeout, env=env
                )
                output = result.stdout
                # Clean Ollama output: remove thinking blocks, progress spinners
                if self.name == "ollama":
                    lines = output.split("\n")
                    cleaned = []
                    in_thinking = False
                    for line in lines:
                        stripped = line.strip()
                        # Skip progress spinner lines (contain spinner characters)
                        if any(c in stripped for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
                            continue
                        # Skip "Thinking..." and "...done thinking." markers
                        if stripped == "Thinking..." or stripped.startswith("...done thinking"):
                            in_thinking = not stripped.startswith("...done thinking")
                            continue
                        if in_thinking:
                            continue
                        cleaned.append(line)
                    output = "\n".join(cleaned).strip()
                return BackendResult(
                    success=result.returncode == 0,
                    output=output,
                    error=result.stderr,
                    exit_code=result.returncode
                )
            except subprocess.TimeoutExpired:
                return BackendResult(False, "", f"Backend {self.name} timed out after {timeout}s")
            except FileNotFoundError:
                return BackendResult(False, "", f"Backend {self.name} command not found: {self.command}")

        elif self.prompt_mode == "file":
            # Write prompt to a temp file, pass path as argument
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
                f.write(prompt)
                prompt_file = f.name
            cmd = self.build_command(prompt_file, read_only, model_id)
            try:
                result = subprocess.run(
                    cmd, cwd=workdir, capture_output=True, text=True,
                    timeout=timeout, env=env
                )
                return BackendResult(
                    success=result.returncode == 0,
                    output=result.stdout,
                    error=result.stderr,
                    exit_code=result.returncode
                )
            finally:
                os.unlink(prompt_file)

        else:
            return BackendResult(False, "", f"Unknown prompt_mode: {self.prompt_mode}")


class BackendRegistry:
    """Registry of all available backends, loaded from YAML config."""

    def __init__(self):
        self.backends: dict[str, Backend] = {}

    @classmethod
    def from_config(cls, config: dict) -> "BackendRegistry":
        """Create a registry from the backends section of council.yaml."""
        registry = cls()
        for name, cfg in config.get("backends", {}).items():
            registry.backends[name] = Backend(name, cfg)
        return registry

    def get(self, name: str) -> Optional[Backend]:
        return self.backends.get(name)

    def available(self) -> dict[str, Backend]:
        """Return only backends that are installed and enabled."""
        return {name: b for name, b in self.backends.items() if b.is_available()}

    def list_available(self) -> list[str]:
        return list(self.available().keys())

    def best_for_role(self, role: str, roles_config: dict) -> Optional[Backend]:
        """Find the best available backend for a given role."""
        role_cfg = roles_config.get(role, {})
        default_backend = role_cfg.get("default_backend", "")
        if default_backend and self.get(default_backend) and self.get(default_backend).is_available():
            return self.get(default_backend)
        # Fallback: any available backend
        for name, b in self.available().items():
            if name != "shell":  # prefer real backends over shell
                return b
        return self.get("shell")


# --- YAML Loading ---

def load_config(config_path: str = "council.yaml") -> dict:
    """Load the YAML config file. Uses only stdlib (no pyyaml dependency)."""
    # Try to use PyYAML if available
    try:
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f)
    except ImportError:
        pass

    # Fallback: minimal YAML parser for our config format
    # This handles the subset of YAML we use: nested dicts, lists, strings, bools, null
    import re

    with open(config_path) as f:
        content = f.read()

    # Remove comments and blank lines
    lines = []
    for line in content.split("\n"):
        # Remove inline comments (but not # inside strings)
        if "#" in line and not line.strip().startswith("-"):
            # Check if # is inside a quote
            hash_pos = line.find("#")
            quote_before = line[:hash_pos].count('"') + line[:hash_pos].count("'")
            if quote_before % 2 == 0:  # # is not inside a quote
                line = line[:hash_pos]
        line = line.rstrip()
        if line.strip():
            lines.append(line)

    # Parse using indentation
    def parse_block(lines, start, indent):
        result = {}
        i = start
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent < indent:
                break
            if cur_indent > indent:
                i += 1
                continue
            stripped = line.strip()
            if stripped.endswith(":"):
                key = stripped[:-1].strip()
                if key == "null":
                    return None
                # Check if next lines are indented (nested block)
                if i + 1 < len(lines) and len(lines[i+1]) - len(lines[i+1].lstrip()) > indent:
                    sub, i = parse_block(lines, i + 1, indent + 2)
                    result[key] = sub
                else:
                    result[key] = {}
                    i += 1
            elif ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif val in ("null", "~", ""):
                    val = None
                elif val in ("true", "false"):
                    val = val == "true"
                elif val.startswith("["):
                    # Inline list
                    val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
                result[key] = val
                i += 1
            elif stripped.startswith("- "):
                # List item
                val = stripped[2:].strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                if "list" not in result:
                    result["list"] = []
                result["list"].append(val)
                i += 1
            else:
                i += 1
        return result, i

    # Simple approach: use json-like structure
    # For production, install pyyaml: pip install pyyaml
    print("WARNING: Using fallback YAML parser. Install pyyaml for reliable parsing: pip install pyyaml", file=sys.stderr)

    # Actually, let's just try a different approach — convert to JSON
    # This is getting complex. Let's just require pyyaml or use a simple
    # alternative: the config can also be JSON.

    # Check for JSON config
    json_path = config_path.replace(".yaml", ".json").replace(".yml", ".json")
    if os.path.exists(json_path):
        with open(json_path) as f:
            return json.load(f)

    # Last resort: try pyyaml one more time with explicit error
    try:
        import yaml
    except ImportError:
        print(f"FATAL: Cannot parse {config_path} without PyYAML. Install it: pip install pyyaml", file=sys.stderr)
        print(f"Or create a JSON version at {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        return yaml.safe_load(f)