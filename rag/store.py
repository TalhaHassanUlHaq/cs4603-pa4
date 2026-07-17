"""Vector Search retriever factory (Task 1.4 support / rag/store.py).

Returns a LangChain retriever over the Databricks Vector Search index built by
`ingest.py`, using `DatabricksVectorSearch` from `databricks_langchain`. This exact
retriever is reused by the deployed model (Part 2) — no separate embedding path.
"""

from __future__ import annotations

from functools import lru_cache

from config import get_settings

TEXT_COLUMN = "chunk_to_retrieve"  # the index's embedding_source_column (see rag/ingest.py)
CITATION_COLUMNS = ["chunk_id", "source", "page"]


def _require_vs_settings() -> dict[str, str]:
    settings = get_settings()
    if not settings["vs_endpoint"] or not settings["vs_index"]:
        raise OSError(
            "Missing VECTOR_SEARCH_ENDPOINT / VECTOR_SEARCH_INDEX. Set them in your "
            ".env (local) or the endpoint's environment_vars (deployed) — see "
            "Task 0.3 / rag/ingest.py."
        )
    return settings


@lru_cache(maxsize=1)
def get_vector_store():
    """Return a `DatabricksVectorSearch` handle over the Task 0.3 index.

    `text_column` is deliberately omitted: a Delta Sync index already has its
    embedding source column (`chunk_to_retrieve`, set at index-creation time in
    rag/ingest.py) baked into the index metadata, and DatabricksVectorSearch raises
    if you pass a redundant/explicit `text_column` on top of that.
    """
    from databricks_langchain import DatabricksVectorSearch

    settings = _require_vs_settings()
    return DatabricksVectorSearch(
        endpoint=settings["vs_endpoint"],
        index_name=settings["vs_index"],
        columns=CITATION_COLUMNS,
    )


def get_retriever(k: int = 4):
    """Return a top-k retriever over the Vector Search index."""
    return get_vector_store().as_retriever(search_kwargs={"k": k})
