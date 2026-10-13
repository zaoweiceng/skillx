import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { join } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resultTools } from './result-tools.mjs';
import { usageTrackingFetch, cachedTokens } from './openai-usage.mjs';

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
let outputParseError;
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
  const outputSchema = JSON.parse(args['output-schema'] ?? '{}');
  const structuredMode = options.structuredMode ?? 'json_schema';
  const descriptors = JSON.parse(args['tool-descriptors'] ?? '[]');
  const submitResultMode = structuredMode === 'json_schema' && descriptors.length === 0;
  const submissionsTools = submitResultMode ? resultTools(outputSchema) : [];
  let submittedResult;
  if (!['json_schema', 'json_object', 'prompt'].includes(structuredMode)) throw new Error('Invalid structured output mode');
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
    + (submitResultMode
      ? '本阶段只生成结构化结果，没有文件、命令行或网络工具。选择一个提交工具并调用一次，参数以该工具 Schema 为准。已有证据充分就提交完成答案，不重新规划。不要尝试浏览环境，也不要输出占位标量。'
      : '最终消息必须且只能是一个符合请求提供的 JSON Schema 的 JSON 对象，不得添加 Markdown 代码围栏、正文、注释或其他文字。')
    + (submitResultMode ? '' : JSON.stringify(outputSchema));
  const loader = new DefaultResourceLoader({ cwd, agentDir, settingsManager: settings, noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true, systemPrompt });
  await loader.reload();
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
  for (const submission of submissionsTools) tools.push({
    name: submission.name, label: '提交结构化结果', description: submission.description,
    parameters: submission.parameters,
    execute: async (_callId, arguments_) => {
      const result = submission.normalize(arguments_);
      if (submittedResult !== undefined && !isDeepStrictEqual(submittedResult, result)) throw new Error('结果提交冲突');
      submittedResult = result;
      return { content: [{ type: 'text', text: '结果已提交。' }], details: {} };
    },
  });
  ({ session } = await createAgentSession({ cwd, agentDir, modelRuntime: runtime, model: runtime.getModel('skillx', modelId), thinkingLevel: process.env.SKILLX_REASONING_EFFORT || 'off', tools: tools.map(tool => tool.name), customTools: tools, resourceLoader: loader, settingsManager: settings, sessionManager: SessionManager.inMemory(cwd) }));
  const rawArguments = new Map();
  let steps = 0; let totalTokens = 0; let usageComplete = true;
  let requestId; let startedAt; let rawUsage;
  let progressCharacters = 0; let lastProgressAt = 0;
  const stream = session.agent.streamFunction;
  session.agent.streamFunction = (model, context, streamOptions) => stream(model, context, {
    ...streamOptions, maxRetries: 0, timeoutMs: (options.timeoutSeconds ?? 300) * 1000,
    fetch: usageTrackingFetch(streamOptions?.fetch ?? globalThis.fetch, usage => { rawUsage = usage; }),
    maxTokens: Math.min(options.maxOutputTokens ?? 8192, Number(args['max-output-tokens'] ?? 8192), Number(args['max-tokens'] ?? 20000)),
  });
  session.agent.onPayload = payload => {
    // Enforce the configured output protocol at the provider boundary, before tracing.
    if (submitResultMode) {
      delete payload.response_format;
      payload.tool_choice = submissionsTools.length === 1
        ? { type: 'function', function: { name: submissionsTools[0].name } } : 'required';
      payload.parallel_tool_calls = false;
    } else if (structuredMode === 'json_schema') {
      payload.response_format = { type: 'json_schema', json_schema: {
        name: 'workflow_node_output', strict: true, schema: outputSchema,
      } };
    } else if (structuredMode === 'json_object') {
      payload.response_format = { type: 'json_object' };
    }
    requestId = randomUUID(); startedAt = performance.now(); rawUsage = undefined;
    rawArguments.clear();
    progressCharacters = 0; lastProgressAt = startedAt;
    emit('model.request.dispatched', { attempt: steps + 1, requestId, model: modelId, provider: 'pi', startedAt: new Date().toISOString() });
    trace(requestId, 'request', payload);
  };
  session.agent.onResponse = response => {
    emit('model.response.started', { requestId, status: response.status, latencyMs: Math.round(performance.now() - startedAt) });
  };
  session.agent.shouldStopAfterTurn = () => submittedResult !== undefined || steps >= Number(args['max-steps'] ?? 6) || totalTokens >= Number(args['max-tokens'] ?? 20000);
  session.subscribe(event => {
    const deltaEvent = event.assistantMessageEvent;
    if (event.type === 'message_update' && deltaEvent?.type === 'toolcall_delta') {
      const index = deltaEvent.contentIndex;
      rawArguments.set(index, (rawArguments.get(index) ?? '') + deltaEvent.delta);
    }
    if (event.type === 'message_update' && event.assistantMessageEvent?.delta?.length > 0) {
      progressCharacters += event.assistantMessageEvent.delta.length;
      const now = performance.now();
      if (requestId && now - lastProgressAt >= 1000) {
        emit('model.response.progress', { requestId, receivedCharacters: progressCharacters,
          elapsedMs: Math.round(now - startedAt) });
        lastProgressAt = now;
      }
    }
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      const usage = event.message.usage ?? {};
      const inputTokens = (usage.input ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
      const outputTokens = usage.output ?? 0;
      const cacheReadTokens = cachedTokens(rawUsage);
      totalTokens += inputTokens + outputTokens;
      usageComplete &&= inputTokens + outputTokens > 0;
      const durationMs = startedAt === undefined ? 0 : Math.round(performance.now() - startedAt);
      trace(requestId ?? `pi-llm-${steps + 1}`, 'response', {
        model: modelId, content: event.message.content.map((item, index) => {
          const { thoughtSignature: _signature, thinkingSignature: _thinkingSignature, ...content } = item; return item.type === 'toolCall' ? { ...content, rawArguments: rawArguments.get(index) } : content;
        }), stopReason: event.message.stopReason, error: event.message.errorMessage,
        rawUsage: rawUsage ?? null,
        usage: { input: usage.input ?? 0, output: outputTokens, cacheRead: cacheReadTokens, cacheWrite: usage.cacheWrite ?? 0, totalTokens: inputTokens + outputTokens },
        durationMs,
      });
      emit('agent.step.completed', { step: ++steps, requestId, model: modelId, provider: 'pi', inputTokens, outputTokens,
        cacheReadTokens, cacheWriteTokens: usage.cacheWrite ?? 0, durationMs,
        usageComplete: inputTokens + outputTokens > 0 });
      if (steps > Number(args['max-steps'] ?? 6) || totalTokens > Number(args['max-tokens'] ?? 20000)) void session.abort();
    }
  });
  emit('agent.plan', { summary: 'Pi 使用 Host 授权的工具执行任务' });
  await session.prompt(args.prompt, { images, expandPromptTemplates: false });
  const message = [...session.state.messages].reverse().find(item => item.role === 'assistant');
  if (!message || ['error', 'aborted'].includes(message.stopReason)) throw new Error(message?.errorMessage || 'Pi model request failed or aborted');
  failureCode = 'MODEL_MALFORMED_OUTPUT';
  const text = message.content.filter(item => item.type === 'text').map(item => item.text).join('');
  // submit_result is an output transport, not an external operation. The SDK
  // may reject its arguments before execute() (e.g. an extra property), leaving
  // no text to parse. Preserve that candidate for the Host's authoritative
  // outputSchema validation and actionable, bounded decision repair.
  // The SDK tolerantly parses incomplete/invalid JSON as an object (sometimes
  // {}). Check the captured transport first so a malformed second submission
  // cannot be mistaken for a conflicting valid result or silently ignored.
  if (submitResultMode) {
    for (const [index, item] of message.content.entries()) {
      if (item.type !== 'toolCall' || !submissionsTools.some(tool => tool.name === item.name)) continue;
      const raw = rawArguments.get(index);
      if (raw === undefined) continue;
      try { JSON.parse(raw); } catch (error) {
        outputParseError = { kind: 'invalid_json', parseError: scrub(String(error.message)).slice(0, 1000),
          rawArguments: scrub(raw).slice(0, 65536), truncated: raw.length > 65536,
          stopReason: message.stopReason };
        throw new Error('结果提交工具参数不是合法 JSON；请只调用一次提交工具，参数使用完整 JSON，不混入 XML/DSML 文本。');
      }
    }
  }
  const submissions = submitResultMode
    ? message.content.filter(item => item.type === 'toolCall' && submissionsTools.some(tool => tool.name === item.name))
      .map(item => ({ ...item, arguments: submissionsTools.find(tool => tool.name === item.name).normalize(item.arguments) }))
    : [];
  if (submissions.some(item => !isDeepStrictEqual(item.arguments, submissions[0].arguments))) {
    throw new Error('结果提交冲突：同一响应包含不同结果，不能自动选择');
  }
  let output;
  try {
    output = submittedResult ?? (submissions.length >= 1 ? submissions[0].arguments : JSON.parse(text));
  } catch {
    throw new Error(text.includes('<｜｜DSML｜｜')
      ? '模型返回了 DSML 工具调用文本，而非约定的 JSON 对象；请检查模型服务的结构化输出支持与 structuredMode 配置。'
      : '模型返回了无法解析的 JSON 结果；请检查模型响应记录与结构化输出配置。');
  }
  if (!output || typeof output !== 'object' || Array.isArray(output)) throw new Error('模型结果必须为 JSON 对象');
  const outputProvenance = Object.fromEntries(Object.keys(output).map(field => [field, { mode: 'generative', sourceCallIds: evidence.map(item => item.callId), evidenceDigests: evidence.map(item => item.evidenceDigest), promptVersion: 'pi-sdk-bridge-v1' }]));
  emit('agent.completed', { steps, totalTokens, usageComplete, model: modelId, provider: 'pi', output, outputProvenance });
} catch (error) {
  emit('agent.failed', { code: failureCode, message: scrub(String(error?.message ?? 'Pi execution failed')).slice(0, 2000), retryable: false,
    ...(outputParseError ? { outputParseError } : {}) });
} finally {
  session?.dispose();
  process.stdin.destroy();
}
