"""Category-neutral, bounded owner evidence shared by consumer workflows."""

from collections import deque

from llm_analysis import _hash
from research import retrieve


def prepare_evidence(engine, rows, *, dataset, asins, question="overall experience strengths weaknesses"):
    products, entries, exposed, fingerprints, identities, source_keys = [], [], {}, [], [], {}
    for product_index, asin in enumerate(asins, 1):
        canonical, _, fingerprint, metadata = engine._prepare(rows, dataset, asin)
        fingerprints.append(fingerprint)
        identities.append({row["review_id"] for row in canonical})
        groups = [[], [], []]
        rated, total_rating = 0, 0
        for row in canonical:
            rating = float(row["rating"]) if row["rating"] else None
            if rating is not None:
                rated += 1
                total_rating += rating
            groups[0 if rating is not None and rating <= 2 else 1 if rating is not None and rating >= 4 else 2].append(row)
        queues = []
        for group in groups:
            if not question:
                # Category-neutral samples prefer substantive owner text within
                # each rating group, without a product-specific keyword list.
                group = sorted(group, key=lambda row: (-min(900, len(row["review_body"]) + len(row["review_title"])), row["review_id"]))
            selected, sources, _ = retrieve(engine, [row for row in group if (row["review_title"] + row["review_body"]).strip()], question)
            queues.append(deque((item, sources[item["key"]]) for item in selected))
        selected_rows, selected_ids = [], set()
        while any(queues) and len(selected_rows) < 12:
            for queue in queues:
                if queue and len(selected_rows) < 12:
                    item, source = queue.popleft()
                    if source["review_id"] in selected_ids:
                        continue
                    selected_ids.add(source["review_id"])
                    key = f"p{product_index}r{len(selected_rows) + 1}"
                    source = {**source}
                    exposed[key] = source
                    source_key = source_keys.setdefault(source["review_id"], f"s{len(source_keys) + 1}")
                    selected_rows.append({**item, "key": key, "product": f"p{product_index}", "source": source_key})
        entries.extend(selected_rows)
        examples = []
        for item in selected_rows[:6]:
            source = exposed[item["key"]]
            quote = (item["body"] or item["title"])[:240]
            if quote.strip():
                examples.append({"asin": asin, "review_id": source["review_id"], "quote": quote,
                                 "rating": source["rating"]})
        products.append({"asin": asin, "stored_reviews": len(canonical), "selected_reviews": len(selected_rows),
            "average_rating": total_rating / rated if rated else None, "rated_reviews": rated,
            "positive_rated_reviews": len(groups[1]), "critical_rated_reviews": len(groups[0]),
            "source_examples": examples, "metadata": metadata})
    overlap = len(identities[0] & identities[1]) if len(identities) == 2 else 0
    return products, entries, exposed, _hash(fingerprints), overlap

