"""N3b fix A, S10: config text echoed at share after `-` is scrubbed.
"""

from __future__ import annotations

from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor

SECRET = "zz" + "secret" + "9d1"


# --- S10 --------------------------------------------------------------------

def test_share_config_text_after_a_hyphen_is_scrubbed():
    redactor = Redactor(context=DisclosureContext(level="share"))
    text = f"agent-postmortem: unknown rule id 'R-NOPE-/private/client/{SECRET}' in [[adopted]]"
    shown = redactor.scrub_leftover_paths(text)
    assert SECRET not in shown and "client" not in shown
    # F fix 5 (W5-6): a rule id is never a path, so the mistyped `R-NOPE-` stays readable
    # (F fix 4 had hidden it with the path)
    assert "unknown rule id 'R-NOPE-path-1' in [[adopted]]" in shown


