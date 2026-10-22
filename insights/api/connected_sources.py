"""Member-bound connected reads stored in the local warehouse."""
import hashlib
import json
import re
from urllib.parse import urlsplit

import frappe
import ibis
import pandas as pd
from frappe.utils import get_url, now_datetime

from insights.decorators import insights_whitelist
from insights.pramaan import PramaanError, get_client

MAX_ROWS = 1000
MAX_BYTES = 50000


def platform(method, **kwargs):
    try:
        return getattr(get_client(), method)(**kwargs)
    except PramaanError as error:
        frappe.throw(str(error))


def member_ref(user):
    return hashlib.sha256((frappe.local.site + ":" + user).encode()).hexdigest()


def validate_source(doc):
    if not doc.connected_app or not re.fullmatch(r"[a-z0-9_-]{1,64}", doc.connected_app):
        frappe.throw("Choose an app from the catalogue.")
    path = doc.connected_path or ""
    parsed = urlsplit(path)
    if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or len(path) > 1024 or "\\" in path:
        frappe.throw("Enter a reviewed relative read path without query parameters.")
    if doc.is_new():
        doc.connected_user = frappe.session.user
    else:
        previous = doc.get_doc_before_save()
        if previous and (previous.type != "Connected App" or previous.connected_user != frappe.session.user):
            frappe.throw("Only the connected member can change this source.", frappe.PermissionError)
        doc.connected_user = previous.connected_user if previous else doc.connected_user
    doc.database_type = "DuckDB"
    doc.database_name = None
    from insights.insights.doctype.insights_data_source_v3.data_warehouse import get_warehouse_schema_name
    doc.schema = get_warehouse_schema_name(doc.name)


def checked_rows(result, key):
    if result.get("status") != 200 or result.get("truncated") or result.get("bodyOmitted"):
        frappe.throw("The read did not return complete data. Stored rows were kept.")
    body = result.get("body")
    if len(json.dumps(body).encode()) > MAX_BYTES:
        frappe.throw("The read is too large. Stored rows were kept.")
    rows = body.get(key) if key and isinstance(body, dict) else body
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        frappe.throw("Choose a rows field containing at most 1000 records.")
    if any(not isinstance(k, str) or not k or len(k) > 128 or isinstance(v, (dict, list)) for row in rows for k, v in row.items()):
        frappe.throw("Records must contain named columns with plain values.")
    return rows


@insights_whitelist(role="Insights Admin", methods=["POST"])
def search(query: str):
    result = platform("search_catalogue", query=query, limit=20)
    return [{"app": r["app"], "description": r.get("description", "")} for r in result.get("results", []) if r.get("readOnly") is True][:20]


@insights_whitelist(role="Insights Admin", methods=["POST"])
def connect(app: str):
    result = platform("start_app_connection", app=app, end_user_ref=member_ref(frappe.session.user),
        redirect_url=get_url("/insights/data-source", allow_header_override=False))
    address = result.get("connectUrl", "")
    if urlsplit(address).scheme != "https":
        frappe.throw("Connected sources are not available yet.")
    return {"connectUrl": address}


@insights_whitelist(role="Insights Admin", methods=["POST"])
def connections():
    result = platform("connected_app_connections", end_user_ref=member_ref(frappe.session.user))
    return [{"app": row["app"], "status": row["status"]} for row in result.get("results", [])][:100]


@insights_whitelist(role="Insights Admin", methods=["POST"])
def create(title: str, app: str, path: str, rows_key: str = ""):
    source = frappe.get_doc(dict(doctype="Insights Data Source v3", title=title, type="Connected App",
        connected_app=app, connected_path=path, connected_rows_key=rows_key)).insert()
    sync_source(source)
    return source.name


def sync_source(source):
    user = source.connected_user
    if not user or not frappe.db.get_value("User", user, "enabled"):
        frappe.throw("The connected member is disabled.", frappe.PermissionError)
    previous_user = frappe.session.user
    try:
        frappe.set_user(user)
        source.check_permission("write")
        result = platform("read_app", app=source.connected_app, end_user_ref=member_ref(user), path=source.connected_path)
        rows = checked_rows(result, source.connected_rows_key)
        import insights
        from insights.insights.doctype.insights_data_source_v3.data_warehouse import WarehouseTableWriter
        from insights.insights.doctype.insights_table_v3.insights_table_v3 import InsightsTablev3
        from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import after_request
        after_request()
        if rows:
            frame = pd.DataFrame(rows)
            with WarehouseTableWriter("records", ibis.memtable(frame).schema(), database=source.schema) as writer:
                writer.insert(frame)
            InsightsTablev3.bulk_create(source.name, ["records"])
            from insights.insights.doctype.insights_table_v3.insights_table_v3 import store_columns
            store_columns(source.name, "records", ibis.memtable(frame).schema())
        else:
            # Empty snapshots clear old rows but retain the known column schema.
            with insights.warehouse.get_write_connection() as db:
                if source.schema in db.list_databases() and "records" in db.list_tables(database=source.schema):
                    db.raw_sql(f'DELETE FROM "{source.schema}"."records"')
        table_name = frappe.db.get_value("Insights Table v3", {"data_source": source.name, "table": "records"}, "name")
        if table_name:
            values = {"stored": 1, "last_synced_on": now_datetime()}
            if not frappe.db.get_value("Insights Table v3", table_name, "sync_schedule"):
                values["sync_schedule"] = "0 * * * *"
            frappe.db.set_value("Insights Table v3", table_name, values)
        source.db_set("connected_last_sync", now_datetime())
    finally:
        frappe.set_user(previous_user)


@insights_whitelist(role="Insights Admin", methods=["POST"])
def sync(data_source: str):
    source = frappe.get_doc("Insights Data Source v3", data_source)
    source.check_permission("write")
    if source.type != "Connected App" or source.connected_user != frappe.session.user:
        frappe.throw("Only the connected member can refresh this source.", frappe.PermissionError)
    sync_source(source)


def sync_connected_sources():
    for name in frappe.get_all("Insights Data Source v3", filters={"type": "Connected App", "status": "Active"}, pluck="name"):
        try:
            sync_source(frappe.get_doc("Insights Data Source v3", name))
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="Connected source refresh failed", message="Refresh did not complete. Check the connection and reviewed read path.")
