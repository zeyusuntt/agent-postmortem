## What this changes and why

<!-- One or two sentences. Verdict first: what did not work before, what does now. -->

## Checklist

- [ ] Tests were written first (a failing test, then the code that makes it pass), and `uv run pytest` is green.
- [ ] `uv run ruff check .` is clean.
- [ ] Privacy check passes: `uv run pytest tests/test_privacy.py` (and, if this touches git history or ref names, `scripts/check-privacy.sh`).
- [ ] `docs/DESIGN.md` was updated in this same PR if the code departs from what it currently says.
- [ ] Any fixture added or changed under `tests/fixtures/` or `src/agent_postmortem/demo/` is fully synthetic (fake session id, fake paths, no real prompt text).
