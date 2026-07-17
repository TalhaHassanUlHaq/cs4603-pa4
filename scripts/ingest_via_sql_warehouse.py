"""Non-graded helper: run the Task 0.3 ingestion pipeline from a local machine.

`rag/ingest.py::build_chunks_table` is written to the assignment's required
signature (`build_chunks_table(spark, ...)`) and must run inside a Databricks
notebook/cluster with Spark. This script does the *equivalent* work purely through
REST APIs -- the Files API, the SQL Statement Execution API (which can call
`ai_parse_document` on a SQL Warehouse with no cluster at all), and
`databricks-vectorsearch` (also cluster-free) -- so ingestion can be kicked off from
a plain local `uv run python scripts/ingest_via_sql_warehouse.py` against a real
workspace.

Requires, in addition to the usual .env values:
    DATABRICKS_SQL_WAREHOUSE_ID=<warehouse id>
    UC_VOLUME=main.default.pa4   (a Unity Catalog volume that already exists)

Run:  uv run python scripts/ingest_via_sql_warehouse.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402

LOCAL_PDF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "annual_report.pdf"
)


def _run_sql(w, warehouse_id: str, statement: str, timeout_s: int = 300):
    from databricks.sdk.service.sql import StatementState

    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=statement, wait_timeout="30s"
    )
    deadline = time.monotonic() + timeout_s
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.monotonic() > deadline:
            raise TimeoutError(f"Statement timed out after {timeout_s}s:\n{statement}")
        time.sleep(2)
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"Statement failed ({resp.status.state}): {resp.status.error}\n{statement}")
    return resp


def _upload_pdf(w, volume_path: str) -> None:
    with open(LOCAL_PDF, "rb") as f:
        w.files.upload(volume_path, f, overwrite=True)
    print(f"Uploaded {LOCAL_PDF} -> {volume_path}")


def ingest() -> None:
    from databricks.sdk import WorkspaceClient

    settings = get_settings()
    warehouse_id = os.environ["DATABRICKS_SQL_WAREHOUSE_ID"]
    # UC_VOLUME must already exist (e.g. `main.default.pa4`) -- creating catalogs/
    # schemas/volumes is an admin operation left to the workspace admin, not this
    # script.
    volume = os.environ.get("UC_VOLUME", "main.default.pa4")
    source_table = os.environ["SOURCE_TABLE"]

    w = WorkspaceClient()

    volume_path = f"/Volumes/{volume.replace('.', '/')}/annual_report.pdf"
    _upload_pdf(w, volume_path)

    # ai_prep_search(ai_parse_document(content)) returns a VARIANT whose
    # document.contents array holds one struct per chunk (chunk_id,
    # chunk_to_retrieve, chunk_to_embed, pages[].page_id) -- this exact call shape
    # was verified directly against this workspace's SQL Warehouse (page_id is
    # 0-indexed, hence +1 to match the report's printed page numbers).
    print("Parsing + chunking with ai_parse_document + ai_prep_search ...")
    _run_sql(
        w,
        warehouse_id,
        f"""
        CREATE OR REPLACE TABLE {source_table} AS
        SELECT
            chunk:chunk_id::STRING AS chunk_id,
            chunk:chunk_to_retrieve::STRING AS chunk_to_retrieve,
            chunk:chunk_to_embed::STRING AS chunk_to_embed,
            element_at(split(source, '/'), -1) AS source,
            CAST(chunk:pages[0]:page_id AS INT) + 1 AS page
        FROM (
            SELECT ai_prep_search(ai_parse_document(content)) AS prepped, path AS source
            FROM READ_FILES('{volume_path}', format => 'binaryFile')
        ) parsed_docs
        LATERAL VIEW EXPLODE(CAST(prepped:document.contents AS ARRAY<VARIANT>)) AS chunk
        """,
    )

    _run_sql(
        w, warehouse_id, f"ALTER TABLE {source_table} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"
    )
    print(f"Chunk table ready: {source_table}")

    print("Creating Vector Search endpoint + Delta Sync index ...")
    from databricks.vector_search.client import VectorSearchClient

    vsc = VectorSearchClient()
    endpoint_name = settings["vs_endpoint"]
    index_name = settings["vs_index"]

    existing_endpoints = {e["name"] for e in vsc.list_endpoints().get("endpoints", [])}
    if endpoint_name not in existing_endpoints:
        vsc.create_endpoint(name=endpoint_name, endpoint_type="STANDARD")
        print(f"Created endpoint {endpoint_name}, waiting for it to come online...")

    vsc.create_delta_sync_index(
        endpoint_name=endpoint_name,
        index_name=index_name,
        primary_key="chunk_id",
        source_table_name=source_table,
        pipeline_type="TRIGGERED",
        embedding_source_column="chunk_to_retrieve",
        embedding_model_endpoint_name=settings["embeddings"],
    )
    print(f"Index {index_name} created. Poll `.describe()` until status.ready is True.")


if __name__ == "__main__":
    ingest()
