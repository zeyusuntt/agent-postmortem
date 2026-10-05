"""Tests for redact.py: the only place raw paths and ids may leak by default."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import agent_postmortem
from agent_postmortem import detectors, reader, report, trend
from agent_postmortem.models import Attempt, Finding, LogicalAgent, Run
from agent_postmortem.redact import BUILT_IN_AGENT_TYPES, Pseudonyms, Redactor, redact


def test_redacts_home_directory_to_tilde():
    # M8 review D-A changed this contract: a home-relative path used to keep
    # every directory name verbatim after the "~" substitution ("~/project/
    # notes.md"); one directory segment ("project") is still enough to name
    # something a reader should not see, so it now collapses to "...", the
    # same as any deeper path (see test_collapses_a_home_relative_path_*
    # below).
    home = Path("/home/user")
    text = f"reading {home}/project/notes.md"
    assert redact(text, home=home) == "reading ~/.../notes.md"


def test_redacts_uuid_to_last_four_hex_chars():
    uuid = "00000000-0000-4000-8000-000000000000"
    text = f"session {uuid} started"
    assert redact(text, home=Path("/no/such/home")) == "session uuid:0000 started"


def test_redacts_project_slug_to_hashed_id():
    slug = "-Users-alice-Documents-demo"
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:6]
    text = f"project {slug} scanned"
    assert redact(text, home=Path("/no/such/home")) == f"project project:{digest} scanned"


def test_agent_ids_are_not_redacted():
    text = "agent a1234567890abcdef finished"
    assert redact(text, home=Path("/no/such/home")) == text


def test_empty_text_is_returned_unchanged():
    assert redact("", home=Path("/home/user")) == ""


def test_redactor_no_op_mode_leaves_text_unchanged():
    text = "/home/user/project has uuid 00000000-0000-4000-8000-000000000000"
    redactor = Redactor(enabled=False, home=Path("/home/user"))
    assert redactor(text) == text


def test_redactor_enabled_mode_matches_redact():
    text = "has uuid 00000000-0000-4000-8000-000000000000 in /home/user/project"
    redactor = Redactor(enabled=True, home=Path("/home/user"))
    assert redactor(text) == redact(text, home=Path("/home/user"))


def test_redacts_project_slug_with_non_ascii_segment():
    """A project slug segment outside [A-Za-z0-9_.] (here CJK characters)
    must not break the match partway through and leak the rest verbatim."""
    slug = "-Users-alice-Documents-测试-project"
    result = redact(slug, home=Path("/no/such/home"))
    assert result.startswith("project:")
    assert "Users" not in result
    assert "测试" not in result


def test_redacts_project_slug_with_apostrophe_segment():
    slug = "-Users-obrien-Documents-app's-config"
    result = redact(slug, home=Path("/no/such/home"))
    assert result.startswith("project:")
    assert "Users" not in result
    assert "config" not in result


def test_redacts_linux_style_home_project_slug():
    slug = "-home-alice-project"
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:6]
    text = f"project {slug} scanned"
    assert redact(text, home=Path("/no/such/home")) == f"project project:{digest} scanned"


def test_redacts_home_directory_truncated_mid_username():
    # The runtime's own error messages quote commands cut at a byte limit, so a
    # transcript can hold "/Users/ali" for a home of "/Users/alice". The exact
    # home string never matches; a proper prefix of the user segment must
    # still be swept up, or the report leaks part of the user name.
    home = Path("/Users/alice")
    assert redact("cmd was: cd /Users/ali", home=home) == "cmd was: cd ~"
    assert redact("cd /Users/alic' (first 20 of 20 bytes)", home=home) == "cd ~' (first 20 of 20 bytes)"
    assert redact("(/home/al", home=Path("/home/alice")) == "(~"


def test_truncation_rule_leaves_other_users_alone():
    # "/Users/bob" is not a prefix of "/Users/alice": another account's path is
    # not ours to rewrite, and the caller sees it unchanged.
    assert redact("see /Users/bob/x", home=Path("/Users/alice")) == "see /Users/bob/x"
    # A longer name that merely starts with ours is a different user too.
    assert redact("see /Users/alicedoe/x", home=Path("/Users/alice")) == "see /Users/alicedoe/x"


def test_full_home_still_redacts_when_followed_by_path():
    # D-A: the directory between "~/" and the file collapses too (see
    # test_collapses_a_home_relative_path_* below); this test now only
    # confirms the home substitution itself still fires ahead of that.
    assert redact("/Users/alice/Documents/x.md", home=Path("/Users/alice")) == "~/.../x.md"


def test_redacts_bare_user_name_and_email():
    # Shell output the runtime captured carries the account name on its own:
    # ls owner columns, ps rows, pytest temp dirs, git author lines.
    home = Path("/Users/alice")
    assert redact("drwxr-xr-x  3 alice  staff  96 x", home=home) == "drwxr-xr-x  3 user  staff  96 x"
    assert redact("/T/pytest-of-alice/pytest-1", home=home) == "/T/pytest-of-user/pytest-1"
    out = redact("Author: Alice Doe <alice06@example.com>", home=home)
    assert "alice06@example.com" not in out and "email:" in out
    # Not a whole word: "malice" and "alices" are other tokens and stay.
    assert redact("malice alices", home=home) == "malice alices"


def test_short_user_names_are_not_swept_as_bare_words():
    # A two-letter account name would match ordinary words; the bare-word rule
    # needs at least three characters. Paths still redact.
    assert redact("go to /Users/al/x and go", home=Path("/Users/al")) == "go to ~/x and go"


# --- D-A: home-relative and /tmp path collapsing ----------------------------


def test_collapses_a_home_relative_path_with_one_directory_segment():
    """Depth 1: one directory between "~/" and the file is still enough to
    name something private (a client, a project); it collapses same as any
    deeper path."""
    home = Path("/home/user")
    text = f"wrote to {home}/secret-client/report.md"
    assert redact(text, home=home) == "wrote to ~/.../report.md"


def test_collapses_a_home_relative_path_with_five_directory_segments():
    """Depth 5: an arbitrarily deep path collapses to the exact same shallow
    shape as depth 1, not a partial walk that still shows some of the
    directory names."""
    home = Path("/home/user")
    text = f"wrote to {home}/a/b/c/d/e/deep.md"
    assert redact(text, home=home) == "wrote to ~/.../deep.md"


def test_collapses_a_home_relative_path_with_spaces_and_a_non_ascii_segment():
    home = Path("/Users/alice")
    text = f"wrote to {home}/Documents/Café Notes/sub dir/report.md"
    result = redact(text, home=home)
    assert result == "wrote to ~/.../report.md"
    assert "Café" not in result
    assert "Documents" not in result


def test_collapse_keeps_a_trailing_slash_on_a_directory_path():
    home = Path("/home/user")
    text = f"cd {home}/Documents/project/sub/"
    assert redact(text, home=home) == "cd ~/.../sub/"


def test_a_bare_tilde_alone_is_left_untouched():
    assert redact("cd ~ && ls", home=Path("/home/user")) == "cd ~ && ls"


def test_a_home_relative_path_with_no_directory_segment_is_left_untouched():
    """"~/notes.md" already shows only its last segment; nothing between
    "~/" and the file needs collapsing, and no spurious "..." is inserted."""
    home = Path("/home/user")
    text = f"wrote to {home}/notes.md"
    assert redact(text, home=home) == "wrote to ~/notes.md"


def test_a_url_containing_a_tilde_is_left_untouched():
    text = "See https://example.com/~alice/notes for details"
    assert redact(text, home=Path("/home/user")) == text


def test_collapses_a_tmp_path_beyond_its_first_segment():
    text = "wrote to /tmp/agent-postmortem-fixture/sub/report.md"
    assert redact(text, home=Path("/no/such/home")) == "wrote to /tmp/agent-postmortem-fixture/.../report.md"


def test_collapses_a_private_tmp_path_beyond_its_first_segment():
    text = "wrote to /private/tmp/agent-postmortem-fixture/sub/dir/report.md"
    result = redact(text, home=Path("/no/such/home"))
    assert result == "wrote to /private/tmp/agent-postmortem-fixture/.../report.md"


def test_a_tmp_path_with_only_a_first_segment_is_left_untouched():
    """Nothing beyond the first segment: the file sits directly inside the
    scratch directory, which is not itself a name to hide (D-A)."""
    text = "wrote to /tmp/agent-postmortem-fixture/report.md"
    assert redact(text, home=Path("/no/such/home")) == text


# --- Pseudonyms (docs/DESIGN.md 5.1) ---------------------------------------


def test_pseudonyms_agent_phase_and_type_number_in_order_of_first_appearance():
    names = Pseudonyms(enabled=True)
    assert names.agent("impl:silent-alpha") == "agent-1"
    assert names.phase("Implement") == "phase-1"
    assert names.agent("impl:silent-beta") == "agent-2"
    assert names.phase("Review") == "phase-2"
    assert names.type("custom-reviewer") == "type-1"


def test_pseudonyms_description_has_its_own_namespace_separate_from_labels():
    """M8 review D-D: a label and a differently-worded description for the
    same attempt must not share one counter -- that used to consume an
    agent-N slot the second attempt's own label needed, skipping a row in
    a rendered AGENT table (agent-1, agent-3, no agent-2)."""
    names = Pseudonyms(enabled=True)
    assert names.agent("impl:silent-alpha") == "agent-1"
    assert names.description("Implement the silent-alpha module.", label="impl:silent-alpha") == "desc-1"
    assert names.agent("impl:silent-beta") == "agent-2"
    assert names.description("Implement the silent-beta module.", label="impl:silent-beta") == "desc-2"


def test_pseudonyms_description_identical_to_label_reuses_the_label_pseudonym():
    """A description that is literally the same string as the label is not
    a second name -- it must not mint its own desc-N (D-D)."""
    names = Pseudonyms(enabled=True)
    assert names.agent("impl:silent-alpha") == "agent-1"
    assert names.description("impl:silent-alpha", label="impl:silent-alpha") == "agent-1"


def test_pseudonyms_are_stable_within_one_instance():
    names = Pseudonyms(enabled=True)
    first = names.agent("impl:silent-alpha")
    assert names.agent("impl:silent-alpha") == first
    assert names.phase("Implement") != first  # different category, own counter


def test_pseudonyms_two_instances_never_share_a_mapping():
    """docs/DESIGN.md 5.1: the mapping is rebuilt for the next render, so two
    reports cannot be linked by a repeated pseudonym."""
    first = Pseudonyms(enabled=True)
    second = Pseudonyms(enabled=True)
    assert first.agent("impl:silent-alpha") == "agent-1"
    assert second.agent("impl:silent-beta") == "agent-1"


def test_pseudonyms_built_in_agent_types_pass_through():
    names = Pseudonyms(enabled=True)
    for built_in in BUILT_IN_AGENT_TYPES:
        assert names.type(built_in) == built_in


def test_pseudonyms_disabled_is_a_pass_through():
    names = Pseudonyms(enabled=False)
    assert names.agent("impl:silent-alpha") == "impl:silent-alpha"
    assert names.phase("Implement") == "Implement"
    assert names.type("custom-reviewer") == "custom-reviewer"
    assert names.apply("impl:silent-alpha did the work") == "impl:silent-alpha did the work"


def test_pseudonyms_none_and_empty_pass_through_without_registering():
    names = Pseudonyms(enabled=True)
    assert names.agent(None) is None
    assert names.agent("") == ""
    assert names.agent("impl:silent-alpha") == "agent-1"  # counter untouched by the no-ops above


def test_pseudonyms_apply_substitutes_a_registered_name_inside_free_text():
    names = Pseudonyms(enabled=True)
    names.agent("impl:silent-alpha")
    text = "impl:silent-alpha restarted 2 times, 10m wasted"
    assert names.apply(text) == "agent-1 restarted 2 times, 10m wasted"


def test_pseudonyms_apply_does_not_touch_an_unregistered_name():
    names = Pseudonyms(enabled=True)
    text = "impl:silent-alpha restarted 2 times"
    assert names.apply(text) == text


def test_pseudonyms_apply_prefers_the_longer_match_when_one_name_contains_another():
    names = Pseudonyms(enabled=True)
    names.agent("impl:alpha")
    names.agent("impl:alpha-2")
    text = "impl:alpha-2 finished; impl:alpha waited"
    result = names.apply(text)
    assert "impl:alpha-2" not in result
    assert result == "agent-2 finished; agent-1 waited"


# --- Redactor + Pseudonyms composition -------------------------------------


def test_redactor_carries_its_own_pseudonyms_instance():
    redactor = Redactor(enabled=True)
    assert isinstance(redactor.names, Pseudonyms)


def test_redactor_call_is_structural_only_and_never_applies_a_pseudonym():
    """docs/DESIGN.md 5.1/5.2: plain Redactor.__call__ is structural redact()
    only. Evidence, diagnostics, and metrics go through this and must never
    have a registered name substituted into them -- only Finding.title
    (Redactor.title()) does."""
    redactor = Redactor(enabled=True, home=Path("/no/such/home"))
    redactor.names.agent("impl:silent-alpha")
    text = "impl:silent-alpha restarted 2 times"
    assert redactor(text) == text


def test_redactor_title_applies_registered_pseudonyms_after_structural_redaction():
    redactor = Redactor(enabled=True, home=Path("/no/such/home"))
    redactor.names.agent("impl:silent-alpha")
    text = "impl:silent-alpha restarted 2 times"
    assert redactor.title(text) == "agent-1 restarted 2 times"


def test_redactor_no_redact_disables_pseudonyms_too():
    redactor = Redactor(enabled=False)
    redactor.names.agent("impl:silent-alpha")  # no-op: names is disabled too
    assert redactor("impl:silent-alpha restarted 2 times") == "impl:silent-alpha restarted 2 times"
    assert redactor.title("impl:silent-alpha restarted 2 times") == "impl:silent-alpha restarted 2 times"


# --- F1: report_path gets its own "path-N" pseudonym namespace -------------
#
# Unlike every other Pseudonyms category (agent/desc/phase/type/target,
# confined to Finding.title via Redactor.title()), a registered path is also
# substituted by the plain Redactor call itself: report_path is checked by
# structural redact() alone before this fix, and a relative path or an
# absolute path outside home and /tmp passed through unchanged (docs/
# DESIGN.md 5.1, M1 review finding 1).


def test_pseudonyms_path_gets_its_own_namespace_numbered_in_order():
    names = Pseudonyms(enabled=True)
    assert names.path("clients/acme-secret/reports/internal.md") == "path-1"
    assert names.path("/var/reports/internal.md") == "path-2"
    # the same value seen again reuses its own pseudonym
    assert names.path("clients/acme-secret/reports/internal.md") == "path-1"


def test_pseudonyms_path_disabled_is_a_pass_through():
    names = Pseudonyms(enabled=False)
    assert names.path("clients/acme-secret/reports/internal.md") == (
        "clients/acme-secret/reports/internal.md"
    )


def test_redactor_call_substitutes_a_registered_path_unlike_a_name():
    """Unlike agent/phase/desc/type/target, a registered path is substituted
    by the plain Redactor call itself (evidence, diagnostics, metrics), not
    only by title() -- report_path is not a faithful transcript quote the
    way evidence text is; it is a self-reported claim value that must never
    appear raw by default wherever it is rendered."""
    redactor = Redactor(enabled=True, home=Path("/no/such/home"))
    redactor.names.path("clients/acme-secret/reports/internal.md")
    text = "report at clients/acme-secret/reports/internal.md was missing"
    assert redactor(text) == "report at path-1 [text hidden]"
    assert "clients/acme-secret/reports/internal.md" not in redactor(text)


def test_redactor_call_path_substitution_runs_before_structural_redact():
    """A report_path under the home directory must become path-N, not the
    general "~/.../basename" collapse -- the path pseudonym is registered
    (and substituted) against the raw value before redact()'s own home/tmp
    rules ever see it."""
    home = Path("/Users/tester")
    redactor = Redactor(enabled=True, home=home)
    raw = "/Users/tester/Documents/secret-client/report.md"
    redactor.names.path(raw)
    result = redactor(f"wrote to {raw}")
    assert result == "wrote to path-1"
    assert "secret-client" not in result
    assert "~" not in result


def test_redactor_no_redact_shows_the_raw_path():
    redactor = Redactor(enabled=False)
    redactor.names.path("clients/acme-secret/reports/internal.md")
    text = "report at clients/acme-secret/reports/internal.md was missing"
    assert redactor(text) == text


def test_pseudonyms_apply_is_word_boundary_anchored():
    """docs/DESIGN.md 5.1: a short registered name must not match as a
    fragment of an unrelated, longer word ('cli' inside 'client')."""
    names = Pseudonyms(enabled=True)
    names.agent("cli")
    assert names.apply("client crashed") == "client crashed"
    assert names.apply("the cli crashed") == "the agent-1 crashed"


# --- D1 review fix: evidence is never fed to Pseudonyms --------------------

_FAKE_HOME = Path("/Users/tester")
_FAKE_UUID = "00000000-0000-4000-8000-000000000000"


def _make_named_run(*, description: str | None = "impl:demo") -> tuple[Run, Finding]:
    """One attempt whose phase is 'Implement' and whose own evidence
    sentence starts with the same word, plus a Finding whose title embeds
    the attempt's label -- the exact shape of the packaged demo run's
    agent_postmortem/demo/wf_00000000-d30/agent-ac200000000000001.jsonl:1,
    the case the review reproduced its corruption against."""
    attempt = Attempt(
        agent_id="a1111111111111111",
        key="v2:demo0001",
        label="impl:demo",
        phase="Implement",
        agent_type="sonnet",
        meta={"description": description} if description else {},
        transcript_path=_FAKE_HOME / "project" / "agent-a1111111111111111.jsonl",
        events=[],
        first_ts=datetime(2026, 9, 10, 19, 0, tzinfo=timezone.utc),
        last_ts=datetime(2026, 9, 10, 19, 10, tzinfo=timezone.utc),
        ordinal=1,
        outcome="completed",
        usage={},
    )
    agent = LogicalAgent(key="v2:demo0001", label="impl:demo", phase="Implement", attempts=[attempt])
    run = Run(
        run_id="wf_00000000-001",
        path=_FAKE_HOME / "project" / "subagents" / "workflows" / "wf_00000000-001",
        project_slug="-Users-tester-Documents-demo",
        session_id=_FAKE_UUID,
        claude_version="2.1.267",
        journal=[],
        agents=[agent],
        attempts=[attempt],
        started_at=attempt.first_ts,
        ended_at=attempt.last_ts,
        diagnostics=[],
    )
    finding = Finding(
        detector="silent_retry",
        severity="major",
        title="impl:demo restarted 2 times, 10m wasted",
        run_id=run.run_id,
        agent_id=attempt.agent_id,
        evidence=["agent-a1111111111111111.jsonl:1: Implement the silent-alpha module and add a test."],
        metrics={"count": 2},
        rule_id="R-RETRY-SAFE",
        finding_id="deadbeef0000",
    )
    return run, finding


def test_evidence_is_never_corrupted_by_a_registered_phase_or_label():
    """The exact corruption the review reproduced: registering phase
    'Implement' must never turn 'Implement the silent-alpha module...'
    inside an evidence line into 'phase-1 the silent-alpha module...', and
    a short registered name must never match as a word fragment either."""
    run, finding = _make_named_run()
    redactor = Redactor(enabled=True, home=_FAKE_HOME)

    # unit M3a: an evidence excerpt only prints with --evidence now (docs/
    # DESIGN.md 4.7 rule 7); this exercises the exact same corruption check
    # against the --evidence text.
    # N3b P16: excerpts exist only at local and raw; this checks the corruption
    # rule at raw, where the excerpt is printed untouched, and that share
    # prints no excerpt at all.
    raw = Redactor(enabled=False, home=_FAKE_HOME)
    text = report.render_run_text(run, [finding], raw, evidence=True)
    assert "Implement the silent-alpha module and add a test." in text
    share_text = report.render_run_text(run, [finding], redactor, evidence=True)
    assert "silent-alpha module" not in share_text

    bug = report.render_bug_report(
        run, [finding], Redactor(enabled=False, home=_FAKE_HOME), evidence=True,
    )
    assert "Implement the silent-alpha module and add a test." in bug
    share_bug = report.render_bug_report(
        run, [finding], Redactor(enabled=True, home=_FAKE_HOME), evidence=True,
    )
    assert "silent-alpha module" not in share_bug

    # N5a-1 (schema 3, docs/DESIGN.md 5.3b): the exported evidence follows the same
    # evidence view as the text: pointer only at share (no book here, so the file
    # stem is positional), the untouched excerpt only at raw with --evidence.
    exported = report.export_run(run, [finding], Redactor(enabled=True, home=_FAKE_HOME))
    assert exported["findings"][0]["evidence"] == ["agent-1.jsonl:1"]
    exported_raw = report.export_run(
        run, [finding], Redactor(enabled=False, home=_FAKE_HOME), evidence=True,
    )
    assert exported_raw["findings"][0]["evidence"] == [
        "agent-a1111111111111111.jsonl:1: Implement the silent-alpha module and add a test."
    ]


def test_finding_title_is_still_pseudonymized_in_every_renderer():
    run, finding = _make_named_run()

    text = report.render_run_text(run, [finding], Redactor(enabled=True, home=_FAKE_HOME))
    assert "impl:demo restarted" not in text
    # unit M3a: STORIES prints "  <label>  <story text>" (two spaces both
    # sides of the label), not the old "<label> <verb>..." finding line.
    assert "agent-1  Restarted" in text

    bug = report.render_bug_report(run, [finding], Redactor(enabled=True, home=_FAKE_HOME))
    assert "impl:demo restarted" not in bug

    exported = report.export_run(run, [finding], Redactor(enabled=True, home=_FAKE_HOME))
    assert "impl:demo" not in exported["findings"][0]["title"]
    assert exported["findings"][0]["title"].startswith("agent-")


def test_meta_description_is_registered_even_when_never_displayed_directly():
    """docs/DESIGN.md 5.1: a renderer registers meta description alongside
    label before rendering, so a title that happens to embed the
    description (not the label) is still pseudonymized."""
    run, _ = _make_named_run(description="Implement the demo feature end to end.")
    finding = Finding(
        detector="codex_write_denied",
        severity="critical",
        title="Implement the demo feature end to end.: write denied",
        run_id=run.run_id,
        agent_id=run.attempts[0].agent_id,
        evidence=[],
        metrics={},
        rule_id="R-CODEX-CONTRACT",
        finding_id="deadbeef0001",
    )
    exported = report.export_run(run, [finding], Redactor(enabled=True, home=_FAKE_HOME))
    assert "Implement the demo feature end to end." not in exported["findings"][0]["title"]


def test_each_public_renderer_call_gets_a_fresh_pseudonym_map():
    """docs/DESIGN.md 5.1: the mapping is rebuilt for every render, even
    reusing the same Redactor object across two calls (the exact reuse the
    review reproduced), so a second run's agent-1 is never really the first
    run's agent-3."""
    run, finding = _make_named_run()
    redactor = Redactor(enabled=True, home=_FAKE_HOME)

    first = report.export_run(run, [finding], redactor)
    second = report.export_run(run, [finding], redactor)

    assert first["run"]["attempts"][0]["label"] == "agent-1"
    assert second["run"]["attempts"][0]["label"] == "agent-1"


