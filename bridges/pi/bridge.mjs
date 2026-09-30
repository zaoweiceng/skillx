import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { join } from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((items, value, index, all) => {
  if (value.startsWith('--')) items.push([value.slice(2), all[index + 1]]);
  return items;
}, []));
const emit = (type, payload) => process.stdout.write(JSON.stringify({ type, payload }) + '\n');
if ('probe' in args) {
  console.log(JSON.stringify({ installed: true, sdk: '0.85.1', protocol: 'host-ndjson', providerVerified: false }));
  process.exit(0);
}
const pending = new Map();
const evidence = [];
let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf('\n')) !== -1) {
    const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
    if (!line.trim()) continue;
    try {
      const event = JSON.parse(line);
      const result = event.payload ?? event;
      const waiter = pending.get(result.callId);
      if (!waiter) throw new Error('Unpaired Host tool result');
      pending.delete(result.callId); evidence.push(result); waiter.resolve(result);
    } catch {
      for (const waiter of pending.values()) waiter.reject(new Error('Invalid Host tool result'));
      pending.clear();
    }
  }
});
let session;
process.on('SIGTERM', async () => {
  for (const waiter of pending.values()) waiter.reject(new Error('Cancelled'));
  await session?.abort();
  process.exit(0);
});
try {
  const endpoint = process.env.SKILLX_AGENT_ENDPOINT;
  const modelId = process.env.SKILLX_AGENT_MODEL;
  const key = process.env[process.env.SKILLX_AGENT_API_KEY_ENV ?? 'SKILLX_AGENT_API_KEY'];
  if (!endpoint || !modelId || !key) throw new Error('Pi model configuration unavailable');
  const cwd = process.cwd();
  const agentDir = join(cwd, '.pi-runtime');
  const runtime = await ModelRuntime.create({ authPath: join(agentDir, 'auth.json'), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  runtime.registerProvider('skillx', {
    baseUrl: endpoint.replace(/\/chat\/completions\/?$/, '').replace(/\/$/, ''),
    api: 'openai-completions',
    models: [{ id: modelId, name: modelId, reasoning: Boolean(process.env.SKILLX_REASONING_EFFORT), input: ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 8192 }],
  });
  await runtime.setRuntimeApiKey('skillx', key);
  const settings = SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
  const loader = new DefaultResourceLoader({ cwd, agentDir, settingsManager: settings, noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true, systemPrompt: 'Complete only the supplied task using authorized tools. Return the requested JSON output.' });
  await loader.reload();
  const descriptors = JSON.parse(args['tool-descriptors'] ?? '[]');
  const tools = descriptors.map((descriptor, index) => ({
    name: `host_tool_${index}`, label: descriptor.capabilityRef, description: descriptor.description ?? descriptor.capabilityRef,
    parameters: descriptor.inputSchema,
    execute: async (callId, arguments_, signal) => {
      const result = await new Promise((resolve, reject) => {
        pending.set(callId, { resolve, reject });
        signal?.addEventListener('abort', () => { pending.delete(callId); reject(new Error('Cancelled')); }, { once: true });
        emit('tool.requested', { callId, capabilityRef: descriptor.capabilityRef, arguments: arguments_ });
      });
      return { content: [{ type: 'text', text: JSON.stringify(result.status === 'succeeded' ? result.output : { error: result.error }) }], details: {} };
    },
  }));
  ({ session } = await createAgentSession({ cwd, agentDir, modelRuntime: runtime, model: runtime.getModel('skillx', modelId), thinkingLevel: process.env.SKILLX_REASONING_EFFORT || 'off', tools: tools.map(tool => tool.name), customTools: tools, resourceLoader: loader, settingsManager: settings, sessionManager: SessionManager.inMemory(cwd) }));
  let steps = 0; let totalTokens = 0; let usageComplete = true;
  session.subscribe(event => {
    if (event.type === 'turn_start') emit('model.request.dispatched', { attempt: steps + 1 });
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      const usage = event.message.usage ?? {};
      const inputTokens = (usage.input ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
      const outputTokens = usage.output ?? 0;
      totalTokens += inputTokens + outputTokens;
      usageComplete &&= inputTokens + outputTokens > 0;
      emit('agent.step.completed', { step: ++steps, inputTokens, outputTokens });
      if (steps > Number(args['max-steps'] ?? 6) || totalTokens > Number(args['max-tokens'] ?? 20000)) void session.abort();
    }
  });
  emit('agent.plan', { summary: 'Pi executes with Host-authorized tools' });
  await session.prompt(args.prompt);
  const message = [...session.state.messages].reverse().find(item => item.role === 'assistant');
  if (!message || ['error', 'aborted'].includes(message.stopReason)) throw new Error('Pi model request failed or aborted');
  const output = JSON.parse(message.content.filter(item => item.type === 'text').map(item => item.text).join(''));
  const outputProvenance = Object.fromEntries(Object.keys(output).map(field => [field, { mode: 'generative', sourceCallIds: evidence.map(item => item.callId), evidenceDigests: evidence.map(item => item.evidenceDigest), promptVersion: 'pi-sdk-bridge-v1' }]));
  emit('agent.completed', { steps, totalTokens, usageComplete, output, outputProvenance });
} catch {
  emit('agent.failed', { code: 'MODEL_PROVIDER_ERROR', message: 'Pi execution failed; check model configuration and output contract', retryable: false });
} finally {
  session?.dispose();
  process.stdin.destroy();
}
