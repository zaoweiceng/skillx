import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cachedTokens, usageTrackingFetch } from './openai-usage.mjs';

test('preserves streaming bytes and observes standard usage across split CRLF/UTF-8 frames', async () => {
  const usage = { prompt_tokens: 100, completion_tokens: 20, prompt_tokens_details: { cached_tokens: 50 } };
  const text = 'data: {"choices":[{"delta":{"content":"中文"}}],"usage":null}\r\n\r\n'
    + `data: ${JSON.stringify({ choices: [], usage })}\r\n\r\ndata: [DONE]\r\n\r\n`;
  const bytes = new TextEncoder().encode(text);
  const observed = [];
  const fetch = usageTrackingFetch(async () => new Response(new ReadableStream({
    start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close(); },
  }), { headers: { 'content-type': 'text/event-stream' } }), value => observed.push(value));
  assert.equal(await (await fetch('http://local')).text(), text);
  assert.deepEqual(observed, [usage]);
  assert.equal(cachedTokens(observed[0]), 50);
});

test('unknown and provider-specific cache fields are not reported as zero', () => {
  for (const value of [undefined, {}, { prompt_cache_hit_tokens: 30 }, { cached_tokens: 30 },
    { prompt_tokens_details: { cached_tokens: -1 } }]) assert.equal(cachedTokens(value), null);
  assert.equal(cachedTokens({ prompt_tokens_details: { cached_tokens: 0 } }), 0);
});
