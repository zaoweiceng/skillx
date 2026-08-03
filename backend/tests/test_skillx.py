from pathlib import Path

from app.runtime.skillx import execute_direct, load_interface


async def test_direct_skillx_validates_and_executes_without_model() -> None:
    package = Path(__file__).resolve().parents[2] / "capabilities" / "skillx" / "text-stats"
    loaded = load_interface(package, "count")

    result = await execute_direct(loaded, "inv-test", {"text": "hello 世界"})

    assert result.status == "succeeded"
    assert result.output == {
        "characters": 8,
        "nonWhitespaceCharacters": 7,
        "words": 2,
    }


async def test_direct_skillx_rejects_invalid_input_before_execution() -> None:
    package = Path(__file__).resolve().parents[2] / "capabilities" / "skillx" / "text-stats"
    loaded = load_interface(package, "count")

    result = await execute_direct(loaded, "inv-test", {})

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "INVALID_INPUT"
