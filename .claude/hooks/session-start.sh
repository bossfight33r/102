#!/bin/bash
# SessionStart: в облачных сессиях Claude Code ставит зависимости, чтобы сразу работали make test / make lint.
set -euo pipefail
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi
cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"
command -v uv >/dev/null || pip install --quiet uv
uv sync --quiet
