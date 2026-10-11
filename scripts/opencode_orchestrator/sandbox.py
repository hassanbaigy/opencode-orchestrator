"""OS sandbox for headless OpenCode via Anthropic's sandbox-runtime (``srt``).

``srt`` wraps a process in Seatbelt (macOS) or bubblewrap (Linux) and routes its network through a
host-side proxy that only allows listed domains. The launcher generates one ``sandbox.json`` per
execution:

- reads: allowed, except credential stores and ``.env`` files;
- writes: only the worktree (not for ``readonly``), its Git metadata, OpenCode's data and cache
  directories, tool caches, and temp directories; never the orchestration run directory;
- network: only the model provider's API and the model catalog, plus package registries with
  ``--allow-network`` and any ``--sandbox-allow-domain``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

PROVIDER_DOMAINS: Dict[str, List[str]] = {
    "opencode": ["opencode.ai", "*.opencode.ai"],
    "openrouter": ["openrouter.ai"],
    "anthropic": ["api.anthropic.com"],
    "openai": ["api.openai.com"],
    "deepseek": ["api.deepseek.com"],
    "google": ["generativelanguage.googleapis.com"],
    "groq": ["api.groq.com"],
    "mistral": ["api.mistral.ai"],
    "xai": ["api.x.ai"],
    "moonshotai": ["api.moonshot.ai"],
    "zai": ["api.z.ai"],
    "zhipuai": ["open.bigmodel.cn"],
    "together": ["api.together.xyz"],
    "fireworks": ["api.fireworks.ai"],
    "cerebras": ["api.cerebras.ai"],
}
# OpenCode refreshes its model catalog from models.dev.
BASE_DOMAINS = ["models.dev"]
PACKAGE_DOMAINS = [
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
]
SECRET_READ_DENY = [
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker",
    "~/.gnupg",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.config/gh",
    "~/.infisical",
    "~/.claude",
    "~/.codex",
    "~/Library/Keychains",
    "**/.env",
    "**/.env.*",
]
# Committed templates stay readable: pytest collection and git diff stat them, and a real monorepo run
# failed before collection when they were denied. allowRead beats the broader **/.env.* deny.
SECRET_READ_ALLOW = ["**/.env.example", "**/.env.sample", "**/.env.template", "**/.env.dist"]
OPENCODE_WRITE = ["~/.local/share/opencode", "~/.local/state/opencode", "~/.cache/opencode"]
TOOL_CACHE_WRITE = [
    "~/.cache",
    "~/Library/Caches",
    "~/.npm",
    "~/.local/share/pnpm",
    "~/Library/pnpm",
    "~/.bun/install/cache",
]
TEMP_WRITE = ["/tmp", "/private/tmp", "/private/var/folders"]

# Tool-output fragments that indicate the OS sandbox blocked an action.
BLOCK_MARKERS = ("Operation not permitted", "Tunnel connection failed: 403", "sandbox-exec")


def srt_binary() -> Optional[str]:
    return shutil.which("srt")


def srt_version(binary: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def git_path(worktree: Path, flag: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", flag],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def configured_model(worktree: Path, env: Dict[str, str]) -> Optional[str]:
    """The model OpenCode would use without ``--model`` (best effort; JSON configs only)."""
    candidates: List[object] = []
    content = env.get("OPENCODE_CONFIG_CONTENT")
    if content:
        try:
            candidates.append(json.loads(content))
        except ValueError:
            pass
    for path in (worktree / "opencode.json", Path("~/.config/opencode/opencode.json").expanduser()):
        try:
            candidates.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    for config in candidates:
        if isinstance(config, dict) and isinstance(config.get("model"), str):
            return config["model"]
    return None


def provider_domains(model: Optional[str]) -> List[str]:
    if not model or "/" not in model:
        raise ValueError("cannot determine the model provider; pass --model provider/model")
    provider = model.split("/", 1)[0]
    if provider not in PROVIDER_DOMAINS:
        raise ValueError(
            f"no known API domain for provider {provider!r}; "
            "pass --sandbox-allow-domain <host> for it"
        )
    return list(PROVIDER_DOMAINS[provider])


def build_settings(
    *,
    profile: str,
    worktree: Path,
    run_root: Optional[Path],
    model: Optional[str],
    allow_network: bool,
    extra_domains: List[str],
    extra_write: List[str],
    skip_provider_lookup: bool = False,
) -> Dict[str, object]:
    domains: List[str] = list(BASE_DOMAINS)
    if not skip_provider_lookup:
        domains += provider_domains(model)
    if allow_network:
        domains += PACKAGE_DOMAINS
    domains += extra_domains

    write: List[str] = []
    if profile != "readonly":
        write.append(str(worktree))
    # Git metadata: the worktree's own git dir (index, HEAD) always; the shared object store and
    # refs only when the agent may commit.
    git_dir = git_path(worktree, "--git-dir")
    if git_dir:
        write.append(git_dir)
    if profile == "implement":
        common = git_path(worktree, "--git-common-dir")
        if common:
            write.append(common)
    write += OPENCODE_WRITE + TOOL_CACHE_WRITE + TEMP_WRITE
    temp = os.path.realpath(tempfile.gettempdir())
    if temp not in write:
        write.append(temp)
    write += extra_write

    deny_write: List[str] = []
    if profile == "readonly":
        # denyWrite beats allowWrite, so this holds even when the worktree sits under a temp dir.
        deny_write.append(str(worktree))
    if run_root is not None:
        # The journal, prompts and evidence are Claude's; no agent may rewrite them.
        deny_write.append(str(run_root))

    return {
        "network": {"allowedDomains": dedupe(domains), "deniedDomains": []},
        "filesystem": {
            "denyRead": list(SECRET_READ_DENY),
            "allowRead": list(SECRET_READ_ALLOW),
            "allowWrite": dedupe([expand(path) for path in write]),
            "denyWrite": dedupe(deny_write),
        },
    }


def expand(path: str) -> str:
    return os.path.expanduser(path) if path.startswith("~") else path


def dedupe(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def sandbox_supported() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def run_root_for(execution_dir: Path) -> Optional[Path]:
    """``.opencode-orchestrator/runs/<run-id>`` when the execution lives in the standard layout."""
    for parent in execution_dir.parents:
        if parent.parent.name == "runs" and parent.parent.parent.name == ".opencode-orchestrator":
            return parent
    return None


NODE_LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lockb", "bun.lock")
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", ".next", "__pycache__"}


def missing_dependencies(worktree: Path, max_depth: int = 3) -> List[str]:
    """Projects in the worktree whose dependencies are not installed.

    Agents cannot install them inside the sandbox: registries are not allowlisted by default, and
    srt always blocks writes to IDE/Git config paths (``.vscode/``, ``.idea/``, ``.git/hooks``)
    that many packages ship, so ``pnpm install`` fails with EPERM even with network allowed.
    """
    root_has_node_lock = any((worktree / name).exists() for name in NODE_LOCKFILES)
    warnings: List[str] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        relative = directory.relative_to(worktree).as_posix() or "."
        if (directory / "package.json").is_file():
            has_lock = root_has_node_lock or any(
                (directory / name).exists() for name in NODE_LOCKFILES
            )
            if has_lock and not (directory / "node_modules").is_dir() and not (
                worktree / "node_modules"
            ).is_dir():
                warnings.append(f"{relative}: node_modules missing")
        if (directory / "pyproject.toml").is_file() and (directory / "uv.lock").is_file():
            if not (directory / ".venv").is_dir():
                warnings.append(f"{relative}: .venv missing (uv sync)")
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
                walk(child, depth + 1)

    walk(worktree, 0)
    return warnings
