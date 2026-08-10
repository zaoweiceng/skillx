from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from check_doc_chain import validate


class CheckDocChainTest(TestCase):
    def test_empty_scan_fails(self) -> None:
        with TemporaryDirectory() as directory:
            errors, count = validate(Path(directory))

        self.assertEqual(count, 0)
        self.assertIn("未扫描到版本化 Markdown 文件", errors)

    def test_known_broken_link_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "STEP").mkdir()
            (root / "REPLAY").mkdir()
            (root / "review").mkdir()
            (root / "STEP" / "NEXT_STEPS-001.md").write_text(
                "[断链](../docs/missing.md)\n",
                encoding="utf-8",
            )

            errors, count = validate(root)

        self.assertEqual(count, 1)
        self.assertTrue(any(error.startswith("断链:") for error in errors))