# --- privacy sweep: no protected value survives redaction, anywhere --------
#
# docs/DESIGN.md 5.1 pseudonymizes four things per attempt: label, meta
# description, phase, and a non-built-in agent_type. This sweep collects all
# four from every fixture (both kinds), not just label, and checks every
# public renderer: text, bug report, JSON export, scan text, and trend text/
# JSON. Each renderer call below gets its own fresh Redactor (docs/DESIGN.md
# 5.1: the mapping is rebuilt for every render), matching how the CLI itself
# only ever constructs one Redactor per invocation. A protected value is
# allowed to survive only inside a Finding's own evidence or metrics -- both
# structural redact() only, never pseudonymized (docs/DESIGN.md 5.1/5.3) --
# so those two fields are excluded before the "must not appear" check runs;
# everything else a renderer prints is in scope.

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runs"
# silent_retry_kill lives in the package now (docs/DESIGN.md 5.4/6); its
# parent is swept alongside FIXTURES_ROOT's case directories so the sweep
# keeps checking it too, not just the cases still under tests/fixtures/.
DEMO_ROOT = Path(agent_postmortem.__file__).parent / "demo"


def _every_fixture_run():
    """Every run any fixture case under tests/fixtures/runs/, plus the
    packaged demo case, loads to (both kinds): each case directory is
    itself a root (docs/DESIGN.md 6), so discover_runs is pointed at each
    one in turn rather than at the whole fixtures tree."""
    case_dirs = sorted(p for p in FIXTURES_ROOT.iterdir() if p.is_dir())
    case_dirs += [DEMO_ROOT]
    for case_dir in case_dirs:
        for ref in reader.discover_runs(case_dir):
            try:
                yield case_dir.name, reader.load_run(ref)
            except reader.NotARun:
                continue


