import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';

const sdkVersion = JSON.parse(readFileSync(new URL('./node_modules/@earendil-works/pi-coding-agent/package.json', import.meta.url), 'utf8')).version;
if (sdkVersion !== '0.85.1') throw new Error('Pi SDK version mismatch; run make pi-install');

const args = Object.fromEntries(process.argv.slice(2).reduce((items, value, index, all) => {
  if (value.startsWith('--')) items.push([value.slice(2), all[index + 1]]);
  return items;
}, []));
const emit = (type, payload) => process.stdout.write(JSON.stringify({ type, payload }) + '\n');
if ('probe' in args) {
  console.log(JSON.stringify({ installed: true, sdk: sdkVersion, protocol: 'host-ndjson', providerVerified: false }));
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
let apiKey;
let failureCode = 'MODEL_PROVIDER_ERROR';
const scrub = value => {
  if (typeof value === 'string' && /^data:image\/[\w.+-]+;base64,/.test(value)) {
    return value.slice(0, value.indexOf(',')) + ',[图片数据已省略]';
  }
  if (typeof value === 'string') return apiKey ? value.split(apiKey).join('[REDACTED]') : value;
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) =>
    [key, /^(authorization|api[-_]?key|password|secret|access[-_]?token)$/i.test(key) ? '[REDACTED]' : scrub(item)]));
  return value;
};
const trace = (requestId, phase, data) => {
  const serialized = JSON.stringify(scrub(data));
  // Split by Unicode code points: each NDJSON line stays below the Host byte limit.
  const characters = Array.from(serialized);
  const chunkCount = Math.ceil(characters.length / 6000);
  for (let chunkIndex = 0; chunkIndex < chunkCount; chunkIndex++) emit('model.trace', {
    requestId, phase, chunkIndex, chunkCount,
    data: characters.slice(chunkIndex * 6000, (chunkIndex + 1) * 6000).join(''),
  });
};
process.on('SIGTERM', async () => {
  for (const waiter of pending.values()) waiter.reject(new Error('Cancelled'));
  await session?.abort();
  process.exit(0);
});
try {
  const endpoint = process.env.SKILLX_AGENT_ENDPOINT;
  const modelId = process.env.SKILLX_AGENT_MODEL;
  const key = process.env[process.env.SKILLX_AGENT_API_KEY_ENV ?? 'SKILLX_AGENT_API_KEY'];
  apiKey = key;
  const options = JSON.parse(process.env.SKILLX_PI_MODEL_OPTIONS ?? '{}');
  const imageReferences = JSON.parse(args.images ?? '[]');
  if (imageReferences.length && !options.images) throw new Error('当前模型未声明支持图片输入');
  const images = imageReferences.map(image => ({ type: 'image', mimeType: image.mimeType, data: readFileSync(image.path).toString('base64') }));
  if (!endpoint || !modelId || !key) throw new Error('Pi model configuration unavailable');
  const cwd = process.cwd();
  const agentDir = join(cwd, '.pi-runtime');
  const runtime = await ModelRuntime.create({ authPath: join(agentDir, 'auth.json'), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
  runtime.registerProvider('skillx', {
    baseUrl: endpoint.replace(/\/chat\/completions\/?$/, '').replace(/\/$/, ''),
    api: 'openai-completions',
    models: [{ id: modelId, name: modelId, reasoning: options.reasoning || Boolean(process.env.SKILLX_REASONING_EFFORT), input: options.images ? ['text', 'image'] : ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: options.contextWindow ?? 128000, maxTokens: options.maxOutputTokens ?? 8192 }],
  });
  await runtime.setRuntimeApiKey('skillx', key);
  const settings = SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
  const systemPrompt = '你是任务执行 Agent。仅使用 Host 授权的工具完成给定任务。'
    + '除用户明确要求其他语言外，分析思考、执行说明和最终回答默认使用简体中文；代码、协议字段、标识符和引用原文保留原样。'
    + '最终消息必须且只能是一个符合以下 JSON Schema 的 JSON 对象，不得添加 Markdown 代码围栏、正文、注释或其他文字：'
    + (args['output-schema'] ?? '{}');
  const loader = new DefaultResourceLoader({ cwd, agentDir, settingsManager: settings, noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true, systemPrompt });
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
  let requestId; let startedAt;
  const stream = session.agent.streamFunction;
  session.agent.streamFunction = (model, context, streamOptions) => stream(model, context, {
    ...streamOptions, maxRetries: 0, timeoutMs: (options.timeoutSeconds ?? 60) * 1000,
    maxTokens: Math.min(options.maxOutputTokens ?? 8192, Number(args['max-tokens'] ?? 20000)),
  });
  session.agent.onPayload = payload => {
    requestId = randomUUID(); startedAt = performance.now();
    emit('model.request.dispatched', { attempt: steps + 1, requestId, model: modelId, provider: 'pi', startedAt: new Date().toISOString() });
    trace(requestId, 'request', payload);
  };
  session.agent.onResponse = response => {
    emit('model.response.started', { requestId, status: response.status, latencyMs: Math.round(performance.now() - startedAt) });
  };
  session.agent.shouldStopAfterTurn = () => steps >= Number(args['max-steps'] ?? 6) || totalTokens >= Number(args['max-tokens'] ?? 20000);
  session.subscribe(event => {
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      const usage = event.message.usage ?? {};
      const inputTokens = (usage.input ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
      const outputTokens = usage.output ?? 0;
      totalTokens += inputTokens + outputTokens;
      usageComplete &&= inputTokens + outputTokens > 0;
      const durationMs = startedAt === undefined ? 0 : Math.round(performance.now() - startedAt);
      trace(requestId ?? `pi-llm-${steps + 1}`, 'response', {
        model: modelId, content: event.message.content.map(item => {
          const { thoughtSignature: _signature, thinkingSignature: _thinkingSignature, ...content } = item; return content;
        }), stopReason: event.message.stopReason, error: event.message.errorMessage,
        usage: { input: usage.input ?? 0, output: outputTokens, cacheRead: usage.cacheRead ?? 0, cacheWrite: usage.cacheWrite ?? 0, totalTokens: inputTokens + outputTokens },
        durationMs,
      });
      emit('agent.step.completed', { step: ++steps, requestId, model: modelId, provider: 'pi', inputTokens, outputTokens,
        cacheReadTokens: usage.cacheRead ?? 0, cacheWriteTokens: usage.cacheWrite ?? 0, durationMs,
        usageComplete: inputTokens + outputTokens > 0 });
      if (steps > Number(args['max-steps'] ?? 6) || totalTokens > Number(args['max-tokens'] ?? 20000)) void session.abort();
    }
  });
  emit('agent.plan', { summary: 'Pi 使用 Host 授权的工具执行任务' });
  await session.prompt(args.prompt, { images, expandPromptTemplates: false });
  const message = [...session.state.messages].reverse().find(item => item.role === 'assistant');
  if (!message || ['error', 'aborted'].includes(message.stopReason)) throw new Error(message?.errorMessage || 'Pi model request failed or aborted');
  failureCode = 'MODEL_MALFORMED_OUTPUT';
  const output = JSON.parse(message.content.filter(item => item.type === 'text').map(item => item.text).join(''));
  const outputProvenance = Object.fromEntries(Object.keys(output).map(field => [field, { mode: 'generative', sourceCallIds: evidence.map(item => item.callId), evidenceDigests: evidence.map(item => item.evidenceDigest), promptVersion: 'pi-sdk-bridge-v1' }]));
  emit('agent.completed', { steps, totalTokens, usageComplete, model: modelId, provider: 'pi', output, outputProvenance });
} catch (error) {
  emit('agent.failed', { code: failureCode, message: scrub(String(error?.message ?? 'Pi execution failed')).slice(0, 2000), retryable: false });
} finally {
  session?.dispose();
  process.stdin.destroy();
}
