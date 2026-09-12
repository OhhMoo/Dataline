from collections import Counter


def process_signals(rows: list[dict], checkpoints=()) -> dict:
    seen_targets = set()
    surfaces = set()
    writes = 0
    writes_after_read = 0
    first_write = None
    coverage = 0
    review = []
    previous = None
    repeats = 0
    for row in rows:
        key = (row["tool"], row["args_hash"])
        repeats = repeats + 1 if key == previous else 1
        previous = key
        if repeats == 3:
            review.append({"signal": "doom_loop", "seq": row["seq"], "reason": "3 identical calls"})
        if row["op_class"] != "read":
            writes += 1
            if first_write is None:
                first_write = row["wts"]
                coverage = len(surfaces)
            prior_read = bool(set(row["targets"]) & seen_targets)
            writes_after_read += int(prior_read)
            if row["op_class"] == "destructive" and not prior_read:
                review.append({
                    "signal": "ungated_destructive", "seq": row["seq"],
                    "reason": "No earlier successful read of a recorded target; backup unknown",
                })
        if row["exit"] == "ok":
            seen_targets.update(row["read_targets"])
            surfaces.add(row["tool"].split("_")[0])
    fingerprints = []
    for checkpoint in checkpoints:
        fingerprints.append(checkpoint["world_fp"])
        recent = fingerprints[-4:]
        if len(recent) == 4 and recent[0] == recent[2] != recent[1] == recent[3]:
            review.append({
                "signal": "thrash", "seq": checkpoint["after_seq"],
                "reason": "Four checkpoints alternate between two fingerprints",
            })
    counts = Counter(item["signal"] for item in review)
    return {
        "experimental": True, "advisory_only": True, "false_positive_rate": None,
        "calibration": "unmeasured", "human_review_required": True,
        "time_to_first_write_s": (
            (first_write - rows[0]["wts"]) / 1000 if first_write is not None else None
        ),
        "time_origin": "first_tool_call", "surfaces_before_write": coverage,
        "read_before_write_ratio": writes_after_read / writes if writes else None,
        "doom_loops": counts["doom_loop"], "thrash": counts["thrash"],
        "ungated_destructive": counts["ungated_destructive"], "review_candidates": review,
    }