def _protected_values(run) -> set[str]:
    """Every value docs/DESIGN.md 5.1 pseudonymizes for this run: each child
    attempt's label, meta description, phase, and non-built-in agent_type --
    exactly what report._register_names registers, and nothing run.main
    carries (its label is the fixed literal 'main', never user text)."""
    values: set[str] = set()
    for attempt in run.attempts:
        if attempt.label:
            values.add(attempt.label)
        if attempt.phase:
            values.add(attempt.phase)
        meta = attempt.meta if isinstance(attempt.meta, dict) else {}
        description = meta.get("description")
        if isinstance(description, str) and description:
            values.add(description)
        if attempt.agent_type and attempt.agent_type not in BUILT_IN_AGENT_TYPES:
            values.add(attempt.agent_type)
    return values


def _meta_descriptions_not_otherwise_displayable(run) -> set[str]:
    """A meta description that differs from its own attempt's label. No
    renderer ever prints meta description on its own (docs/DESIGN.md 5.3
    excludes meta from export entirely, and neither the AGENT table nor the
    bug report's reproduction line reads it) -- it is registered purely so
    that a title or evidence line that happens to embed it is still caught,
    not because it is meant to be visible anywhere by itself. A session
    attempt's label already *is* its description (reader.py sets one from
    the other), so that case is already covered by the label check and
    excluded here."""
    values: set[str] = set()
    for attempt in run.attempts:
        meta = attempt.meta if isinstance(attempt.meta, dict) else {}
        description = meta.get("description")
        if isinstance(description, str) and description and description != attempt.label:
            values.add(description)
    return values


