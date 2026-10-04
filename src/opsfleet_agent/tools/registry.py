"""Per-role tool allowlist (HLD §4.2, §6). Default deny: an unknown role gets no tools.

``run_sql`` is enabled from iteration 14a, when the graph wires it with the per-turn budget and
the output guard. ``RUN_SQL_ENABLED`` is the single kill switch: set it to False to take
``run_sql`` away from every role at once (rollback rule). It has no per-call override.
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

RUN_SQL_ENABLED: Final = True

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
