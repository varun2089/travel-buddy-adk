from dataclasses import dataclass
from hashlib import sha256
import re

from .config import LABEL, MAX_EMBEDDING_BYTES, MAX_DOCUMENT_BYTES, MAX_SECTION_CHARS, MAX_SECTIONS, identifier


@dataclass(frozen=True)
class Section:
    document_id: str
    version: str
    section_id: str
    heading: str
    text: str
    content_hash: str


def chunk_markdown(source):
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("Policy document exceeds the size limit.")
    lines = source.splitlines()
    if len(lines) < 5 or lines[0] != "# " + LABEL:
        raise ValueError("The synthetic policy label is required.")
    if not lines[1].startswith("Document-ID: ") or not lines[2].startswith("Version: "):
        raise ValueError("Document-ID and Version are required.")
    document_id = identifier(lines[1][13:])
    version = identifier(lines[2][9:])
    sections = []
    seen = set()
    current = None
    body = []

    def finish():
        if current is None:
            return
        section_id, heading = current
        text = "\n".join(body).strip()
        if not text or len(text) > MAX_SECTION_CHARS:
            raise ValueError("Sections must contain 1–4000 characters.")
        if len((heading + "\n" + text).encode("utf-8")) > MAX_EMBEDDING_BYTES:
            raise ValueError("Section heading and body exceed the 1800-byte embedding limit.")
        digest = sha256((heading + "\n" + text).encode()).hexdigest()
        sections.append(Section(document_id, version, section_id, heading, text, digest))

    for line in lines[3:]:
        if line.startswith("## "):
            finish()
            match = re.fullmatch(r"## ([A-Za-z0-9_.-]+) \| (.+)", line)
            if not match:
                raise ValueError("Use section headings: ## SECTION-ID | Heading")
            section_id = identifier(match[1])
            heading = match[2].strip()
            if section_id in seen or not heading or len(heading) > 160:
                raise ValueError("Section IDs must be unique and headings limited to 160 characters.")
            seen.add(section_id)
            current = (section_id, heading)
            body = []
        elif current is not None:
            body.append(line)
        elif line.strip():
            raise ValueError("Place policy text inside sections.")
    finish()
    if not 1 <= len(sections) <= MAX_SECTIONS:
        raise ValueError("Provide between 1 and 24 sections.")
    return sections
