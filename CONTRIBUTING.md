# Contributing to agent-postmortem

Read `docs/DESIGN.md` first. It is the binding contract for this project.
If code and DESIGN.md disagree, DESIGN.md wins until it is changed in the
same commit as the code that departs from it.

## Dev setup

```
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Runtime code under `src/agent_postmortem/` imports only the Python 3.11
standard library; no third-party runtime dependency, ever. Dev-only tools
(`pytest`, `ruff`) live in `[project.optional-dependencies].dev`.

## Test first

Write the failing test before the code that makes it pass. Run only the
test file(s) you are iterating on:

```
uv run pytest tests/test_x.py -q
```

Run the full suite before opening a pull request:

```
uv run pytest
```

Never weaken or delete an existing test to make it pass. If DESIGN.md
changes a contract an existing test encodes, update the test to the new
contract and say so in the pull request description.

Every detector needs: a fixture that triggers it, a fixture that does not,
and an evidence line in `path:line` form pointing into the fixture. See
`tests/fixtures/README.md` for the fixture format.

## Privacy check before every push

This repo is read-only over the data it inspects and redacts by default.
`tests/test_privacy.py` runs as part of the normal test suite and scans
every tracked file for real data (a real UUID, an account path, an email
outside `example.com`, an unlisted `wf_` id). Before pushing, also run the
same rules over git history and the current branch's ref names, which the
test suite does not cover:

```
scripts/check-privacy.sh
```

Fixtures under `tests/fixtures/` and `src/agent_postmortem/demo/` must stay
fully synthetic: no real session id, no real absolute path outside `/tmp`
or `~`, no real project slug, no real prompt text, no real code.

## Linting

```
uv run ruff check .
```

Fix what ruff flags rather than disabling a rule. `[tool.ruff]` in
`pyproject.toml` is the source of truth for line length and selected rule
sets.

## Releasing (maintainers)

Releases are cut by pushing a `v*` tag, which `.github/workflows/release.yml`
picks up: it runs the tests, builds the wheel and sdist, runs the privacy
check against them, and makes the GitHub release with the built files and
that version's CHANGELOG section. Users install from that tag
(`uv tool install git+https://github.com/zeyusuntt/agent-postmortem@v<version>`),
and so does the plugin.

Publishing to PyPI (via a trusted publisher, no API token stored in this
repository) is switched off until the repository variable `PUBLISH_PYPI` is
`true`; without it the publish job is skipped, not failed. To turn it on once
the setup below is done: set the variable (Settings, Secrets and variables,
Actions, Variables, `PUBLISH_PYPI` = `true`), then re-run the release workflow
for the existing tag (Actions, Release, the tag's run, Re-run all jobs), and
check that installing the `agent-postmortem` package from PyPI gives that
version. After that,
switch the install lines in README.md, plugin/README.md, the postmortem skill
and plugin/hooks/run.sh back to PyPI in one release (tests/test_launch_docs.py
and tests/test_plugin.py pin them).

One-time trusted-publisher setup on pypi.org, before the first tagged
release:

1. Create the project on PyPI (or reserve the name via a first manual
   upload) so a project page exists to attach the publisher to.
2. On the project's PyPI page, go to Publishing, and add a new trusted
   publisher: GitHub, this repository, workflow file `release.yml`,
   environment name `pypi`.
3. In this repository's GitHub settings, create an environment named
   `pypi` (Settings, Environments). No secrets are needed there; the OIDC
   token GitHub Actions presents is what PyPI trusts.

TestPyPI dry run before a real release (recommended for the first release
and for any change to the release workflow):

```
uv build
uv publish --index testpypi --token "$TESTPYPI_TOKEN"
```

replacing `testpypi` with whatever index name you have configured for
`https://test.pypi.org/legacy/` in `uv`'s configuration, or pass
`--publish-url https://test.pypi.org/legacy/` directly.
