# agent-postmortem: rules for anyone (human or agent) working in this repo

Read `docs/DESIGN.md` first. It is the binding contract. If code and DESIGN.md
disagree, DESIGN.md wins until it is changed in the same commit.

## Non-negotiables
- Runtime code under `src/agent_postmortem/` imports only the Python 3.11
  standard library. No third-party runtime dependency, ever. Dev deps live in
  `[project.optional-dependencies].dev`.
- The tool is read-only over the data it inspects. It never writes inside a
  run directory, never deletes, never modifies transcripts.
- Redaction is on by default. Anything printed or written by default must pass
  `redact()`. `--no-redact` is the only way to see raw paths and session ids.
- Test fixtures under `tests/fixtures/` are synthetic. They may copy the exact
  marker strings the runtime emits (for example the interrupt sentence), but
  never a real session UUID, a real absolute path outside `/tmp` or `~`, a real
  project slug, real prompt text, or real code. `tests/test_privacy.py`
  enforces this and must stay green.
- Every detector has: a fixture that triggers it, a fixture that does not, and
  an evidence line in `path:line` form pointing into the fixture.
- Test first. Write the failing test, then the code. Run only the test files
  you touched while iterating (`uv run pytest tests/test_x.py`), the full suite
  before committing. Never weaken or delete a test to make it pass.
- Commit every green increment with a plain message. No attribution trailers,
  no mention of AI tools or code generation in git metadata.
- User-facing text (report, README, docs): plain English, no em-dashes, numbers
  over adjectives, verdict first. The first ten lines of any report must carry
  the verdict and the one number a reader would repeat.

## Layout
```
src/agent_postmortem/
  __init__.py
  cli.py          argparse entry: scan | run | rules | apply | trend | demo | hook
  models.py       dataclasses: Run, Agent, Attempt, Event, Finding
  reader.py       discover run dirs, parse journal / meta / transcripts
  redact.py       redact() and the rules it applies
  detectors/      one module per detector, registered in detectors/__init__.py
  report.py       text and JSON renderers, bug-report renderer
  rules.py        suggested-rule templates keyed by detector id
  state.py        locked, versioned state store
  trend.py        weekly and before/after aggregation
  hooks_cli.py    `hook` subcommands for the Claude Code plugin
  demo/           one canonical synthetic run shipped in the wheel
  hooks/          model_allocation_guard.mjs template
plugin/           the Claude Code plugin: hooks, /postmortem skill, marketplace.json
scripts/
  check-privacy.sh    repository privacy audit: tree, history, artifacts
  export-public.sh    sanitized export that becomes the public history
tests/
  fixtures/runs/<case>/   synthetic run dirs
  fixtures/README.md      field-level description of the formats
  privacy_rules.py        rule functions shared by test_privacy.py and check-privacy.sh
  synthetic-ids.txt       every synthetic wf_ id used in the tree
  test_privacy.py         repository-wide privacy audit (DESIGN.md 5.4)
  test_*.py
docs/DESIGN.md
```

## If you may be a retry after a killed attempt
First run `git status` and `git diff --stat`. If uncommitted work exists,
verify it (import the module, run the touched test files) and commit it before
continuing. Never `git checkout`, `git restore`, or `git stash` a file holding
uncommitted work.

## Machine quirk: "No module named agent_postmortem" under uv
Sandboxed shells on this Mac sometimes mark the venv's .pth files hidden, and
the editable install disappears. Fix, do not reinstall:
`chflags nohidden .venv/lib/python3.13/site-packages/*.pth`. Tests are immune
because pyproject sets `pythonpath = ["src"]`; the `postmortem` console script
is not, so run that command first if `uv run postmortem` fails to import.
The flag can come back after any `uv run` that rebuilds the package, so for
scripted checks prefer the invocation that never touches the .pth:
`PYTHONPATH=src uv run python -m agent_postmortem.cli <args>`.
