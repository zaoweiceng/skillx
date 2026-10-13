"""Build exact-input fixtures from observed calls, never from model assertions."""

from app.application.compiler import digest_json
from app.application.skillx_dependencies import lock_dependencies


async def observed_dependencies(session, dependencies, evidence):
    locks = await lock_dependencies(
        session,
        {
            "spec": {
                "capabilities": dependencies,
                "interfaces": {"execute": {"kind": "direct", "capabilities": list(dependencies)}},
            }
        },
    )
    fixtures = []
    for alias, lock in locks.items():
        matches = []
        for call in evidence.values():
            if call.status != "succeeded":
                continue
            request = call.request_json
            if lock["kind"] == "skillx":
                match = (
                    call.kind == "skillx"
                    and request.get("versionId") == lock["versionId"]
                    and request.get("interface") == lock["interface"]
                )
            elif lock["kind"] == "tool":
                match = (
                    call.kind == "tool" and request.get("capabilityRef") == lock["capabilityRef"]
                )
            else:
                match = False
            if match:
                matches.append(
                    {
                        "alias": alias,
                        "input": request.get("input", {}),
                        "output": call.output_json,
                        "sourceInvocationId": call.id,
                        "dependencyDigest": lock["digest"],
                    }
                )
        if not matches:
            raise ValueError(f"依赖 {alias} 缺少同版本真实执行证据，保留候选")
        fixtures.extend(matches)
    return locks, fixtures


def fixture_handler(fixtures):
    async def call(alias, inputs):
        matches = [f for f in fixtures if f["alias"] == alias and f["input"] == inputs]
        if not matches or len({digest_json(f["output"]) for f in matches}) != 1:
            raise ValueError("缺少确定的同输入依赖 fixture，禁止执行外部操作")
        return matches[0]["output"]

    return call
