import re
from .core import *
from .knowledge import ARTICLE, VISIBLE, normal, sentences, tokens


def retrieve(db, role, question, category, topic, config):
    parts = [part.strip() for part in re.split("[？?；;]", question) if part.strip()]
    if len(parts) > 1:
        groups = [
            retrieve(db, role, part, category, topic, config) for part in parts[:4]
        ]
        ranked = []
        for rank in range(config["top_k"]):
            for group in groups:
                if rank < len(group) and all(
                    row["chunk_id"] != group[rank]["chunk_id"] for row in ranked
                ):
                    ranked.append(group[rank])
        return ranked[: config["top_k"]]
    if not topic and re.fullmatch(
        r"[那这]?[它其该](?:适用于谁|是什么|呢)[？?。]?", question.strip()
    ):
        return []
    terms = tokens(question)
    title_names = re.findall(r"《([^》]{1,200})》", question)
    if not terms:
        return []
    context_title = (
        topic
        if re.search(r"^(那|它|该|这个|这次|上述|如果这个|如果该)", question)
        else None
    )
    focus = [x for x in terms if len(x) > 1 or x.isdecimal()] or terms
    if category:
        category = uid(category)
    selectors = ["(" + VISIBLE + ")", "k.search_vector @@ to_tsquery('simple',:terms)"]
    search_terms = focus + (tokens(context_title) if context_title else [])
    params = dict(role=role, day=today(), terms=" | ".join(dict.fromkeys(search_terms)))
    if title_names:
        selectors.append("d.title=ANY(:titles)")
        params["titles"] = title_names
    elif context_title:
        selectors.append("d.title=:context_title")
        params["context_title"] = context_title
    if category:
        selectors.append("d.category_id=:category")
        params["category"] = category
    rows = many(
        db,
        """SELECT k.chunk_id,k.content,k.locator,k.ordinal,v.version_id,v.version_no,v.valid_from,v.valid_to,
        d.document_id,d.title,d.source_name,d.source_url,c.name AS category,
        ts_rank(k.search_vector,to_tsquery('simple',:terms)) AS fts
        FROM knowledge_chunk k JOIN document_version v USING(version_id) JOIN knowledge_document d USING(document_id)
        JOIN category c USING(category_id) WHERE """
        + " AND ".join(selectors)
        + " ORDER BY fts DESC,k.ordinal,k.chunk_id LIMIT 180",
        **params,
    )
    candidates = []
    for row in rows:
        body = normal(row["content"] + " " + row["locator"])
        title = normal(row["title"])
        weight = {
            t: 2 if ARTICLE.fullmatch(t) or t.isdecimal() or t in title else 1
            for t in focus
        }
        score = sum(w for t, w in weight.items() if t in body or t in title) / sum(
            weight.values()
        )
        if score < float(config["threshold"]) and not context_title:
            continue
        body_score = sum(w for t, w in weight.items() if t in body) / sum(
            weight.values()
        )
        candidates.append((score, body_score, float(row["fts"]), -row["ordinal"], row))
    candidates.sort(key=lambda item: item[:4], reverse=True)
    selected = [item[4] for item in candidates[: config["top_k"]]]
    if len(title_names) > 1:
        for title in dict.fromkeys(title_names):
            if not any(row["title"] == title for row in selected):
                extra = next((x[4] for x in candidates if x[4]["title"] == title), None)
                if extra and selected:
                    counts = {
                        name: sum(x["title"] == name for x in selected)
                        for name in title_names
                    }
                    idx = next(
                        (
                            n
                            for n in reversed(range(len(selected)))
                            if counts.get(selected[n]["title"], 0) > 1
                        ),
                        None,
                    )
                    if idx is not None:
                        selected[idx] = extra
    return selected


def active_rules(db):
    return many(
        db,
        "SELECT * FROM (SELECT DISTINCT ON(rule_id) * FROM safety_rule ORDER BY rule_id,rule_version DESC) r WHERE enabled",
    )


def matches(rule, value):
    pattern = re.escape(rule["pattern"])
    if rule["match_type"] == "EXACT":
        pattern = r"(?<!\w)" + pattern + r"(?!\w)"
    return list(re.finditer(pattern, value, re.I))


def screen(value, rules):
    whitelist = [
        m.span() for rule in rules if rule["allowlist"] for m in matches(rule, value)
    ]
    found_rules = []
    masked = list(value)
    for rule in rules:
        if rule["allowlist"]:
            continue
        hits = [
            m
            for m in matches(rule, value)
            if not any(a <= m.start() and m.end() <= b for a, b in whitelist)
        ]
        if hits:
            found_rules.append(
                (str(rule["rule_id"]), rule["rule_version"], rule["action"])
            )
        if rule["action"] == "MASK":
            for match in hits:
                masked[match.start() : match.end()] = ["＊"] * (
                    match.end() - match.start()
                )
    return any(x[2] == "BLOCK" for x in found_rules), "".join(masked), found_rules


def verify(candidate, evidence, rules):
    if not isinstance(candidate, dict) or not isinstance(candidate.get("points"), list):
        return "SERVICE_ERROR", "MODEL_FORMAT", []
    mode = candidate.get("mode")
    if mode == "NO_EVIDENCE" and not candidate["points"]:
        return "NO_EVIDENCE", "INSUFFICIENT_EVIDENCE", []
    if (
        mode not in ("EXCERPT", "SUMMARY", "COMPARE")
        or not 1 <= len(candidate["points"]) <= 24
    ):
        return "SERVICE_ERROR", "MODEL_FORMAT", []
    verified = []
    for point in candidate["points"]:
        if not isinstance(point, dict) or not isinstance(point.get("quote"), str):
            return "SERVICE_ERROR", "MODEL_FORMAT", []
        src = evidence.get(point.get("evidence_id"))
        if not src:
            return "REFUSED", "INVALID_CITATION", []
        # Only complete, consecutive sentences may become visible quotations.
        normalize = lambda x: re.sub(r"\s+", "", x)
        want = normalize(point["quote"])
        found_quote = None
        parts = sentences(src["model_text"])
        for start in range(len(parts)):
            joined = ""
            for sent in parts[start:]:
                joined += sent
                norm = normalize(joined)
                if norm == want:
                    found_quote = joined
                    break
                if not want.startswith(norm):
                    break
            if found_quote:
                break
        if not found_quote:
            return "REFUSED", "QUOTE_MISMATCH", []
        blocked, visible, _ = screen(found_quote, rules)
        if blocked:
            return "REFUSED", "OUTPUT_RULE", []
        if not any(
            existing["chunk_id"] == src["chunk_id"] and existing["quote"] == visible
            for existing in verified
        ):
            verified.append(src | {"quote": visible})
    if mode == "COMPARE" and len({r["document_id"] for r in verified}) < 2:
        return "REFUSED", "COMPARISON_SOURCES", []
    return "SUCCESS", mode, verified
