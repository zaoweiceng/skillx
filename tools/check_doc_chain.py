from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
VERSION_PATTERN = re.compile(r"NEXT_STEPS-(\d{3})\.md")
# 001/002 发生在建立 REPLAY 制度之前；018 是纠正一次版本误编号后明确保留的缺号。
ALLOWED_MISSING_REPLAY_VERSIONS = {1, 2, 18}


def markdown_files(root: Path = ROOT) -> list[Path]:
    files = [
        *root.glob("STEP/NEXT_STEPS-*.md"),
        *root.glob("REPLAY/REPLAY-*.md"),
        *root.glob("review/design-compliance-review-*.md"),
        *root.glob("review/code-review-*.md"),
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


def validate(root: Path) -> tuple[list[str], int]:
    errors: list[str] = []
    files = markdown_files(root)
    if not files:
        errors.append("未扫描到版本化 Markdown 文件")

    next_steps = sorted(root.glob("STEP/NEXT_STEPS-*.md"))
    if not next_steps:
        errors.append("STEP/ 缺少 NEXT_STEPS 文档")
    else:
        latest = next_steps[-1]
        match = VERSION_PATTERN.fullmatch(latest.name)
        if match is None:
            errors.append(f"无法解析版本号: {latest.name}")
        else:
            version = int(match.group(1))
            expected = [
                root / "REPLAY" / f"REPLAY-{version - 1:03d}.md",
                root / "review" / f"design-compliance-review-{version:03d}.md",
                root / "review" / f"code-review-{version:03d}.md",
            ]
            for path in expected:
                if not path.is_file():
                    errors.append(f"版本链缺失: {path.relative_to(root)}")

    latest_version = (
        int(VERSION_PATTERN.fullmatch(next_steps[-1].name).group(1))
        if next_steps and VERSION_PATTERN.fullmatch(next_steps[-1].name)
        else 0
    )
    restored_next_steps = [
        root / "STEP" / f"NEXT_STEPS-{index:03d}.md"
        for index in range(1, latest_version + 1)
    ]
    restored_replays = [
        root / "REPLAY" / f"REPLAY-{index:03d}.md"
        for index in range(1, latest_version)
        if index not in ALLOWED_MISSING_REPLAY_VERSIONS
    ]
    for path in [*restored_next_steps, *restored_replays]:
        if not path.is_file():
            errors.append(f"历史审计文档缺失: {path.relative_to(root)}")

    for source in files:
        text = source.read_text(encoding="utf-8")
        for raw in LINK_PATTERN.findall(text):
            target = local_target(source, raw)
            if target is not None and not target.exists():
                errors.append(
                    f"断链: {source.relative_to(root)} -> {raw}"
                )
    return errors, len(files)


def main() -> int:
    errors, file_count = validate(ROOT)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(
        f"文档链检查通过: {file_count} 个版本化 Markdown 文件",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
