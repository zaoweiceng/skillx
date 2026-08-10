from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
VERSION_PATTERN = re.compile(r"NEXT_STEPS-(\d{3})\.md")


def markdown_files() -> list[Path]:
    files = [
        *ROOT.glob("NEXT_STEPS-*.md"),
        *ROOT.glob("REPLAY-*.md"),
        *ROOT.glob("review/design-compliance-review-*.md"),
        *ROOT.glob("review/code-review-*.md"),
    ]
    return sorted(set(files))


def local_target(source: Path, raw: str) -> Path | None:
    target = raw.strip().split(maxsplit=1)[0].strip("<>")
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    path = unquote(target.split("#", 1)[0])
    if not path:
        return None
    return (source.parent / path).resolve()


def main() -> int:
    errors: list[str] = []
    next_steps = sorted(ROOT.glob("NEXT_STEPS-*.md"))
    if not next_steps:
        errors.append("根目录缺少 NEXT_STEPS 文档")
    else:
        latest = next_steps[-1]
        match = VERSION_PATTERN.fullmatch(latest.name)
        if match is None:
            errors.append(f"无法解析版本号: {latest.name}")
        else:
            version = int(match.group(1))
            expected = [
                ROOT / f"REPLAY-{version - 1:03d}.md",
                ROOT / "review" / f"design-compliance-review-{version:03d}.md",
                ROOT / "review" / f"code-review-{version:03d}.md",
            ]
            for path in expected:
                if not path.is_file():
                    errors.append(f"版本链缺失: {path.relative_to(ROOT)}")

    restored_next_steps = [ROOT / f"NEXT_STEPS-{index:03d}.md" for index in range(1, 22)]
    restored_replays = [
        ROOT / f"REPLAY-{index:03d}.md"
        for index in (*range(3, 18), *range(19, 22))
    ]
    for path in [*restored_next_steps, *restored_replays]:
        if not path.is_file():
            errors.append(f"历史审计文档缺失: {path.relative_to(ROOT)}")

    for source in markdown_files():
        text = source.read_text(encoding="utf-8")
        for raw in LINK_PATTERN.findall(text):
            target = local_target(source, raw)
            if target is not None and not target.exists():
                errors.append(
                    f"断链: {source.relative_to(ROOT)} -> {raw}"
                )

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(
        f"文档链检查通过: {len(markdown_files())} 个版本化 Markdown 文件",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
