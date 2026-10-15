"""General document RAG: permitted candidates -> hybrid -> parent -> local LLM."""

import re
from .document_answers import COMMON, title_key
from .synthesis import generate
from .hybrid import IndexUnavailable, sentences
from .store import can_read, query_candidates, scope_matches, validity_info
from .document_lookup import is_file_lookup
from .source_locations import source_location, exact_sections
from .query_expansion import expand_query, missing_acronyms


def bibliography_part(part):
    """Reference titles are discovery evidence, not definitions of their topics."""
    marker = re.search(
        r"(?mi)^\s*(?:#{1,6}\s*)?(?:\d+[.)]?\s+)?(?:references|bibliography|tài liệu tham khảo)\s*$",
        part["doc"]["section_text"],
    )
    return bool(marker and part["offset"] >= marker.start())


def passage_citation(part, citation):
    doc = part["doc"]
    start = part["offset"]
    return {
        **citation(doc, part["section_text"]),
        "location": (
            "Danh mục tài liệu tham khảo"
            if bibliography_part(part)
            else source_location(doc, start, part["title"])
        ),
        "start_offset": start,
        "end_offset": start + len(part["section_text"]),
        "line_start": part["line_start"],
        "line_end": part["line_end"],
    }


def preview_citations(parts, question, tokens, citation):
    """For a definition, put the exact definitional sentence before other hits."""
    focused = []
    if re.search(r"\b(la gi|what is|what are|define|definition)\b", title_key(question)):
        terms = set(tokens(question)) - COMMON - {"la", "what", "is", "are", "define", "definition"}
        for part in parts:
            for sentence in sentences(part["section_text"]):
                text = sentence["text"]
                shared = terms & set(tokens(text))
                if (
                    len(shared) >= min(2, len(terms))
                    and shared
                    and re.search(
                        r"\b(is a|is an|refers to|is defined|means|là một|là cách|là phương pháp)\b",
                        text,
                        re.I,
                    )
                ):
                    start = part["offset"] + sentence["start"]
                    focused.append(
                        {
                            **part,
                            "section_text": text,
                            "offset": start,
                            "line_start": part["doc"]["section_text"].count("\n", 0, start) + 1,
                            "line_end": part["doc"]["section_text"].count(
                                "\n", 0, start + len(text)
                            )
                            + 1,
                        }
                    )
    candidates = [*focused, *parts]
    selected = []
    used = set()
    for part in candidates:
        # One preview per retrieved parent; its full context opens on request.
        if part["id"] not in used:
            selected.append(passage_citation(part, citation))
            used.add(part["id"])
        if len(selected) == 3:
            break
    return selected


