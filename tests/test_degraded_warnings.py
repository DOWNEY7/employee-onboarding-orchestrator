"""
tests/test_degraded_warnings.py
─────────────────────────────────────────────────────────────────────────────
Regression tests for non-fatal LLM failure handling in PlannerAgent and
CommsAgent. Both agents catch LLM errors and fall back to placeholder/base
content so a transient LLM hiccup doesn't kill the whole run — but that
fallback must be visible in state["warnings"], not silently swallowed.
"""

from unittest.mock import patch

from agents.comms import comms_node, leaver_comms_node
from agents.planner import leaver_planner_node, planner_node

STARTER_FORM = {
    "first_name": "Alice",
    "last_name": "Smith",
    "role": "Software Engineer",
    "department": "Engineering",
    "start_date": "2026-08-01",
    "manager_email": "john.doe@contoso.com",
    "email_prefix": "alice.smith",
    "domain": "contoso.com",
    "licence_sku": "E3",
    "location": "UK",
}

LEAVER_FORM = {
    "first_name": "John",
    "last_name": "Doe",
    "email": "john.doe@contoso.com",
    "last_working_day": "2026-08-30",
    "manager_email": "sarah.jones@contoso.com",
    "reason": "Resignation",
    "hardware_return_required": True,
}


class TestPlannerDegradedFallback:
    def test_onboarding_llm_failure_falls_back_and_warns(self):
        with patch("agents.planner.get_llm", side_effect=RuntimeError("LLM unavailable")):
            result = planner_node({"form": STARTER_FORM, "iteration": 0})

        assert result["checklist"]  # base matrix checklist still produced
        assert result["error"] == ""
        assert any("LLM enrichment failed" in w for w in result["warnings"])

    def test_leaver_llm_failure_falls_back_and_warns(self):
        with patch("agents.planner.get_llm", side_effect=RuntimeError("LLM unavailable")):
            result = leaver_planner_node({"form": LEAVER_FORM})

        assert result["checklist"]
        assert any("LLM enrichment failed" in w for w in result["warnings"])

    def test_llm_invoke_failure_produces_exactly_one_warning(self):
        # get_llm itself succeeds but the mocked model raises on .invoke —
        # exercises the same except branch without special-casing get_llm.
        mock_llm = patch("agents.planner.get_llm").start()
        mock_llm.return_value.invoke.side_effect = RuntimeError("boom")
        try:
            result = planner_node({"form": STARTER_FORM, "iteration": 0})
        finally:
            patch.stopall()

        assert result["warnings"] == [
            "PlannerAgent: LLM enrichment failed — checklist generated from the base "
            "role/department matrix only, without LLM-tailored additions."
        ]


class TestCommsDegradedFallback:
    def test_onboarding_comms_llm_failure_falls_back_and_warns(self):
        with patch("agents.comms.get_llm", side_effect=RuntimeError("LLM unavailable")):
            result = comms_node({"form": STARTER_FORM, "checklist": []})

        assert result["comms"]["welcome_email_html"]
        assert result["error"] == ""
        assert len(result["warnings"]) == 2  # welcome email + day1 guide both degraded

    def test_leaver_comms_llm_failure_falls_back_and_warns(self):
        with patch("agents.comms.get_llm", side_effect=RuntimeError("LLM unavailable")):
            result = leaver_comms_node({"form": LEAVER_FORM})

        assert result["comms"]["welcome_email_html"]
        assert any("leaver handover drafting failed" in w for w in result["warnings"])

    def test_warnings_from_prior_node_are_preserved(self):
        with patch("agents.comms.get_llm", side_effect=RuntimeError("LLM unavailable")):
            result = comms_node(
                {
                    "form": STARTER_FORM,
                    "checklist": [],
                    "warnings": ["PlannerAgent: LLM enrichment failed — using base checklist."],
                }
            )

        assert len(result["warnings"]) == 3
        assert result["warnings"][0].startswith("PlannerAgent")
