# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Ask-your-data drafts, written from the site's own schema.

A draft is a suggestion shown to the person who asked for it. Nothing here
runs a draft, saves it or sends it anywhere: the editor is where the text
lands, and only the reader's own actions do the rest.

Every draft rides the single client. Schema context is bounded, so a question
cannot ship the whole warehouse to the platform.
"""

import frappe
from frappe import _

from insights.api.data_sources import get_data_source_tables, get_permitted_ibis_table
from insights.decorators import insights_whitelist
from insights.insights.doctype.insights_data_source_v3.ibis_utils import get_columns_from_schema
from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import (
    db_type_to_sqlglot_dialect,
)
from insights.insights.doctype.insights_table_v3.insights_table_v3 import get_stored_columns
from insights.insights.doctype.insights_team.insights_team import check_data_source_permission
from insights.pramaan import IncompleteGeneration, PramaanError, get_client

MAX_TABLES = 12
MAX_COLUMNS_PER_TABLE = 20
MAX_QUESTION = 2000
MAX_DIALECT_NOTE = {"mysql": "MySQL", "postgres": "PostgreSQL", "sqlite": "SQLite", "duckdb": "DuckDB"}


def _schema_context(data_source: str) -> str:
    """A bounded, permission-checked view of what the source holds."""
    stored = get_stored_columns(data_source=data_source)
    # New and older demo sources may predate the stored-column cache. Inspect
    # only the bounded set of tables, through the query builder's permissions.
    tables = list(stored.values())[:MAX_TABLES]
    if len(tables) < MAX_TABLES:
        for table in get_data_source_tables(data_source, limit=MAX_TABLES):
            if table.name in stored:
                continue
            try:
                schema = get_permitted_ibis_table(data_source, table.table_name).schema()
            except Exception:
                continue  # an unreadable or unavailable table contributes no schema
            tables.append(frappe._dict(table=table.table_name, columns=get_columns_from_schema(schema)))
            if len(tables) == MAX_TABLES:
                break
    lines = []
    for table_doc in tables:
        columns = [c for c in (table_doc.columns or []) if isinstance(c, dict) and c.get("name")]
        shown = ", ".join(
            f"{c['name']} ({c.get('type') or 'unknown'})" for c in columns[:MAX_COLUMNS_PER_TABLE]
        )
        if not columns:
            continue
        note = "" if len(columns) <= MAX_COLUMNS_PER_TABLE else _("first {0} columns").format(MAX_COLUMNS_PER_TABLE)
        lines.append(f"table {table_doc.table}: {shown}{(' — ' + note) if note else ''}")
    return "\n".join(lines)


@insights_whitelist(methods=["POST"])
def draft_sql(data_source: str, question: str) -> dict:
    """Draft a SQL query for the question, over tables the caller may read.

    Returns the draft text and whether it is complete. The caller shows it in
    the editor; this endpoint never runs it.
    """
    question = (question or "").strip()[:MAX_QUESTION]
    if not question:
        frappe.throw(_("Ask a question first"))
    check_data_source_permission(data_source)

    source = frappe.db.get_value(
        "Insights Data Source v3", data_source, ["database_type", "title"], as_dict=True
    )
    if not source:
        frappe.throw(_("Unknown data source"))

    dialect = MAX_DIALECT_NOTE.get(db_type_to_sqlglot_dialect(source.database_type) or "", source.database_type)
    schema = _schema_context(data_source)
    if not schema:
        frappe.throw(_("This source has no stored tables to draft from yet. Sync its tables first."))

    system = (
        "You write one SQL query for a reporting tool. Answer with the query only: no prose, "
        f"no markdown fences. The source is a {dialect} database. Use only the tables and "
        "columns listed. If the question cannot be answered with them, answer with the query "
        "that comes closest and add a short SQL comment saying what is missing."
    )
    user = f"Tables and columns:\n{schema}\n\nQuestion: {question}"

    try:
        result = get_client().draft_text(system=system, user=user, max_tokens=1024)
    except IncompleteGeneration as e:
        raise frappe.throw(str(e.args[0])) from None
    except PramaanError as e:
        raise frappe.throw(str(e.args[0]) if e.args else _("The draft could not be written")) from None

    return {
        "sql": (result.get("text") or "").strip(),
        "complete": result.get("finishReason") == "stop",
    }


@insights_whitelist(methods=["POST"])
def draft_title(kind: str, text: str) -> dict:
    """Suggest a short title for a query or chart from what it shows."""
    text = (text or "").strip()[:MAX_QUESTION]
    if not text:
        frappe.throw(_("Nothing to title yet"))
    if kind not in ("query", "chart", "dashboard"):
        frappe.throw(_("Unknown kind"))

    system = (
        "You write one short title (at most six words) for a reporting object. "
        "Answer with the title only: no quotes, no trailing period, no markdown."
    )
    user = f"Kind: {kind}\nWhat it shows:\n{text}"

    try:
        result = get_client().draft_text(system=system, user=user, max_tokens=64)
    except IncompleteGeneration as e:
        raise frappe.throw(str(e.args[0])) from None
    except PramaanError as e:
        raise frappe.throw(str(e.args[0]) if e.args else _("The draft could not be written")) from None

    title = " ".join((result.get("text") or "").split())
    return {"title": title[:120], "complete": result.get("finishReason") == "stop"}
