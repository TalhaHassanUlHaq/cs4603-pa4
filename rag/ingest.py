"""Corpus ingestion into Databricks Vector Search (Task 0.3 / rag/ingest.py).

Run inside a Databricks notebook (needs Spark + the `ai_parse_document` /
`ai_prep_search` SQL AI functions). Mirrors PA2 Part 1:

    from rag.ingest import build_chunks_table, create_index
    build_chunks_table(spark, "/Volumes/main/default/pa4/annual_report.pdf",
                        "main.default.<name>_analyst_chunks")
    create_index()

There is no Spark session available outside a Databricks notebook/cluster, so this
module cannot be exercised from a plain local Python process. For running the
equivalent ingestion from a local machine against a real workspace (no notebook, no
cluster -- only a SQL Warehouse), see `scripts/ingest_via_sql_warehouse.py`.
"""

from __future__ import annotations

from config import get_settings


def build_chunks_table(spark, volume_path: str, chunks_table: str) -> None:
    """Parse `volume_path` with `ai_parse_document` and chunk it with
    `ai_prep_search` into the Delta table `chunks_table`
    (chunk_id, chunk_to_retrieve, chunk_to_embed, source, page), with Change Data
    Feed enabled so the Vector Search Delta Sync index can track updates.

    `ai_prep_search(ai_parse_document(content))` returns a VARIANT whose
    `document.contents` array holds one struct per chunk
    (chunk_id, chunk_to_retrieve, chunk_to_embed, pages[].page_id) -- this exact
    call shape was verified against a real Databricks SQL Warehouse (0-indexed
    `page_id`, so `+1` below matches the report's printed page numbers).
    """
    spark.sql(f"""
        CREATE OR REPLACE TABLE {chunks_table}
        USING DELTA AS
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
    """)

    spark.sql(
        f"ALTER TABLE {chunks_table} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"
    )


def create_index() -> None:
    """Create a STANDARD Vector Search endpoint and a TRIGGERED Delta Sync index
    over the Delta table produced by `build_chunks_table`, reading endpoint/index/
    source-table names from `config.get_settings()` + `SOURCE_TABLE` env var."""
    import os

    from databricks.vector_search.client import VectorSearchClient

    settings = get_settings()
    source_table = os.environ["SOURCE_TABLE"]
    endpoint_name = settings["vs_endpoint"]
    index_name = settings["vs_index"]
    embedding_endpoint = settings["embeddings"]

    client = VectorSearchClient()

    existing_endpoints = {e["name"] for e in client.list_endpoints().get("endpoints", [])}
    if endpoint_name not in existing_endpoints:
        client.create_endpoint(name=endpoint_name, endpoint_type="STANDARD")

    client.create_delta_sync_index(
        endpoint_name=endpoint_name,
        index_name=index_name,
        primary_key="chunk_id",
        source_table_name=source_table,
        pipeline_type="TRIGGERED",
        embedding_source_column="chunk_to_retrieve",
        embedding_model_endpoint_name=embedding_endpoint,
    )
