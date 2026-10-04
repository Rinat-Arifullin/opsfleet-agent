"""Per-role tool allowlist (HLD §4.2, §6). Default deny: an unknown role gets no tools.

``run_sql`` is disabled until the owner passes the iteration 13 🔴 gate (rollback rule: it
stays unregistered unless every named test is green and the gate is approved). Flip
``RUN_SQL_ENABLED`` only as part of that approval.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

__all__ = [
    "LIBRARY_TOOLS",
    "READ_TOOLS",
    "RUN_SQL",
    "RUN_SQL_ENABLED",
    "TOOLS_BY_ROLE",
    "tools_for",
]

RUN_SQL: Final = "run_sql"
READ_TOOLS: Final = frozenset({"list_tables", "get_schema"})
LIBRARY_TOOLS: Final = frozenset(  # HLD §4.2 role table
    {
        "save_report",
        "list_reports",
        "search_reports",
        "view_report",
        "rename_report",
        "export_report",
        "delete_reports",
        "set_preference",
    }
)

RUN_SQL_ENABLED: Final = False

TOOLS_BY_ROLE: Final[Mapping[str, frozenset[str]]] = {
    "quick_analyst": READ_TOOLS | {RUN_SQL},
    "deep_analyst": READ_TOOLS | {RUN_SQL},
    "library_agent": LIBRARY_TOOLS,
    # router, light_path, report_writer, report_verifier, summary, judge, fallback: no tools
}


def tools_for(role: str) -> frozenset[str]:
    """The tools ``role`` may call. ``run_sql`` is removed while it is disabled.

    No override argument: the only switch is the module constant, read at call time.
    """
    tools = TOOLS_BY_ROLE.get(role, frozenset())
    if not RUN_SQL_ENABLED:
        tools = tools - {RUN_SQL}
    return frozenset(tools)
