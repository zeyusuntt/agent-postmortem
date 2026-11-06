"""Story counts agree with their nouns (docs/DESIGN.md 4.7 rule 6,
"bash_backgrounded false positive ...": `1 Bash commands backgrounded.`)."""

from __future__ import annotations

import pytest

from agent_postmortem import report


@pytest.mark.parametrize(("detector", "one", "two"), [
    ("bash_backgrounded", "1 Bash command backgrounded", "2 Bash commands backgrounded"),
    ("bash_timeout", "1 Bash command timed out", "2 Bash commands timed out"),
    ("tool_json_error", "1 tool input JSON error", "2 tool input JSON errors"),
    ("structured_output_failure", "1 structured output failure", "2 structured output failures"),
    ("codex_write_denied", "1 sandbox write denial", "2 sandbox write denials"),
    ("codex_unavailable", "1 Codex call could not start", "2 Codex calls could not start"),
    ("classifier_unavailable", "1 classifier outage", "2 classifier outages"),
    ("git_cwd_mismatch", "1 git cwd mismatch", "2 git cwd mismatches"),
    ("permission_denied", "1 tool call denied", "2 tool calls denied"),
])
def test_a_tooling_story_count_agrees_with_its_noun(detector, one, two):
    assert report._tooling_phrase(detector, 1) == one
    assert report._tooling_phrase(detector, 2) == two
