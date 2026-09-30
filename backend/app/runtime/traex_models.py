"""Discover public model identifiers through the local CLI, never its credentials."""

import asyncio
import json
import shutil
from contextlib import suppress
from typing import Any


async def list_traex_models() -> list[dict[str, Any]]:
    command = shutil.which("traex")
    if not command:
        raise ValueError("本机未安装 TraeX")
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            "models",
            "--json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ValueError("无法启动 TraeX 模型查询，请检查可执行文件权限") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode:
            raise ValueError("TraeX 模型列表读取失败，请检查本机登录状态")
        try:
            items = json.loads(output)
            if not isinstance(items, list):
                raise ValueError
            result = []
            seen = set()
            for item in items:
                identifier = item.get("config_name") or item.get("name")
                if not isinstance(identifier, str) or not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                result.append(
                    {
                        "id": identifier,
                        "name": item.get("name") or identifier,
                        "contextWindow": item.get("context_window"),
                        "reasoningEfforts": ["low", "medium", "high"]
                        if (
                            identifier.lower().startswith(("gpt-5", "gpt-6", "o3", "o4"))
                            and "support reasoning" in (item.get("description") or "").lower()
                        )
                        else [],
                    }
                )
            return result
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("TraeX 返回了无法识别的模型列表") from exc
    except TimeoutError as exc:
        raise ValueError("读取 TraeX 模型列表超时，请重试") from exc
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def validate_traex_model(model: str | None) -> None:
    if model and model not in {item["id"] for item in await list_traex_models()}:
        raise ValueError("所选模型不在当前 TraeX 模型列表中，请刷新后重选")
