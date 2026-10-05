"""N9 release gate (docs/DESIGN.md 4.13.9, .reviews/v0.3/N9-plan.md V1).

Built by parsing tests/*.py with `ast`, so nothing under tests/ is imported
and no import side effect can hide a mark. It fails when:

- a field case that 4.13.9 names is missing from FIELD_NODES, the explicit map
  from each case to the exact pytest node ids of its tests;
- a mapped test is not collected, or does not PASS when pytest runs the mapped
  node ids in a subprocess (a skip, xfail, xpass, or deselect, spelled any way,
  including an alias or a module or class mark, is not a PASS);
- a field test carries an xfail or skip mark (decorator, module `pytestmark`,
  or a `pytest.skip`/`pytest.xfail` call in its body);
- any other test in tests/ carries an xfail or an unconditional skip mark
  (a conditional `skipif` that guards a missing tool, such as node, uv, or the
  private measurement script, is an environment guard and stays allowed
  outside the field tests), except the one
  strict xfail the owner's N7 decision holds open
  (`test_field_warm_scan_parses_zero_cached_transcripts`, strict, reason
  naming that decision).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

FIELD_CASES = (
    "field_shell_writes_invisible",
    "field_git_c_commit",
    "field_self_quote_backgrounded",
    "field_cat_backgrounded_still_counts",
    "field_workflow_failed_reported_clean",
    "field_failed_generation_completed_attempt",
    "field_resumed_run_unfinished",
    "field_resume_after_failure_possibly_running",
    "field_queue_only_notification",
    "field_quoted_notification_ignored",
    "field_conflicting_notifications",
    "field_parent_same_size_rewrite",
    "field_readonly_codex_denial",
    "field_session_brief_other_agents",
    "field_live_session_wording",
    "field_unknown_mcp_tool_partial",
    "field_first_agent_before_launch_result",
)

_V = "tests/test_field_v03.py::"
_FA = "tests/test_fa_generations.py::"

# case -> (levels note, exact node ids). DESIGN 4.13.9 (D14/D17): a model-level
# test and a public test where both apply; the note says which levels a case has
# and why a case has one.
FIELD_NODES: dict[str, tuple[str, tuple[str, ...]]] = {
    "field_shell_writes_invisible": (
        "model N4 + public N5",
        (_V + "test_field_shell_writes_invisible_model", _V + "test_field_shell_writes_invisible"),
    ),
    "field_git_c_commit": (
        "model N4 + public N4",
        (_V + "test_field_git_c_commit_model", _V + "test_field_git_c_commit"),
    ),
    "field_self_quote_backgrounded": (
        "model N1 + public N1 (reader and detector surface)",
        (_V + "test_field_self_quote_backgrounded_model", _V + "test_field_self_quote_backgrounded"),
    ),
    "field_cat_backgrounded_still_counts": (
        "detector level N1 + public N5 (schema-3 JSON finding and text)",
        (_V + "test_field_cat_backgrounded_still_counts", _V + "test_field_cat_backgrounded_still_counts_public"),
    ),
    "field_workflow_failed_reported_clean": (
        "model N2 + public N2",
        (_V + "test_field_workflow_failed_reported_clean_model", _V + "test_field_workflow_failed_reported_clean"),
    ),
    "field_failed_generation_completed_attempt": (
        "model N2 (lifecycle) + model N4 (ledger) + public N5",
        (
            _V + "test_field_failed_generation_completed_attempt_lifecycle",
            _V + "test_field_failed_generation_completed_attempt_ledger",
            _V + "test_field_failed_generation_completed_attempt",
        ),
    ),
    "field_resumed_run_unfinished": (
        "model N2 + public N2",
        (_V + "test_field_resumed_run_unfinished_model", _V + "test_field_resumed_run_unfinished"),
    ),
    "field_resume_after_failure_possibly_running": (
        "model N2 + public N2",
        (
            _V + "test_field_resume_after_failure_possibly_running_model",
            _V + "test_field_resume_after_failure_possibly_running",
        ),
    ),
    "field_queue_only_notification": (
        "model N1 + public N2",
        (_V + "test_field_queue_only_notification_model", _V + "test_field_queue_only_notification"),
    ),
    "field_quoted_notification_ignored": (
        "model N1 + public N1",
        (_V + "test_field_quoted_notification_ignored_model", _V + "test_field_quoted_notification_ignored"),
    ),
    "field_conflicting_notifications": (
        "model N2 + public N2",
        (_V + "test_field_conflicting_notifications_model", _V + "test_field_conflicting_notifications"),
    ),
    "field_parent_same_size_rewrite": (
        "N1 only (D17): accepted generations and cache invalidation, no public rendering",
        (_V + "test_field_parent_same_size_rewrite",),
    ),
    "field_readonly_codex_denial": (
        "public N5 only: no new model field, the finding shape is unchanged",
        (_V + "test_field_readonly_codex_denial",),
    ),
    "field_session_brief_other_agents": (
        "public N5 only: a rendering assertion through the CLI entry point (D15)",
        (_V + "test_field_session_brief_other_agents",),
    ),
    "field_live_session_wording": (
        "public N2 only: a rendering choice, no new model field",
        (_V + "test_field_live_session_wording",),
    ),
    "field_unknown_mcp_tool_partial": (
        "model N4 + public N5",
        (_V + "test_field_unknown_mcp_tool_partial_model", _V + "test_field_unknown_mcp_tool_partial"),
    ),
    "field_first_agent_before_launch_result": (
        "model FA2 + public FA2",
        (
            _FA + "test_field_first_agent_before_launch_result_model",
            _FA + "test_field_first_agent_before_launch_result",
        ),
    ),
}

HELD_TEST = "test_field_warm_scan_parses_zero_cached_transcripts"
HELD_FILE = "test_field_v03_more.py"
HELD_REASON_PHRASE = "N7 step 3 is deferred past v0.3 by the owner's decision C (N7-plan.md)"

_MARK_NAMES = {"xfail", "skip", "skipif"}


def _tests_in(tree: ast.AST):
    """Every test function (module level or in a class), by name."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node


