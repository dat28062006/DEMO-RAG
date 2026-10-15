"""Conservative spelling normalization for retrieval intent, never source facts/ACL."""

from difflib import SequenceMatcher
import re
import unicodedata


def _one_typo(word, target):
    if word == target:
        return False
    if abs(len(word) - len(target)) > 1:
        return False
    if len(word) == len(target):
        wrong = [i for i, (a, b) in enumerate(zip(word, target)) if a != b]
        if len(wrong) == 1:
            return True
        return (
            len(wrong) == 2
            and wrong[1] == wrong[0] + 1
            and word[wrong[0]] == target[wrong[1]]
            and word[wrong[1]] == target[wrong[0]]
        )
    longer, shorter = (word, target) if len(word) > len(target) else (target, word)
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))


def _corpus_spelling(word, vocabulary):
    """Resolve only an unambiguous long-term typo from the permitted corpus.

    Protect existing words, short words, identifiers and numeric values. A common
    four-letter prefix and a high sequence similarity avoid guessing other topics.
    """
    if len(word) < 10 or word in vocabulary:
        return None
    matches = sorted(
        (
            (SequenceMatcher(None, word, term).ratio(), term)
            for term in vocabulary
            if len(term) >= 10 and term[:4] == word[:4] and abs(len(term) - len(word)) <= 3
        ),
        reverse=True,
    )
    if not matches or matches[0][0] < 0.82:
        return None
    if len(matches) > 1 and matches[0][0] - matches[1][0] < 0.06:
        return None
    return matches[0][1]


def normalize_query(question, vocabulary=()):
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", question.lower()) if not unicodedata.combining(c)
    )
    if re.search(r"\b(nguyen van|chinh xac|literal|verbatim|exact)\b", folded):
        return question, []
    changes = []
    vocabulary = {term.casefold() for term in vocabulary if re.fullmatch(r"[A-Za-z]+", term)}
    # Quoted phrases and paths/identifiers are literal; do not rewrite them.
    pieces = re.split(r'("[^"\n]*"|\x27[^\x27\n]*\x27|`[^`\n]*`|“[^”\n]*”)', question)

    def normalize_piece(piece):
        def spelling(match):
            word = match.group()
            canonical = word.lower()
            if canonical == "workflow" or _one_typo(canonical, "workflow"):
                if word != "workflow":
                    changes.append({"from": word, "to": "workflow", "reason": "workflow_spelling"})
                return "workflow"
            corrected = _corpus_spelling(canonical, vocabulary)
            if corrected:
                changes.append(
                    {"from": word, "to": corrected, "reason": "permitted_corpus_spelling"}
                )
                return corrected
            return word

        def phrase(match):
            changes.append({"from": match.group(), "to": "workflow", "reason": "workflow_alias"})
            return "workflow"

        piece = re.sub(
            r"(?<![\w./\\])work[ -]+flow(?![\w/\\]|\.[A-Za-z0-9])", phrase, piece, flags=re.I
        )
        return re.sub(
            r"(?<![\w./\\-])(?:[A-Za-z]{6,30})(?![\w/\\-]|\.[A-Za-z0-9])", spelling, piece
        )

    normalized = "".join(
        piece if i % 2 else normalize_piece(piece) for i, piece in enumerate(pieces)
    )
    return normalized, changes
