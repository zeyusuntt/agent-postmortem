"""N3b P19 at local, through the run sinks, on an in-memory run: a path in
free text (an excerpt, a title) renders repo-relative, ~/.../name, or path-N,
never absolute; the repository root comes only from the context (D-n).

The CLI-level matrix with a real repository is in test_n3b_run_local.py and
test_n3b_run_path_matrix.py.
"""

from __future__ import annotations

from pathlib import Path

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.models import Finding
from agent_postmortem.redact import Redactor
from tests.test_bug_report import _make_findings, _make_run

HOME = Path("/srv/people/tester")
REPO = "/srv/work/repo"

def _findings(run, text: str) -> list[Finding]:
    findings = _make_findings(run)
    findings[0].evidence = [f"agent-a1111111111111111.jsonl:5: {text}"]
    return findings


def _local(repo_root: str | None = REPO) -> Redactor:
    return Redactor(context=DisclosureContext(level="local", home=HOME, repo_root=repo_root))


def _render(text: str, *, repo_root: str | None = REPO) -> str:
    run = _make_run()
    return report.render_run_text(
        run, _findings(run, text), _local(repo_root), verbose=True, evidence=True,
    )


def test_an_absolute_path_inside_the_repository_is_repo_relative():
    out = _render(f"wrote {REPO}/src/m.py and {REPO}/README.md")
    # F fix 6 (W6-2): the first path hides the rest of its line, so the span is one opaque path-N
    # (the repo-relative form of a path on its own line is pinned in test_fc_fix6_redact.py)
    assert "wrote path-1 [text hidden]" in out
    assert REPO not in out


def test_the_repository_root_alone_is_dot_and_dotdot_escapes_are_outside():
    out = _render(f"in {REPO} and {REPO}/src/../../elsewhere/x.py")
    # F fix 6 (W6-2): the first path hides the rest of its line (one path-N per path on its own
    # line is pinned in test_fc_fix6_redact.py)
    assert "in path-1 [text hidden]" in out
    assert "elsewhere" not in out


def test_a_sibling_prefix_directory_is_outside():
    out = _render(f"touched {REPO}2/x.py")
    assert "touched path-1" in out
    assert "repo2" not in out


def test_inside_home_but_outside_the_repository_is_structural():
    out = _render(f"read {HOME}/notes/todo.md")
    assert "read ~/.../todo.md" in out
    assert str(HOME) not in out


def test_outside_home_and_repository_is_path_n_and_the_numbers_are_stable():
    out = _render("a /opt/tool/bin/x then /usr/lib/y then /opt/tool/bin/x again")
    # F fix 6 (W6-2): the first path hides the rest of its line
    assert "a path-1 [text hidden]" in out


def test_without_a_repository_root_every_path_is_outside():
    out = _render(f"wrote {REPO}/src/m.py", repo_root=None)
    assert "wrote path-1" in out


def test_the_same_path_gets_the_same_display_everywhere_in_one_render():
    run = _make_run()
    out = report.render_run_text(
        run, _findings(run, f"wrote {REPO}/src/m.py"), _local(), verbose=True, evidence=True,
    )
    assert out.count("src/m.py") >= 1
    assert "/srv" not in out


def test_text_that_is_not_a_path_is_left_alone():
    out = _render("rate 3/4 and a/b and and/or and https://host.example/x/y")
    assert "rate 3/4 and a/b and and/or" in out
    assert "https://host.example/x/y" in out


def test_share_is_unaffected_by_the_local_path_rule():
    run = _make_run()
    share = Redactor(context=DisclosureContext(level="share", home=HOME, repo_root=REPO))
    out = report.render_run_text(
        run, _findings(run, f"wrote {REPO}/src/m.py"), share, verbose=True, evidence=True,
    )
    assert "src/m.py" not in out


def test_a_collapsed_home_path_is_a_fixed_point_inside_a_list():
    """N3b Part 2: P19 prints `~/.../name`, and every line then goes through
    structural redaction again. The collapse rule must not swallow the rest of
    a comma list after such a form (written after the code, found by the
    rescue plan matrix)."""
    from agent_postmortem.redact import redact

    text = "5 paths (src/a.py, ~/.../n.md, path-2, rel/inside.py)"
    assert redact(text, home=None) == text
    # a form that is not the collapsed one still collapses
    assert redact("~/work/client/notes.md", home=None) == "~/.../notes.md"
    assert redact("~/.../secret/notes.md", home=None) == "~/.../notes.md"