def _marks(node) -> list[tuple[str, ast.AST]]:
    """(mark name, decorator expression) for every pytest mark on a function
    or class: `@pytest.mark.xfail(...)`, `@pytest.mark.skip`, `@pytest.mark.skipif(...)`."""
    found = []
    for deco in getattr(node, "decorator_list", []):
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr in _MARK_NAMES:
            found.append((target.attr, deco))
    return found


def _module_marks(tree: ast.Module) -> list[str]:
    """Names of marks assigned through a module-level `pytestmark`."""
    names = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Attribute) and sub.attr in _MARK_NAMES:
                    names.append(sub.attr)
    return names


def _body_skips(func) -> list[str]:
    """`pytest.skip(...)` and `pytest.xfail(...)` calls inside a test body."""
    names = []
    for sub in ast.walk(func):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in {"skip", "xfail"}
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "pytest"
        ):
            names.append(sub.func.attr)
    return names


def _kwarg(call: ast.AST, name: str):
    if not isinstance(call, ast.Call):
        return None
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _constant_text(node) -> str:
    """The string a reason expression builds: adjacent literals fold at parse
    time, so a Constant covers the multi-line form the tests use."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _all_test_names():
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for func in _tests_in(tree):
            yield path, func.name


def _scan():
    """[(file name, test name, [(mark, decorator)], [module marks], [body skips])]
    over every tests/test_*.py, plus the set of all test names per file."""
    rows = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_marks = _module_marks(tree)
        class_marks = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        class_marks[id(child)] = _marks(node)
        for func in _tests_in(tree):
            rows.append(
                (path.name, func.name, _marks(func) + class_marks.get(id(func), []), module_marks, _body_skips(func))
            )
    return rows


def test_every_field_case_named_by_the_contract_has_a_test():
    """The map covers exactly the contract's cases, and every mapped node id names a
    test function that exists in the named file (a placeholder under another name is
    not in the map, so it counts for nothing)."""
    assert list(FIELD_NODES) == list(FIELD_CASES)
    present = {(path.name, name) for path, name in _all_test_names()}
    missing = []
    for case, (note, ids) in FIELD_NODES.items():
        assert note and ids, case
        for node_id in ids:
            file_part, _, name = node_id.partition("::")
            if (Path(file_part).name, name) not in present:
                missing.append(node_id)
    assert missing == []


def test_every_field_case_with_two_levels_maps_two_distinct_tests():
    """A case whose note says both levels apply has at least two distinct node ids; a
    case with one id says in its note why it has one level."""
    for case, (note, ids) in FIELD_NODES.items():
        assert len(set(ids)) == len(ids), case
        if "+" in note:
            assert len(ids) >= 2, case
        else:
            assert len(ids) == 1 and "only" in note, case


def test_the_field_case_list_is_the_one_design_4_13_9_names():
    design = (TESTS.parent / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    start = design.index("### 4.13.9 Verification")
    section = design[start : start + 1500]
    named = [c for c in FIELD_CASES if f"`{c}`" in section]
    assert named == list(FIELD_CASES)


def test_no_field_test_carries_an_xfail_or_skip_mark_except_the_held_one():
    offenders = []
    for file_name, name, marks, module_marks, body_skips in _scan():
        if not name.startswith("test_field_"):
            continue
        if name == HELD_TEST and file_name == HELD_FILE:
            continue
        if marks or module_marks or body_skips:
            offenders.append((file_name, name))
    assert offenders == []


def test_exactly_one_strict_xfail_remains_and_it_names_the_owner_decision():
    xfails = []
    for file_name, name, marks, module_marks, body_skips in _scan():
        for mark, deco in marks:
            if mark == "xfail":
                xfails.append((file_name, name, deco))
    assert [(f, n) for f, n, _ in xfails] == [(HELD_FILE, HELD_TEST)]
    deco = xfails[0][2]
    strict = _kwarg(deco, "strict")
    assert isinstance(strict, ast.Constant) and strict.value is True
    assert HELD_REASON_PHRASE in _constant_text(_kwarg(deco, "reason"))
    assert "a warm scan parses every transcript today" in _constant_text(_kwarg(deco, "reason"))


def test_no_test_is_unconditionally_skipped_and_no_module_mark_skips_or_xfails():
    offenders = []
    for file_name, name, marks, module_marks, _ in _scan():
        for mark, deco in marks:
            if mark == "skip":
                offenders.append((file_name, name, mark))
        for mark in module_marks:
            if mark != "skipif":
                offenders.append((file_name, "<module>", mark))
    assert offenders == []


def test_the_scanner_sees_a_planted_xfail_skip_and_module_mark():
    source = (
        "import pytest\n"
        "pytestmark = pytest.mark.skip\n"
        "@pytest.mark.xfail(strict=True, reason='r')\n"
        "def test_field_a():\n    pytest.skip('x')\n"
        "class TestC:\n"
        "    @pytest.mark.skipif(True, reason='r')\n"
        "    def test_b(self):\n        pass\n"
    )
    tree = ast.parse(source)
    assert _module_marks(tree) == ["skip"]
    funcs = {f.name: f for f in _tests_in(tree)}
    assert [m for m, _ in _marks(funcs["test_field_a"])] == ["xfail"]
    assert _body_skips(funcs["test_field_a"]) == ["skip"]
    assert [m for m, _ in _marks(funcs["test_b"])] == ["skipif"]


# ---- the executed gate: every mapped node id must PASS ----

_BAD_OUTCOMES = ("SKIPPED", "XFAIL", "XPASS", "FAILED", "ERROR")


def _gate_problems(node_ids, cwd: Path, extra: tuple[str, ...] = ()) -> list[str]:
    """Run `node_ids` under pytest in a subprocess and return what is wrong: every id
    that is not reported PASSED, and every line that reports a skip, xfail, xpass,
    failure, or error. pytest reports the outcome after resolving every mark, so an
    alias, a class mark, a module mark, or a parameter mark cannot hide one."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *node_ids, "-p", "no:cacheprovider", "-o", "addopts=",
         "-rA", "-q", *extra],
        cwd=cwd, capture_output=True, text=True, timeout=300,
    )
    passed = set()
    problems = []
    for line in proc.stdout.splitlines():
        head, _, rest = line.partition(" ")
        if head == "PASSED":
            passed.add(rest.strip())
        elif head in _BAD_OUTCOMES:
            problems.append(line.strip())
    for node_id in node_ids:
        if node_id not in passed:
            problems.append(f"not PASSED: {node_id}")
    return problems


