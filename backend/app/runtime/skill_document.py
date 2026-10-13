"""Read Markdown frontmatter using document boundary lines, not inline text."""

import re


def split_frontmatter(content: str) -> tuple[str | None, str]:
    opening = re.match(r"\A---[ \t]*\r?\n", content)
    if opening is None:
        return None, content
    closing = re.search(r"^---[ \t]*(?:\r?\n|$)", content[opening.end():], re.MULTILINE)
    if closing is None:
        raise ValueError("SKILL.md 的 YAML 元信息未闭合")
    start = opening.end()
    return content[start:start + closing.start()], content[start + closing.end():]
