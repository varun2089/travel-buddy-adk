import math

from travel_agent.policy.config import (
    LABEL, MAX_EMBEDDING_BYTES, MAX_DISTANCE, MAX_QUERY_CHARS, MAX_SECTION_CHARS, TOP_K, identifier,
)
from travel_agent.policy.embeddings import EmbeddingFailure, embed_text


def failure(code, message):
    return {"status": "error", "error": {"code": code, "message": message}, "retryable": False}


def search_travel_policy(query: str) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS or len(query.encode("utf-8")) > MAX_EMBEDDING_BYTES:
        return failure("invalid_query", "Provide a policy question of 1–1000 characters and at most 1800 UTF-8 bytes.")
    factory = None
    try:
        from travel_agent.db.policy_repository import create_policy_session_factory, has_active_policies, nearest_sections

        factory = create_policy_session_factory()
        if not has_active_policies(factory):
            return no_evidence()
        vector = embed_text(query.strip(), "RETRIEVAL_QUERY")
        rows = nearest_sections(factory, vector)
        evidence = []
        for row in rows[:TOP_K]:
            document_id = identifier(row["document_id"])
            version = identifier(row["version"])
            if (not math.isfinite(row["distance"]) or not 0 <= row["distance"] <= 2.000001
                    or not isinstance(row["text"], str) or not 1 <= len(row["text"]) <= MAX_SECTION_CHARS
                    or not isinstance(row["heading"], str) or not 1 <= len(row["heading"]) <= 160):
                raise ValueError("Invalid retrieved evidence.")
            identifier(row["section_id"])
            if row["distance"] <= MAX_DISTANCE:
                evidence.append(dict(row, citation=f"[{document_id}/{version}/{row['section_id']}]"))
        if not evidence:
            return no_evidence()
        return {"status": "ok", "source": LABEL, "evidence_is_untrusted": True,
                "evidence": evidence}
    except EmbeddingFailure:
        return failure("policy_embedding_error", "Policy retrieval failed because embeddings are unavailable.")
    except Exception:
        return failure("policy_retrieval_error", "Policy retrieval failed. No policy answer can be established.")
    finally:
        if factory is not None:
            factory.kw["bind"].dispose()


def no_evidence():
    return {"status": "no_evidence", "evidence": [], "source": LABEL,
            "message": "No sufficiently relevant evidence in active policies. Do not infer a policy answer."}
