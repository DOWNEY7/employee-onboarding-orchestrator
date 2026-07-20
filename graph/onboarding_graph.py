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
    builder.add_node("planner",          planner_node)
    builder.add_node("provisioner",      provisioner_node)
    builder.add_node("comms",            comms_node)
    builder.add_node("human_gate",       human_gate_node)
    builder.add_node("auditor",          auditor_node)
    builder.add_node("max_rejections",   max_rejections_node)

    # Edges — linear flow to the human gate
    builder.add_edge(START,          "planner")
    builder.add_edge("planner",      "provisioner")
    builder.add_edge("provisioner",  "comms")
    builder.add_edge("comms",        "human_gate")

    # Conditional edge: after human gate, route by approval status
    builder.add_conditional_edges(
        "human_gate",
        _route,
        {
            "auditor":          "auditor",
            "planner":          "planner",
            "max_rejections":   "max_rejections",
        },
    )

    builder.add_edge("auditor",        END)
    builder.add_edge("max_rejections", END)

    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_gate"],  # pause BEFORE human_gate runs
    )

    logger.info("OnboardingGraph: compiled successfully")
    return graph, checkpointer
