// Observe the original usage before the SDK replaces absent cache fields with 0.
// Pass bytes through unchanged, with stream backpressure and cancellation intact.
export function usageTrackingFetch(fetchImpl, onUsage) {
  return async (...args) => {
    const response = await fetchImpl(...args);
    if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) return response;
    const decoder = new TextDecoder();
    let pending = '';
    const inspect = block => {
      const data = block.split(/\r?\n/).filter(line => line.startsWith('data:'))
        .map(line => line.slice(5).trimStart()).join('\n');
      if (!data || data === '[DONE]') return;
      let event;
      try { event = JSON.parse(data); } catch { return; }
      if (event.usage && typeof event.usage === 'object') onUsage(event.usage);
    };
    const body = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) {
        pending += decoder.decode(chunk, { stream: true });
        let separator;
        while ((separator = /\r?\n\r?\n/.exec(pending))) {
          inspect(pending.slice(0, separator.index));
          pending = pending.slice(separator.index + separator[0].length);
        }
        controller.enqueue(chunk);
      },
      flush() { pending += decoder.decode(); if (pending.trim()) inspect(pending); },
    }));
    return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
  };
}

export function cachedTokens(usage) {
  const value = usage?.prompt_tokens_details?.cached_tokens;
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}
