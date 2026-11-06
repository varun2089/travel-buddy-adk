import argparse
import json
from pathlib import Path

from .chunking import chunk_markdown
from .config import MAX_DOCUMENT_BYTES
from .embeddings import embed_text


def main():
    parser = argparse.ArgumentParser(description="Ingest a synthetic policy explicitly; calls Gemini embeddings.")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    factory = None
    try:
        with args.path.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("Policy file exceeds the size limit.")
        sections = chunk_markdown(data.decode("utf-8"))
        from travel_agent.db.policy_repository import create_policy_session_factory, ingest_sections

        factory = create_policy_session_factory()
        result = ingest_sections(factory, sections, args.path.name, embed_text)
    except Exception:
        print(json.dumps({"status": "error", "message": "Ingestion failed; check dependencies, configuration and document. Existing versions must remain unchanged."}))
        return 1
    finally:
        if factory is not None:
            factory.kw["bind"].dispose()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