def answer_rag(
    db,
    user,
    docs,
    relations,
    question,
    as_of,
    tokens,
    citation,
    retriever,
    explicit_only=False,
    authorize=None,
):
    def context(d):
        org = d.get("applies_department") or d.get("applies_company")
        if org in user.get("organization_read_scope", {}).get("allowed_ids", []) and not d.get(
            "facts", {}
        ).get("subject_id"):
            return {
                **user,
                "company_id": d["applies_company"],
                "department_id": d["applies_department"],
                "role": d["applies_role"] or user["role"],
            }
        return user

    eligible = [
        d
        for d in docs
        if d["policy_key"] == "general"
        and d in query_candidates(context(d), docs, relations, as_of)
    ]
    references = [
        d
        for d in docs
        if d["policy_key"] == "general" and scope_matches(context(d), d) and can_read(user, d)
    ]
    query = set(tokens(question)) - COMMON
    named = []
    for doc in references:
        shared = query & (set(tokens(doc["title"])) - COMMON)
        if len(shared) >= 2 or any(
            len(w) >= 6
            and len({title_key(d["title"]) for d in references if w in set(tokens(d["title"]))})
            == 1
            for w in shared
        ):
            named.append(doc)
    if explicit_only and not named:
        return None
    readable = [d for d in eligible if can_read(user, d)]
    candidates = [d for d in named if d in readable] if named else readable
    if not candidates:
        return None
    if missing_acronyms(question, candidates):
        return missing(
            "Chưa tìm thấy thuật ngữ được hỏi trong các nguồn hợp lệ; chưa có căn cứ để giải thích.",
            retriever,
        )
    dates = set(re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", question))
    if len(dates) > 1:
        result = missing(
            "MVP chưa tổng hợp nhiều thời điểm trong cùng câu hỏi. Hãy tách từng ngày để dùng đúng phiên bản.",
            retriever,
        )
        result["status"] = "unsupported"
        return result
    if dates:
        from datetime import date

        value = next(iter(dates))
        try:
            day = (
                date(*reversed(list(map(int, value.split("/"))))).isoformat()
                if "/" in value
                else date.fromisoformat(value).isoformat()
            )
        except ValueError:
            day = None
        if day != as_of:
            result = missing(
                "Ngày trong câu hỏi khác ngày áp dụng. Vui lòng xác nhận ngày tra cứu trước khi chọn phiên bản.",
                retriever,
            )
            result["status"] = "needs_context"
            return result
    # File lookup has no content generation and works independently of inference availability.
    content = query - set().union(*(set(tokens(d["title"])) for d in named)) if named else query
    folded = title_key(question)
    if named and not content and is_file_lookup(question):
        return None  # Existing verified file lookup handles this.
    retrieval_query = re.sub(r"\b(?:quản lí|quan li)\b", "quản lý", question, flags=re.I)
    retrieval_query, expansions = expand_query(retrieval_query, candidates)
    ranked = retriever.rank_parents(retrieval_query, candidates)
    if expansions:
        trace = retriever.last_trace or {}
        normalization = trace.setdefault(
            "query_normalization",
            {"original": question, "normalized": retrieval_query, "changes": []},
        )
        normalization["changes"] = expansions + normalization["changes"]
        retriever.last_trace = trace
    normalized_query = (
        (retriever.last_trace or {}).get("query_normalization", {}).get("normalized", question)
    )
    relevant = [
        (p, s)
        for p, s in ranked
        if p["dense_score"] >= 0.48 and (p["sparse_score"] > 0 or p["dense_score"] >= 0.63)
    ]
    heading_matches = [
        p
        for p, _ in ranked
        if content and len(content & set(tokens(p["title"]))) / len(content) >= 0.7
    ]
    if heading_matches:
        headings = {(p["doc"]["id"], p["title"]) for p in heading_matches}
        relevant = [(p, s) for p, s in ranked if (p["doc"]["id"], p["title"]) in headings]
    focused_sections = exact_sections(normalized_query, candidates)
    if focused_sections:
        focused = [(p, 1.0) for p in focused_sections]
        comparative = re.search(r"\b(khac|so sanh|compare|versus|vs|between)\b", folded)
        relevant = focused + relevant if comparative else focused
    reference_intent = bool(
        re.search(r"\b(tham khao|references|bibliography|trich dan|tac gia|xuat ban)\b", folded)
    )
    reference_hits = [(p, s) for p, s in relevant if bibliography_part(p)]
    if not reference_intent:
        relevant = [(p, s) for p, s in relevant if not bibliography_part(p)]
    if not relevant:
        result = missing("Chưa tìm thấy đủ căn cứ cho nội dung bạn hỏi.", retriever)
        if reference_hits and not reference_intent:
            parts = [p for p, _ in reference_hits[:3]]
            sources = list({p["doc"]["id"]: p["doc"] for p in parts}.values())
            result.update(
                answer="Tìm thấy nội dung liên quan trong danh mục tài liệu tham khảo, nhưng các đoạn đó chỉ nêu tên bài viết, chưa đủ để giải thích câu hỏi. Cần bổ sung tài liệu có phần giải thích nội dung này.",
                citations=[passage_citation(p, citation) for p in parts],
                _document_sources=sources,
            )
            result["trace"]["retrieved_ids"] = [d["id"] for d in sources]
        return result
    family_ids = {p["doc"]["id"] for p, _ in relevant[:8]}
    matching = [d for d in candidates if d["id"] in family_ids]
    if any(
        not can_read(user, d) and title_key(d["title"]) in {title_key(m["title"]) for m in matching}
        for d in eligible
    ):
        return missing("Chưa đủ nguồn hợp lệ để xác định phiên bản áp dụng.", retriever)
    used = [r for r in relations if r["source_id"] in family_ids and r["target_id"] in family_ids]
    replaced = {r["target_id"] for r in used}
    winners = [d for d in matching if d["id"] not in replaced]
    if len(winners) > 1 and len({title_key(d["title"]) for d in winners}) == 1:
        result = missing(
            "Có nhiều phiên bản cùng hiệu lực chưa có quan hệ ưu tiên đã duyệt; chưa kết luận.",
            retriever,
        )
        result.update(
            status="conflict",
            citations=[citation(d) for d in winners],
            _document_sources=matching,
            _document_relations=used,
        )
        return result
    selected_ids = {d["id"] for d in winners}
    parts = [p for p, _ in relevant if p["doc"]["id"] in selected_ids][:8]
    broad = bool(
        named
        and re.search(r"\b(workflow|luong|quy trinh|cac buoc|tom tat)\b", folded)
        and not re.search(
            r"\b(buoc \d|chi phi|bao nhieu|cau truc|quyen|xac thuc|nhat ky|ghi log|logging|dau ra)\b",
            folded,
        )
    )
    required = ()
    if heading_matches:
        # Expand complete matching structural sections using exact original spans.
        from .document_answers import passages

        headings = {(p["doc"]["id"], p["title"]) for p in heading_matches}
        expanded = [p for d in winners for p in passages(d) if (d["id"], p["title"]) in headings]
        if expanded:
            parts = expanded[:8]
            required = tuple(f"E{i+1}" for i in range(len(parts)))
    if broad:
        # Expand the complete structural workflow, not a random top sentence.
        # Prefer source blocks whose headings explicitly name a flow; all spans remain exact.
        workflow = []
        for doc in winners:
            text = doc["section_text"]
            flow_titles = list(
                re.finditer(
                    r"(?mi)^(?:Luồng[^\n]{0,80}|Quản lý tài liệu[^\n]{0,80}|Workflow[^\n]{0,80})$",
                    text,
                )
            )
            for heading in flow_titles:
                end = text.find("\nNEXTGEN |", heading.end())
                end = len(text) if end < 0 else end
                body = text[heading.end() : end].strip()
                if len(body) < 120 or len(body) > 5000:
                    continue
                start = (
                    heading.end()
                    + len(text[heading.end() : end])
                    - len(text[heading.end() : end].lstrip())
                )
                workflow.append(
                    {
                        "id": doc["id"] + ":flow:" + str(start),
                        "doc": doc,
                        "title": heading.group(),
                        "section_text": body,
                        "offset": start,
                        "line_start": text.count("\n", 0, start) + 1,
                        "line_end": text.count("\n", 0, start + len(body)) + 1,
                    }
                )
        if workflow:
            overview = [p for p in workflow if title_key(p["title"]).startswith("workflow")]
            if overview and re.match(r"^(tim|tong quan|gioi thieu)\b", folded):
                # A discovery/overview request uses the introduction with its data/ACL tables.
                workflow = overview
            elif len(workflow) > 1:
                # A closing recap repeats the detailed flows; it is not another required workflow.
                workflow = [
                    p for p in workflow if not title_key(p["title"]).startswith("luong chot")
                ]
            parts = workflow[:8]
        else:
            # Generic structural expansion includes every parent in the matched file.
            parts = sorted(
                [p for p, _ in ranked if p["doc"]["id"] in selected_ids],
                key=lambda p: (p["doc"]["id"], p["offset"]),
            )
        required = tuple(f"E{i+1}" for i in range(len(parts)))
    if not parts:
        return missing("Chưa tìm thấy đoạn nguồn đủ căn cứ.", retriever)
    # Verify current SQL state immediately before sending text to the model.
    if authorize:
        authorize()
    base = {
        "trace": {
            "policy_key": "general",
            "retrieved_ids": sorted(family_ids),
            "selected_ids": sorted(selected_ids),
            "retrieval": [
                {
                    "id": p["doc"]["id"],
                    "parent_id": p["id"],
                    "location": source_location(p["doc"], p["offset"], p["title"]),
                    "line_start": p["line_start"],
                    "line_end": p["line_end"],
                    "start_offset": p["offset"],
                    "end_offset": p["offset"] + len(p["section_text"]),
                }
                for p in parts
            ],
            "sources_sent_to_llm": [],
            "relations_used": used,
            "hybrid": retriever.last_trace,
        },
        "_document_sources": matching,
        "_document_relations": used,
        "amount": None,
        "unit": None,
        "citations": [],
    }
    if re.search(r"\b(bao nhieu tien|how much money)\b", folded) and not any(
        re.search(
            r"\b(usd|vnd|dollars?|euros?|dong|nghin|ngan|trieu)\b|\$", title_key(p["section_text"])
        )
        for p in parts
    ):
        return {
            **base,
            "status": "insufficient_evidence",
            "answer": "Các đoạn tìm được không có số liệu tiền hoặc chi phí để trả lời câu hỏi này; tôi chưa kết luận.",
            "citations": preview_citations(parts, normalized_query, tokens, citation),
        }
    try:
        generated, trace = generate(db, normalized_query, parts, required, authorize=authorize)
    except IndexUnavailable as error:
        context = getattr(error, "context", None)
        if context:
            base["_generation"] = context
            base["trace"]["sources_sent_to_llm"] = context.get("source_ids", [])
        return {
            **base,
            "status": "insufficient_evidence",
            "reason": "answer_generation_unavailable",
            "answer": "Đã tìm được các đoạn liên quan bên dưới, nhưng chưa thể hoàn tất câu trả lời: "
            + str(error),
            "citations": preview_citations(parts, normalized_query, tokens, citation),
        }
    base["_generation"] = trace
    base["trace"]["sources_sent_to_llm"] = sorted({p["doc"]["id"] for p in parts})
    if not trace["answerable"]:
        return {
            **base,
            "status": "insufficient_evidence",
            "answer": "Các đoạn tìm được chưa đủ căn cứ trả lời điều bạn hỏi. Tôi chưa kết luận; cần bổ sung nguồn phù hợp.",
            "citations": preview_citations(parts, normalized_query, tokens, citation),
        }
    cites = []
    claims = []
    reference_indices = {}
    for claim in generated:
        indices = []
        for ref in claim["references"]:
            part = ref["part"]
            doc = part["doc"]
            key = (doc["id"], ref["start_offset"], ref["end_offset"])
            if key not in reference_indices:
                c = citation(doc, ref["text"])
                c.update(
                    location=source_location(doc, ref["start_offset"], part["title"]),
                    sentence_id=ref["sentence_id"],
                    start_offset=ref["start_offset"],
                    end_offset=ref["end_offset"],
                    line_start=doc["section_text"].count("\n", 0, ref["start_offset"]) + 1,
                    line_end=doc["section_text"].count("\n", 0, ref["end_offset"]) + 1,
                )
                reference_indices[key] = len(cites) + 1
                cites.append(c)
            indices.append(reference_indices[key])
        claims.append({"text": claim["text"], "citation_indices": indices})
    return {
        **base,
        "status": "answered",
        "answer_kind": "grounded_llm",
        "citations": cites,
        "claims": claims,
        "answer": "\n\n".join(
            c["text"] + " " + " ".join(f"[{i}]" for i in c["citation_indices"]) for c in claims
        ),
        "generation": {
            k: trace[k]
            for k in (
                "model",
                "digest",
                "strategy",
                "thinking",
                "duration_ms",
                "complete",
                "provider",
            )
            if k in trace
        },
    }


def missing(message, retriever):
    return {
        "status": "insufficient_evidence",
        "answer": message,
        "citations": [],
        "amount": None,
        "unit": None,
        "trace": {
            "policy_key": "general",
            "retrieved_ids": [],
            "selected_ids": [],
            "retrieval": [],
            "sources_sent_to_llm": [],
            "relations_used": [],
            "hybrid": retriever.last_trace,
        },
        "_document_sources": [],
        "_document_relations": [],
    }
