from pathlib import Path
import shutil

import pytest
import yaml

from app.application.managed_skills import canonical_content, parse_skill
from app.runtime.skill_document import split_frontmatter
from app.runtime.skillx import SkillXValidationError, load_manifest


@pytest.mark.parametrize("description", [
    # Actual failed builder used Markdown frontmatter in its description. Host
    # safe_dump sorts description before name, so inline splitting lost name.
    r"序列化为 `---\n{yaml}---\n\n{正文}` 形态文本",
    "multiple --- inline --- separators",
    "a multiline value\n---\nstill part of the quoted YAML string",
])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_same_name_survives_inline_separators_across_import_and_runtime(tmp_path, description, newline):
    package = Path(__file__).resolve().parents[2] / "capabilities" / "skillx" / "text-stats"
    shutil.copytree(package, tmp_path / "package")
    root = tmp_path / "package"
    manifest = yaml.safe_load((root / "skillx.yaml").read_text())
    name = manifest["metadata"]["name"]
    text = "---\n" + yaml.safe_dump({"name": name, "description": description}, allow_unicode=True) + "---\nGuide\n---\nBody separator"
    text = text.replace("\n", newline)
    (root / "SKILL.md").write_text(text)
    assert load_manifest(root)["metadata"]["name"] == name
    metadata, body = parse_skill(text)
    assert metadata == {"name": name, "description": description}
    assert "Body separator" in body
    assert parse_skill(canonical_content(name, description, text))[0] == metadata


def test_unclosed_frontmatter_and_actual_name_mismatch_still_rejected(tmp_path):
    with pytest.raises(ValueError, match="未闭合"):
        split_frontmatter('---\nname: test\ndescription: inline --- marker')
    package = Path(__file__).resolve().parents[2] / "capabilities" / "skillx" / "text-stats"
    shutil.copytree(package, tmp_path / "package")
    root = tmp_path / "package"
    (root / "SKILL.md").write_text('---\nname: wrong-name\n---\nGuide')
    with pytest.raises(SkillXValidationError, match="name 必须"):
        load_manifest(root)
