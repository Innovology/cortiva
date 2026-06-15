"""Domain-work grounding: an agent surveys its real tools and must escalate a
data gap instead of inventing the result (the anti-hallucination belief rule)."""
from __future__ import annotations
from types import SimpleNamespace
from cortiva.core.fabric import Fabric


def _fab():
    return Fabric.__new__(Fabric)


def test_lists_real_tools_and_demands_escalation():
    out = _fab()._grounded_work_context(SimpleNamespace(id="financial-accountant"))
    assert "Grounded work" in out
    # the agent's ACTUAL tools are named (so it can survey its own equipment)
    assert "send_email" in out
    # the rule: gap -> escalate to manager, never invent
    assert "escalate that access request to your manager" in out
    assert "Inventing the result is the one thing you must never do" in out


def test_universal_even_with_no_authority_tools():
    # a plain agent still gets the rule + its baseline tools
    out = _fab()._grounded_work_context(SimpleNamespace(id="nobody"))
    assert "register_commitment" in out and "drink_coffee" in out
