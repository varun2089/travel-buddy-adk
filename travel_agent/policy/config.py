import re

MODEL = "gemini-embedding-001"
DIMENSIONS = 768
MAX_SECTION_CHARS = 4000
MAX_EMBEDDING_BYTES = 1800
MAX_DOCUMENT_BYTES = 100000
MAX_SECTIONS = 24
MAX_QUERY_CHARS = 1000
TOP_K = 3
MAX_DISTANCE = 0.45
LABEL = "Synthetic training policy — not Monash policy"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("Policy identifiers must contain 1–64 letters, digits, dots, underscores or hyphens.")
    return value