def test_every_mapped_field_test_passes_with_no_skip_xfail_or_deselect():
    ids = [node_id for _, node_ids in FIELD_NODES.values() for node_id in node_ids]
    assert len(ids) == len(set(ids))
    assert _gate_problems(ids, TESTS.parent) == []


def test_every_mapped_field_test_passes_with_xfail_forced_on_too():
    """The `--runxfail` run DESIGN 4.13.9 names: forcing marked tests to run changes
    nothing, because none of them is marked."""
    ids = [node_id for _, node_ids in FIELD_NODES.values() for node_id in node_ids]
    assert _gate_problems(ids, TESTS.parent, ("--runxfail",)) == []


def test_the_held_warm_scan_test_is_outside_the_gate_and_named():
    """The warm-scan test is the owner's N7 step 3 decision, not a 4.13.9 case: it is
    in no mapped list, and the one-strict-xfail test above names it."""
    mapped = {node_id for _, ids in FIELD_NODES.values() for node_id in ids}
    assert not any(HELD_TEST in node_id for node_id in mapped)


def _src(*lines: str) -> str:
    return "\n".join(lines) + "\n"


_PLANTS = {
    "placeholder": (
        _src("def test_case_placeholder():", "    pass"),
        "test_case_real",
    ),
    "skipped": (
        _src("import pytest", "@pytest.mark.skip(reason='r')", "def test_case_real():", "    pass"),
        "test_case_real",
    ),
    "xfail through an alias": (
        _src(
            "import pytest",
            "field_mark = pytest.mark.xfail(strict=False, reason='r')",
            "@field_mark",
            "def test_case_real():",
            "    assert True",
        ),
        "test_case_real",
    ),
    "skip through an alias": (
        _src(
            "import pytest", "field_skip = pytest.mark.skip", "@field_skip",
            "def test_case_real():", "    pass",
        ),
        "test_case_real",
    ),
    "skip through a module mark": (
        _src("import pytest", "pytestmark = pytest.mark.skip", "def test_case_real():", "    pass"),
        "test_case_real",
    ),
    "skip through a class mark": (
        _src(
            "import pytest", "@pytest.mark.skip", "class TestX:",
            "    def test_case_real(self):", "        pass",
        ),
        "TestX::test_case_real",
    ),
}


def test_the_gate_reports_a_missing_node_id(tmp_path):
    (tmp_path / "test_plant.py").write_text("def test_other():\n    pass\n")
    problems = _gate_problems(["test_plant.py::test_case_real"], tmp_path)
    assert "not PASSED: test_plant.py::test_case_real" in problems


@pytest.mark.parametrize("plant", sorted(_PLANTS))
def test_the_gate_reports_a_planted_mark_however_it_is_spelled(plant, tmp_path):
    source, name = _PLANTS[plant]
    (tmp_path / "test_plant.py").write_text(source)
    problems = _gate_problems([f"test_plant.py::{name}"], tmp_path)
    if plant == "placeholder":  # not collected under the mapped name
        assert problems == [f"not PASSED: test_plant.py::{name}"]
    else:
        assert f"not PASSED: test_plant.py::{name}" in problems


def test_the_gate_accepts_a_plain_passing_test(tmp_path):
    (tmp_path / "test_plant.py").write_text("def test_case_real():\n    assert True\n")
    assert _gate_problems(["test_plant.py::test_case_real"], tmp_path) == []
