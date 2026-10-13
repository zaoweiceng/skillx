"""Bound model observations without changing durable execution evidence."""

import json


def observation(value, limit=12000):
    """Keep structured previews; explicitly mark every omission for targeted rereads."""

    def preview(item):
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return preview(parsed)
            if len(item) > 1600:
                return {"preview": item[:1600], "truncated": True, "totalCharacters": len(item)}
            return item
        if isinstance(item, list):
            values = [preview(x) for x in item[:30]]
            return (
                values
                if len(item) <= 30
                else {"items": values, "truncated": True, "totalItems": len(item)}
            )
        if isinstance(item, dict):
            values = {k: preview(v) for k, v in list(item.items())[:30]}
            if len(item) > 30:
                values["_omittedKeys"] = len(item) - 30
                values["truncated"] = True
            return values
        return item

    result = preview(value)
    encoded = json.dumps(result, ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return result
    return {
        "preview": encoded[:limit],
        "truncated": True,
        "instruction": "完整结果保留在执行记录中；如需未展示内容，请缩小路径和行范围重新读取。",
    }


def context_record(directory, value):
    """Immutable, exact local record for targeted rereads instead of repeating work."""
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    if not path.exists():
        path.write_text(encoded, encoding="utf-8")
    return str(path.resolve())


def catalog_context(catalog, directory, *, discovery_only=False):
    """Expose selection metadata; load the exact contract only when relevant."""
    if discovery_only:
        return {
            "catalogPath": context_record(directory, catalog),
            "capabilities": [{key: contract[key] for key in ("id", "name", "description")
                              if key in contract} for contract in catalog["capabilities"]],
            "tools": [{key: tool[key] for key in ("capabilityRef", "description", "effect")
                       if key in tool} for tool in catalog["tools"]],
        }
    capabilities = []
    for contract in catalog["capabilities"]:
        capabilities.append({
            **{key: contract[key] for key in (
                "id", "name", "displayName", "description", "useWhen", "doNotUseWhen",
            ) if key in contract},
            "interfaces": list(contract.get("interfaces", {})),
            "contractPath": context_record(directory, contract),
        })
    return {"capabilities": capabilities, "tools": catalog["tools"]}


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def share_budget(sizes, budget):
    """Water-fill: small results stay whole; large results share the remainder."""
    allocations = [0] * len(sizes)
    remaining = set(range(len(sizes)))
    while remaining:
        share = max(0, budget // len(remaining))
        small = [i for i in remaining if sizes[i] <= share]
        if not small:
            for i in remaining:
                allocations[i] = share
            break
        for i in small:
            allocations[i] = sizes[i]
            budget -= sizes[i]
            remaining.remove(i)
    return allocations


def bounded_evidence(value, budget):
    """Preserve complete values whenever they fit; clipping is explicit and bounded."""
    if encoded_size(value) <= budget:
        return value
    if isinstance(value, dict):
        overhead = encoded_size({key: None for key in value}) - 4 * len(value)
        allocations = share_budget([encoded_size(v) for v in value.values()], budget - overhead)
        if allocations and all(
            allowance >= 80 or encoded_size(item) <= allowance
            for item, allowance in zip(value.values(), allocations, strict=True)
        ):
            return {key: bounded_evidence(item, allowance)
                    for (key, item), allowance in zip(value.items(), allocations, strict=True)}
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    result = {"preview": "", "truncated": True, "totalCharacters": len(text)}
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        result["preview"] = text[:middle]
        if encoded_size(result) <= budget:
            low = middle
        else:
            high = middle - 1
    result["preview"] = text[:low]
    return result


def evidence_output(record):
    output = record["output"]
    if record["kind"] == "code" and isinstance(output, dict):
        # Audit metadata is available through recordPath, not the evidence budget.
        return {key: output[key] for key in (
            "stdout", "stderr", "exitCode", "status", "error", "streams",
        )
                if key in output}
    return output


def call_context(records, directory, limit=24000, *, fresh_keys=None):
    """Share a bounded evidence budget across retained calls, without a top-three cliff."""
    history_path = context_record(directory, records) if records else None
    selected, outputs = [], []
    used = 2
    for record in reversed(records):
        request = record["request"]
        if record["kind"] == "code":
            request = {key: value for key, value in request.items() if key != "source"}
        item = {
            **record,
            "request": observation(request, 500),
            "output": None,
            "error": observation(record["error"], 600),
            "recordPath": context_record(directory, record),
        }
        size = encoded_size(item) + 1
        # Reserve at least two thirds of context for evidence, not record envelopes.
        if used + size > limit // 3:
            break
        selected.append(item)
        outputs.append(evidence_output(record))
        used += size
    sizes = []
    for item, output in zip(selected, outputs, strict=True):
        size = encoded_size(output)
        # Already-consumed evidence remains addressable, but should not be sent in
        # full on every decision. Keep the last dispatched batch fresh, including
        # during schema retries; the model has not successfully consumed it yet.
        if fresh_keys is not None and item["key"] not in fresh_keys:
            size = min(size, 2000)
        # A single output larger than the entire context cannot be shown whole.
        # Give it a preview rather than letting it absorb all unused capacity.
        elif size > limit:
            size = limit // 4
        sizes.append(size)
    allowances = share_budget(sizes, limit - used)
    for item, output, allowance in zip(selected, outputs, allowances, strict=True):
        item["output"] = bounded_evidence(output, allowance)
    return list(reversed(selected)), {
        "path": history_path, "totalCalls": len(records),
        "omittedCalls": len(records) - len(selected),
    }
