import { isDeepStrictEqual } from 'node:util';

// Tool schemas are validated as a whole by the SDK (not compiled branch by
// branch as provider response_format schemas are). Factor repeated constraints
// without changing their meaning.
function compact(schema) {
  if (!schema || typeof schema !== 'object') return schema;
  if (Array.isArray(schema)) return schema.map(compact);
  const result = Object.fromEntries(Object.entries(schema).map(([key, value]) => [key, compact(value)]));
  if (result.properties && result.anyOf?.every(branch => branch.properties && branch.type === result.type)) {
    result.anyOf = result.anyOf.map(branch => {
      const reduced = {};
      for (const [key, value] of Object.entries(branch)) {
        if (key === 'properties') {
          const delta = Object.fromEntries(Object.entries(value).filter(([name, rule]) => !isDeepStrictEqual(rule, result.properties[name])));
          if (Object.keys(delta).length) reduced.properties = delta;
        } else if (key === 'required') {
          const extra = value.filter(name => !result.required?.includes(name));
          if (extra.length) reduced.required = extra;
        } else if (!isDeepStrictEqual(value, result[key])) reduced[key] = value;
      }
      return reduced;
    });
  }
  return result;
}

// Provider tools stay small; the Host validates the canonical decision.
export function resultTools(schema) {
  const metadata = schema['x-task-decision'];
  if (!metadata) return [{ name: 'submit_result', description: '提交最终结构化结果。', parameters: schema, normalize: value => value }];
  return schema.anyOf.map(branch => {
    const action = branch.properties.action.const;
    const defaults = action === 'complete' ? metadata.completeDefaults : {};
    const omitted = new Set(['action', ...Object.keys(defaults),
      ...Object.entries(branch.properties).filter(([, value]) => value.maxItems === 0).map(([key]) => key)]);
    const properties = Object.fromEntries(Object.entries(branch.properties).filter(([key]) => !omitted.has(key)));
    const relevant = {
      dispatch: ['summary', 'completionCriteria', 'remaining', 'calls', 'questions', 'evidence', 'evidenceGap'],
      'human.request': ['summary', 'completionCriteria', 'remaining', 'calls', 'questions', 'evidence'],
      blocked: ['summary', 'completionCriteria', 'remaining', 'answer', 'evidence'],
      complete: ['answer', 'evidence', 'learningCandidate'],
    }[action];
    for (const key of Object.keys(properties)) if (!relevant.includes(key)) delete properties[key];
    if (action === 'complete') {
      delete properties.evidenceGap;
      properties.answer = { ...properties.answer, description: '直接回答用户：只表述一次核心结论，附必要的短原文依据和改变结论的适用边界。引用必须支持紧邻判断；检查否定词、必要/充分条件和例外与结论一致，不再逐段重复结论。' };
    }
    const parameters = { type: 'object', additionalProperties: false, properties,
      required: branch.required.filter(key => !omitted.has(key)) };
    const definitions = {};
    const visit = value => {
      if (!value || typeof value !== 'object') return;
      if (value.$ref?.startsWith('#/$defs/')) {
        const name = value.$ref.slice(8);
        if (!(name in definitions)) { definitions[name] = schema.$defs[name]; visit(definitions[name]); }
      }
      Object.values(value).forEach(visit);
    };
    visit(parameters);
    if (Object.keys(definitions).length) parameters.$defs = definitions;
    return { name: `task_${action.replace('.', '_')}`,
      description: { complete: '证据已足够时提交答案。运行时保留既有完成条件，无需重新规划或重复提交。',
        dispatch: '证据不足时提交下一批实际调用；已有证据时必须指出具体缺口。',
        'human.request': '缺少用户才能提供的信息或决策时提问。', blocked: '确实无法继续时报告阻塞。' }[action],
      parameters: compact(parameters),
      // Preserve unexpected fields for Host rejection, never repair content.
      normalize: value => ({ ...defaults, ...value, action }),
    };
  });
}
