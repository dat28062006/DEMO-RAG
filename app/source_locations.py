"""Display source headings from original text without altering indexed offsets."""

import re

HEADINGS = re.compile(
    r"(?m)^(?:#{1,6}[ \t]+(?P<markdown>\S[^\n]*)|"
    r"(?P<numbered>\d{1,2}(?:\.\d{1,2})*[.)]?[ \t]+[A-ZĐ][^\n]{1,99})|"
    r"(?P<references>References|Bibliography|Tài liệu tham khảo))\s*$"
)


def source_location(doc, offset, fallback=None):
    heading = None
    for match in HEADINGS.finditer(doc["section_text"]):
        if match.start() > offset:
            break
        heading = match.group("markdown") or match.group("numbered") or match.group("references")
    return heading or fallback or doc["section"]


def exact_sections(question, docs):
    """Expand a literal section heading to its bounded body in permitted files."""
    from .document_answers import title_key

    query = title_key(question)
    parts = []
    for doc in docs:
        headings = list(HEADINGS.finditer(doc["section_text"]))
        for index, match in enumerate(headings):
            title = match.group("markdown") or match.group("numbered") or match.group("references")
            phrase = title_key(re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", title))
            if len(phrase) < 4 or not re.search(r"\b" + re.escape(phrase) + r"\b", query):
                continue
            start = match.start()
            end = (
                headings[index + 1].start()
                if index + 1 < len(headings)
                else len(doc["section_text"])
            )
            text = doc["section_text"][start:end].strip()
            if len(text) <= len(match.group()) or len(text) > 5000:
                continue
            parts.append(
                {
                    "id": doc["id"] + ":section:" + str(start),
                    "doc": doc,
                    "title": title,
                    "section_text": text,
                    "offset": start,
                    "line_start": doc["section_text"].count("\n", 0, start) + 1,
                    "line_end": doc["section_text"].count("\n", 0, start + len(text)) + 1,
                    "selection_method": "exact_section_heading",
                }
            )
    return parts