def _structural_evidence_substrings(findings, *, enabled: bool) -> list[str]:
    """The exact, structurally-redacted evidence text report.py embeds for
    each finding at this redaction setting -- the one place a protected
    value is allowed to survive untouched."""
    redactor = Redactor(enabled=enabled)
    return [redactor(line) for finding in findings for line in finding.evidence if line]


def _without_known_evidence(text: str, evidence_substrings: list[str]) -> str:
    result = text
    for substring in evidence_substrings:
        if substring:
            result = result.replace(substring, "")
    return result


def _json_excluding_evidence_and_metrics(payload: dict) -> str:
    """Stringify an export_run()/to_json() payload with each finding's
    evidence and metrics removed first (both structural redact() only,
    docs/DESIGN.md 5.1/5.3) -- everything left is text that must never carry
    a protected value."""
    sanitized_findings = [
        {key: value for key, value in finding.items() if key not in ("evidence", "metrics")}
        for finding in payload["findings"]
    ]
    return str({"run": payload["run"], "findings": sanitized_findings})


def test_no_protected_value_survives_redaction_outside_evidence_or_metrics():
    """docs/DESIGN.md 5.1/5.2: every attempt label, meta description, phase,
    and non-built-in agent_type is pseudonymized by default in the text
    report, the bug report, and the JSON export (outside evidence/metrics),
    and shown raw only with --no-redact. Swept over every fixture case, both
    kinds; tests/fixtures/runs/redaction_adversarial's phase and agent_type
    are deliberately never also a label, so this only passes if the sweep
    genuinely checks all four value kinds, not just label."""
    checked_at_least_one_value = False

    for case_name, run in _every_fixture_run():
        protected = _protected_values(run)
        if not protected:
            continue
        checked_at_least_one_value = True
        findings = detectors.run_detectors(run)

        redacted_text = report.render_run_text(run, findings, Redactor(enabled=True))
        redacted_evidence = _structural_evidence_substrings(findings, enabled=True)
        text_outside_evidence = _without_known_evidence(redacted_text, redacted_evidence)
        raw_text = report.render_run_text(run, findings, Redactor(enabled=False))

        redacted_bug = report.render_bug_report(run, findings, Redactor(enabled=True))
        bug_outside_evidence = _without_known_evidence(redacted_bug, redacted_evidence)
        raw_bug = report.render_bug_report(run, findings, Redactor(enabled=False))

        redacted_json = report.export_run(run, findings, Redactor(enabled=True))
        redacted_json_text = _json_excluding_evidence_and_metrics(redacted_json)
        raw_json = report.export_run(run, findings, Redactor(enabled=False))
        raw_json_text = str(raw_json)

        # docs/DESIGN.md 5.3 excludes meta from export, and no renderer
        # prints a meta description on its own; a description is registered
        # purely as a defensive catch for a title that happens to embed it,
        # so it is not required to be visible in any --no-redact renderer.
        displayable = protected - _meta_descriptions_not_otherwise_displayable(run)

        for value in protected:
            assert value not in text_outside_evidence, (
                f"{case_name}: {value!r} leaked into the text report outside evidence"
            )
            assert value not in bug_outside_evidence, (
                f"{case_name}: {value!r} leaked into the bug report outside evidence"
            )
            assert value not in redacted_json_text, (
                f"{case_name}: {value!r} leaked into the JSON export outside evidence/metrics"
            )
        for value in displayable:
            assert value in raw_text or value in raw_bug or value in raw_json_text, (
                f"{case_name}: {value!r} missing from every --no-redact renderer"
            )

    assert checked_at_least_one_value, "no fixture had a protected value to check -- the sweep found nothing"


