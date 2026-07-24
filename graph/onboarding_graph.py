"""
graph/onboarding_graph.py
─────────────────────────────────────────────────────────────────────────────
LangGraph StateGraph for the employee onboarding workflow.

Graph topology:
    START → planner → provisioner → comms → human_gate ─┐
                                                         │ (rejected)
                ┌────────────────────────────────────────┘
                │                              ↑ (back to planner)
                │ (approved)
                ▼
            auditor → END

State is persisted to SQLite via SqliteSaver (or MemorySaver for tests).
The human_gate node uses LangGraph interrupt() — the graph PAUSES at this
node and resumes when Command(resume=approval_dict) is called by the API.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from agents.auditor import auditor_node
from agents.comms import comms_node
from agents.human_gate import human_gate_node, route_after_approval
from agents.planner import planner_node
from agents.provisioner import provisioner_node
from models.state import ApprovalStatus, OnboardingState

logger = logging.getLogger(__name__)

# ── Max-rejections fallback node ──────────────────────────────────────────────


def max_rejections_node(state: OnboardingState) -> dict:
    """
    Safety-valve node reached after 3 consecutive rejections.
    Sets an error so the API layer can surface this to the caller.
    """
    logger.error(
        "OnboardingGraph: max rejection iterations reached for run %s",
        state.get("run_id", "unknown"),
    )
    return {
        "error": (
            "Maximum review iterations (3) reached without approval. "
            "The onboarding pack has been escalated for manual review. "
            "Please contact the IT team directly."
        ),
        "approval_status": ApprovalStatus.REJECTED.value,
    }


# ── Error-sink node + routing helper ───────────────────────────────────────────────


def error_sink_node(state: OnboardingState) -> dict:
    """
    Terminal node reached when any agent sets state["error"] to a non-empty string.
    Logs the failure so it appears in structured logs and the /status response.
    No state update is needed — the error is already recorded.
    """
    error = state.get("error", "unknown error")
    run_id = state.get("run_id", "unknown")
    logger.error("OnboardingGraph: run %s terminated early with error: %s", run_id, error)
    return {}


def _error_or(next_node: str):
    """
    Factory returning a routing function that goes to ``error_sink`` when
    state["error"] is non-empty, otherwise proceeds to ``next_node``.
    """

    def _route(state: OnboardingState) -> str:
        return "error_sink" if state.get("error") else next_node

    _route.__name__ = f"_error_or_{next_node}"
    return _route


# ── Routing for human_gate → planner vs auditor ───────────────────────────────


def _route(state: OnboardingState) -> Literal["auditor", "planner", "max_rejections"]:
    return route_after_approval(state)  # type: ignore[return-value]


# ── Graph builder ─────────────────────────────────────────────────────────────


def build_onboarding_graph(
    checkpointer=None,
    db_path: str | None = None,
) -> tuple:
    """
    Build and compile the onboarding StateGraph.

    Args:
        checkpointer: An explicit LangGraph checkpointer to use. If None, uses
                      SqliteSaver (db_path) or MemorySaver as fallback.
        db_path:      Path to the SQLite database file. Defaults to the
                      DATABASE_URL env var or "./onboarding.db".

    Returns:
        (compiled_graph, checkpointer) — the checkpointer is returned so
        callers can manage its lifecycle (e.g., closing the connection).
    """
    # ── Set up checkpointer ───────────────────────────────────────────────────
    if checkpointer is None:
        if db_path is None:
            raw_url = os.getenv("DATABASE_URL", "sqlite:///./onboarding.db")
            db_path = raw_url.replace("sqlite:///", "")

        try:
            import sqlite3

            conn = sqlite3.connect(db_path, check_same_thread=False)
            checkpointer = SqliteSaver(conn)
            checkpointer.setup()
            logger.info("OnboardingGraph: using SqliteSaver at %s", db_path)
        except Exception as exc:
            logger.warning("OnboardingGraph: SqliteSaver unavailable (%s) — using MemorySaver", exc)
            checkpointer = MemorySaver()

    # ── Build graph ───────────────────────────────────────────────────────────
    builder = StateGraph(OnboardingState)

    # Nodes
    builder.add_node("planner", planner_node)
    builder.add_node("provisioner", provisioner_node)
    builder.add_node("comms", comms_node)
    builder.add_node("human_gate", human_gate_node)
    builder.add_node("auditor", auditor_node)
    builder.add_node("max_rejections", max_rejections_node)
    builder.add_node("error_sink", error_sink_node)

    # Linear flow to the human gate, with error-routing after each agent node
    builder.add_edge(START, "planner")
    builder.add_conditional_edges(
        "planner",
        _error_or("provisioner"),
        {"provisioner": "provisioner", "error_sink": "error_sink"},
    )
    builder.add_conditional_edges(
        "provisioner",
        _error_or("comms"),
        {"comms": "comms", "error_sink": "error_sink"},
    )
    builder.add_conditional_edges(
        "comms",
        _error_or("human_gate"),
        {"human_gate": "human_gate", "error_sink": "error_sink"},
    )

    # Conditional edge: after human gate, route by approval status
    builder.add_conditional_edges(
        "human_gate",
        _route,
        {
            "auditor": "auditor",
            "planner": "planner",
            "max_rejections": "max_rejections",
        },
    )

    builder.add_edge("auditor", END)
    builder.add_edge("max_rejections", END)
    builder.add_edge("error_sink", END)

    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_gate"],  # pause BEFORE human_gate runs
    )

    logger.info("OnboardingGraph: compiled successfully")
    return graph, checkpointer
