# NEXT_STEPS-018：主流程通过后的保通清单

依据：[`REPLAY-017.md`](REPLAY-017.md)、
[`review/design-compliance-review-018.md`](review/design-compliance-review-018.md)、
[`review/code-review-018.md`](review/code-review-018.md)。

范围：单用户 Local Demo；不做鉴权/RBAC、多租户和分布式部署。

## 1. 结论

018 没有剩余的主流程修复任务。以下目标已经在真实环境通过：

```text
首次探索 → 固化 → 人工发布 → 新对话复用 → 结果返回
任一步真实失败 → checkpoint → 跨 Worker 接力 → 原对话最终结果
```

下一步不应继续为闭环补功能；应冻结当前行为契约，优先防止回归。只有新增需求或真实故障证据出现时，再进入
新的实现版本。

## 2. 提交前文档整理（非功能阻塞）

- [ ] 确认修复说明的正式文件名是 `REPLAY-017.md`。
- [ ] 将其一级标题从“REPLAY-018”统一为“REPLAY-017”。
- [ ] 清理末尾多余空行。
- [ ] 处理 Git 中 `D REPLAY-018.md` 与 `?? REPLAY-017.md` 的改名状态，确保不是意外删除/遗漏跟踪。
- [ ] 运行 `git diff --check` 并确认包含未跟踪文档的最终提交内容无空白错误。

以上只修正文档编号和版本控制状态，不修改已经通过的业务实现。

## 3. 必须持续保留的回归门禁

- [ ] `make demo-check` 保持全绿。
- [ ] `make browser-ui-check` 必须继续包含两条独立场景：正常主流程、真实 503 failure handoff。
- [ ] failure handoff 不得退化为单测、HTTP 冒烟或 Workflow `INVALID_INPUT`。
- [ ] source Run 必须保留 failed，禁止用 continuation success 覆盖历史。
- [ ] continuation message、Run 和 checkpoint CAS 必须保持唯一、原子、可恢复。
- [ ] 原 Chat 必须无手动刷新显示最终 output；reload 后正文只能有一份。
- [ ] Browser/lookup/file 三类自然入口和 remaining-only continuation 继续共用同一通配框架。
- [ ] checkpoint chain 必须持续覆盖 success、failure、cancel、awaiting_user、budget 和 integrity terminal。

## 4. 新需求进入条件

只有以下情况之一发生时，才需要创建下一修复/设计版本：

- 真实端到端门禁复现新的用户可见阻塞；
- 新增 capability、外部系统或副作用等级；
- 从 Local Demo 扩展到鉴权、多租户或分布式 Worker；
- 多代 continuation 的真实产品场景暴露当前 generation/观察模型不足；
- 性能数据证明 Conversation/checkpoint 轮询需要改为推送，而不是仅凭架构偏好提前重写。

## 5. 当前完成矩阵

| 门禁 | 状态 |
|---|---|
| Natural exploration | passed |
| Distill/check/publish | passed |
| L0/L1 progressive reuse | passed |
| Workflow result delivery | passed |
| Real 503 source evidence | passed |
| Cross-worker remaining-only continuation | passed |
| Continuation Conversation linkage | passed |
| Automatic final result delivery | passed |
| Reload/idempotency | passed |
| Run/checkpoint/lineage observability | passed |
| Full quality gates | passed |

**当前版本可以直接判定通过。**
