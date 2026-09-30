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
