"""RAG agent node (Task 1.4) — retrieves from Databricks Vector Search."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from agent.prompts import RAG_EXTRACT_PROMPT
from agent.state import AnalystState

NOT_FOUND = "not found in documents"


def _doc_source(doc) -> str:
    metadata = getattr(doc, "metadata", None) or {}
    source = metadata.get("source", "unknown source")
    page = metadata.get("page")
    return f"{source}, p.{page}" if page not in (None, "") else str(source)


def format_docs(docs) -> str:
    if not docs:
        return ""
    lines = []
    for doc in docs:
        content = getattr(doc, "page_content", None)
        if content is None:
            content = str(doc)
        lines.append(f"- {content.strip()} [source: {_doc_source(doc)}]")
    return "\n".join(lines)


def make_rag_agent(retriever, llm):
    def rag_agent(state: AnalystState) -> dict:
        idx = state.get("current_step_index", 0)
        step = state["plan"][idx]
        prior_results = state.get("step_results", [])

        try:
            docs = retriever.invoke(step)
        except AttributeError:
            docs = retriever.get_relevant_documents(step)
        except Exception:
            docs = []

        if not docs:
            fact = NOT_FOUND
        else:
            context = format_docs(docs)
            prior_context = ""
            if prior_results:
                prior_context = "Results of earlier steps:\n" + "\n".join(
                    f"{i + 1}. {r}" for i, r in enumerate(prior_results)
                ) + "\n\n"
            try:
                response = llm.invoke(
                    [
                        SystemMessage(content=RAG_EXTRACT_PROMPT),
                        HumanMessage(
                            content=f"{prior_context}Step: {step}\n\nRetrieved excerpts:\n{context}"
                        ),
                    ]
                )
                fact = getattr(response, "content", str(response)).strip() or NOT_FOUND
            except Exception as exc:
                fact = f"Error extracting fact: {exc}"

        step_results = state.get("step_results", []) + [fact]
        return {"step_results": step_results, "current_step_index": idx + 1}

    return rag_agent
