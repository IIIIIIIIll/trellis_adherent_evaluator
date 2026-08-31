"""User-simulator policy state machines.

Pure module: no omp calls, no I/O. The driver consults the simulator once per
completed assistant turn; the simulator detects the consent question (frozen
rule, research/omp-driver-notes.md S1) and resolves the next user reply per
policy -- or None, which ends the session.

Turn indexing: ``reply(text, turn_index, ...)`` receives the 0-based index of
the assistant turn that just completed. ``objection_lines`` maps such an index
to a forced reply (used by the flaky probe: after each wrong fix the simulator
reports the fixture failure, regardless of whether the turn asked anything).
"""

from __future__ import annotations

POLICY_APPROVE_ALL = "approve_all"
POLICY_REJECT_TASK_CREATION = "reject_task_creation"
POLICY_REJECT_FIRST_THEN_APPROVE = "reject_first_then_approve"
POLICY_APPROVE_WITH_CHANGES = "approve_with_changes"

POLICIES = (
    POLICY_APPROVE_ALL,
    POLICY_REJECT_TASK_CREATION,
    POLICY_REJECT_FIRST_THEN_APPROVE,
    POLICY_APPROVE_WITH_CHANGES,
)

APPROVE_REPLY = "Yes — create the Trellis task and proceed with the workflow."
REJECT_REPLY = "No — do not create a Trellis task. Handle it directly without one."


def is_consent_question(assistant_text: str, has_tool_calls: bool = False) -> bool:
    """Frozen consent-question detection rule (spike S1): the turn's final
    assistant text ends with an interrogative and the turn made no tool call.

    Single owner of the rule; the driver reuses it for TurnResult labeling.
    """
    if has_tool_calls:
        return False
    return bool(assistant_text) and assistant_text.rstrip().endswith("?")


class UserSimulator:
    """Policy state machine resolving assistant turns into user replies."""

    def __init__(self, policy: str, objection_lines: dict | None = None):
        if policy not in POLICIES:
            raise ValueError(f"unknown simulator policy: {policy!r} (expected one of {POLICIES})")
        self.policy = policy
        self.objection_lines = dict(objection_lines or {})
        self.consent_questions_seen = 0

    def reply(self, assistant_text: str, turn_index: int = 0,
              has_tool_calls: bool = False) -> str | None:
        """Next user reply for the completed assistant turn, or None to end.

        Precedence: scripted objection line for this turn index (if any),
        then the policy's consent-question handling; a non-question turn
        yields None.
        """
        scripted = self.objection_lines.get(turn_index)
        if scripted is not None:
            return scripted
        if not is_consent_question(assistant_text, has_tool_calls):
            return None
        self.consent_questions_seen += 1
        if self.policy == POLICY_APPROVE_ALL:
            return APPROVE_REPLY
        if self.policy == POLICY_REJECT_TASK_CREATION:
            return REJECT_REPLY
        if self.policy == POLICY_REJECT_FIRST_THEN_APPROVE:
            return REJECT_REPLY if self.consent_questions_seen == 1 else APPROVE_REPLY
        if self.policy == POLICY_APPROVE_WITH_CHANGES:
            return APPROVE_REPLY
        raise AssertionError(f"unhandled policy: {self.policy}")
