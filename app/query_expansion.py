"""Translate retrieval terms only when the equivalent occurs in permitted sources.

No answer facts, permissions or numeric values are supplied by this glossary.
Quoted/literal queries remain untouched. The original question is kept in the audit.
"""

import re
import unicodedata

ALIASES = {
    "hoc suat": "learning rate",
    "toc do hoc": "learning rate",
    "he so hoc": "learning rate",
    "ha gradient": "gradient descent",
    "lan truyen nguoc": "backpropagation",
    "chuan hoa theo lo": "batch normalization",
    "chuan hoa lo": "batch normalization",
    "dung huan luyen som": "early stopping",
    "dung som": "early stopping",
    "toi uu hoa": "optimization",
}


def fold(text):
    text = unicodedata.normalize("NFKD", text.casefold().replace("đ", "d"))
    return "".join(c for c in text if not unicodedata.combining(c))


def folded_offsets(text):
    output, offsets = [], []
    for index, char in enumerate(text):
        for folded in fold(char):
            output.append(folded)
            offsets.append(index)
    return "".join(output), offsets


def expand_query(question, docs):
    folded = fold(question)
    if re.search(r"\b(nguyen van|chinh xac|literal|verbatim|exact)\b", folded):
        return question, []
    corpus = " ".join(fold(d["title"] + " " + d["section_text"]) for d in docs)
    corpus = re.sub(r"\s+", " ", corpus)
    pieces = re.split(r'("[^"\n]*"|\x27[^\x27\n]*\x27|`[^`\n]*`|“[^”\n]*”)', question)
    changes = []
    for index in range(0, len(pieces), 2):
        piece = pieces[index]
        for alias, canonical in ALIASES.items():
            if not re.search(r"\b" + re.escape(canonical) + r"\b", corpus):
                continue
            if alias == "dung som" and not re.search(r"\b(huan luyen|training|mo hinh)\b", folded):
                continue
            folded_piece, offsets = folded_offsets(piece)
            matches = list(
                re.finditer(
                    r"(?<![\w./\\-])" + re.escape(alias) + r"(?![\w/\\-]|\.[A-Za-z0-9])",
                    folded_piece,
                )
            )
            for match in reversed(matches):
                start, end = offsets[match.start()], offsets[match.end() - 1] + 1
                while end < len(piece) and unicodedata.combining(piece[end]):
                    end += 1
                original = piece[start:end]
                piece = piece[:start] + canonical + piece[end:]
                changes.append(
                    {"from": original, "to": canonical, "reason": "permitted_bilingual_term"}
                )
        pieces[index] = piece
    return "".join(pieces), changes


def missing_acronyms(question, docs):
    corpus = " ".join(fold(d["title"] + " " + d["section_text"]) for d in docs)
    ignored = {"WHAT", "IS", "ARE", "WHY", "HOW", "WHEN", "THE", "PDF", "DOCX", "FILE", "AND", "OR"}
    return [
        word
        for word in re.findall(r"(?<!\w)[A-Z][A-Z0-9]{1,20}(?!\w)", question)
        if word not in ignored and not re.search(r"\b" + re.escape(word.casefold()) + r"\b", corpus)
    ]
