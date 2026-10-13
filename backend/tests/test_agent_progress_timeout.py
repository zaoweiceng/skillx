import asyncio

import pytest

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest


class StreamAdapter:
    def __init__(self, delays):
        self.delays = delays
        self.cancelled = False

    async def events(self, run_id, request):
        for index, delay in enumerate(self.delays):
            await asyncio.sleep(delay)
            yield {'type': 'model.response.progress', 'payload': {
                'requestId': 'one', 'receivedCharacters': index + 1, 'elapsedMs': index + 1}}
        yield {'type': 'agent.completed', 'payload': {'steps': 0, 'output': {'ok': True}}}

    async def cancel(self, run_id):
        self.cancelled = True


async def test_live_progress_survives_idle_window(tmp_path):
    adapter = StreamAdapter([0.015] * 6)
    result = await AgentHost(adapter=adapter).execute(AgentHostRequest(
        run_id='progress', prompt='test', output_schema={'type': 'object'},
        working_directory=tmp_path, include_managed_skills=False,
        max_idle_seconds=0.05, max_duration_seconds=1,
    ))
    assert result.output == {'ok': True}
    assert not adapter.cancelled


async def test_silent_stream_is_cancelled_before_total_budget(tmp_path):
    adapter = StreamAdapter([1])
    with pytest.raises(AgentHostError, match='无新进展'):
        await AgentHost(adapter=adapter).execute(AgentHostRequest(
            run_id='stalled', prompt='test', output_schema={'type': 'object'},
            working_directory=tmp_path, include_managed_skills=False,
            max_idle_seconds=0.02, max_duration_seconds=1,
        ))
    assert adapter.cancelled