def test_scan_and_trend_text_never_carry_a_protected_value():
    """docs/DESIGN.md 4.7: scan lists runs and trend lists detector counts;
    neither ever names an individual agent. Swept here (text and JSON, both
    renderers) so a future change that starts threading a label into either
    output trips this test immediately, not silently."""
    checked_at_least_one_value = False

    for case_name, run in _every_fixture_run():
        protected = _protected_values(run)
        if not protected:
            continue
        checked_at_least_one_value = True
        findings = detectors.run_detectors(run)

        row = {
            "run_id": run.run_id,
            "kind": run.kind,
            "date": "2026-09-20",
            "project": run.project_slug,
            "agents": len(run.agents),
            "attempts": len(run.attempts),
            "verdict": report.verdict_word(findings, run.status),
            "top_detector": findings[0].detector if findings else None,
        }
        totals = {"workflow": {}, "session": {}, "findings_per_detector": {}}
        scan_text = report.render_scan_text([row], totals, Redactor(enabled=True))

        weekly = trend.weekly_counts([(run, findings)], weeks=8)
        trend_text = trend.render_trend_text(weekly, [], weeks=8)
        trend_json = trend.to_json(weekly, [], weeks=8)

        for value in protected:
            assert value not in scan_text, f"{case_name}: {value!r} leaked into scan text"
            assert value not in trend_text, f"{case_name}: {value!r} leaked into trend text"
            assert value not in str(trend_json), f"{case_name}: {value!r} leaked into trend JSON"

    assert checked_at_least_one_value, "no fixture had a protected value to check -- the sweep found nothing"


def test_silent_retry_kill_fixtures_own_label_is_the_worked_example():
    """The exact case docs/DESIGN.md 6 calls out: impl:silent-alpha is
    absent from the redacted text report and present with --no-redact."""
    run = reader.load_run(
        DEMO_ROOT / "-home-user-project" / "00000000-0000-4000-8000-000000000000"
        / "subagents" / "workflows" / "wf_00000000-d30"
    )
    findings = detectors.run_detectors(run)

    redacted_text = report.render_run_text(run, findings, Redactor(enabled=True))
    raw_text = report.render_run_text(run, findings, Redactor(enabled=False))

    assert "impl:silent-alpha" not in redacted_text
    assert "impl:silent-alpha" in raw_text
