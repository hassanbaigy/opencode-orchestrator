#!/usr/bin/env bash
# One-step install of opencode-orchestrator into Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/hassanbaigy/opencode-orchestrator/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --scope project --with-sandbox
#
# Options:
#   --scope user|project|local   where Claude Code records the plugin (default: user)
#   --with-sandbox               also install the OS sandbox (npm i -g @anthropic-ai/sandbox-runtime)
#   --dry-run                    print what would run without changing anything
set -euo pipefail

REPO="hassanbaigy/opencode-orchestrator"
PLUGIN="opencode-orchestrator@opencode-orchestrator"
SCOPE="user"
WITH_SANDBOX=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --scope) SCOPE="${2:?--scope needs user, project or local}"; shift 2 ;;
    --with-sandbox) WITH_SANDBOX=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,11p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$SCOPE" in user|project|local) ;; *) echo "--scope must be user, project or local" >&2; exit 2 ;; esac

say() { printf '%s\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi
}
have() { command -v "$1" >/dev/null 2>&1; }

say "opencode-orchestrator installer (scope: $SCOPE)"
say ""
say "Checking prerequisites"

if have claude; then ok "Claude Code $(claude --version 2>/dev/null | head -1)"; else
  say "  ✗ Claude Code CLI not found. Install it first: https://claude.com/claude-code"; exit 1
fi

if have python3 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  ok "Python $(python3 -c 'import platform; print(platform.python_version())')"
else
  say "  ✗ Python 3.9+ is required for the bundled tools (python3 on PATH)."; exit 1
fi

if have opencode; then ok "OpenCode $(opencode --version 2>/dev/null | tail -1)"; else
  warn "OpenCode not found. Install it before running agents: npm install -g opencode-ai (https://opencode.ai)"
fi

if have git; then ok "git"; else warn "git not found; runs need a Git repository."; fi

if have srt; then ok "OS sandbox (srt $(srt --version 2>/dev/null | head -1))"
elif [ "$WITH_SANDBOX" = 1 ]; then
  if have npm; then
    say "  … installing the OS sandbox (@anthropic-ai/sandbox-runtime)"
    run npm install -g @anthropic-ai/sandbox-runtime
  else
    warn "npm not found; cannot install the OS sandbox. Agents will run with policy-only protection."
  fi
else
  warn "OS sandbox not installed (recommended): rerun with --with-sandbox, or npm install -g @anthropic-ai/sandbox-runtime"
fi

say ""
say "Installing the plugin"
if claude plugin marketplace list 2>/dev/null | grep -q "opencode-orchestrator"; then
  ok "marketplace already known; updating it"
  run claude plugin marketplace update opencode-orchestrator
else
  run claude plugin marketplace add "$REPO" --scope "$SCOPE"
fi
run claude plugin install "$PLUGIN" --scope "$SCOPE"

say ""
say "Done. Restart Claude Code (or run /reload-plugins), then try:"
say "  /opencode-orchestrator:workflow   plan, delegate to OpenCode agents, verify, report"
say "  /opencode-orchestrator:orchestrate one focused agent cycle"
