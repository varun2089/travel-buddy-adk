import json
import math
import sys

from .config import DIMENSIONS, MAX_EMBEDDING_BYTES, MAX_QUERY_CHARS, MAX_SECTION_CHARS, MODEL


class EmbeddingFailure(Exception):
    pass


def normalize_vector(values):
    try:
        vector = [float(value) for value in values]
        if len(vector) != DIMENSIONS or not all(math.isfinite(value) for value in vector):
            raise ValueError
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError
        return [value / norm for value in vector]
    except (TypeError, ValueError, OverflowError):
        raise EmbeddingFailure("Invalid embedding vector.") from None


def embed_text(text, task_type):
    from google import genai
    from google.genai.errors import APIError
    from google.genai import types

    if task_type not in {"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"}:
        raise ValueError("Unsupported embedding task.")
    limit = MAX_QUERY_CHARS if task_type == "RETRIEVAL_QUERY" else MAX_SECTION_CHARS + 200
    if not isinstance(text, str) or not text.strip() or len(text) > limit or len(text.encode("utf-8")) > MAX_EMBEDDING_BYTES:
        raise ValueError("Invalid embedding input.")
    try:
        with genai.Client(vertexai=True, project="gen-lang-client-0842015305",
                          location="us-central1", http_options=types.HttpOptions(
            timeout=10000,
            retry_options=types.HttpRetryOptions(attempts=2, initial_delay=0.5,
                                                max_delay=1, jitter=0,
                                                http_status_codes=[429, 500, 502, 503, 504]),
        )) as client:
            response = client.models.embed_content(
                model=MODEL, contents=text,
                config=types.EmbedContentConfig(task_type=task_type,
                                               output_dimensionality=DIMENSIONS),
            )
        if not response.embeddings or len(response.embeddings) != 1:
            raise EmbeddingFailure("Missing embedding.")
        return normalize_vector(response.embeddings[0].values)
    except Exception as error:
        status = error.code if isinstance(error, APIError) else None
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        print(json.dumps({"exception_type": type(error).__name__, "http_status": status}),
              file=sys.stderr, flush=True)
        raise EmbeddingFailure("Embedding service unavailable or returned an invalid vector.") from None
