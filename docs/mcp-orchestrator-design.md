# c-orch MCP 技术设计方案

日期：2026-05-11

## 目标

`c-orch` 要做一个基于 Codex 的 Agent 工作流编排器。第一版采用 `Codex MCP` 方案：外层总控是确定性的程序，Planner 和 Worker 都通过 `codex()` 启动为真实 Codex thread，并通过 `codex-reply()` 继续对话。

项目名里的 `c` 指 Codex。这个项目的前提是：用户的 ChatGPT Pro 订阅和模型 usage 能力主要通过 Codex 身份获得，因此 Planner 和 Worker 的执行必须尽量留在 Codex runtime 内，而不是绕到一个独立 API key 驱动的外部 agent runtime。

核心目标：

- Planner 使用最强可用模型，负责方案、任务拆分、验收标准和 Worker 结果 review。
- Worker 默认使用 `gpt-5.3-codex`，负责执行 Planner 下发的具体任务。
- Planner 和 Worker 都应作为 Codex session/thread 保留，便于在 Codex Session 列表中查看过程。
- 总控负责状态机、thread 映射、重试、结果传递和本地 run manifest。
- 第一版先用 MCP 快速闭环，后续可把底层 driver 换成 Codex App Server 以获得更细粒度控制。
- 多 Worker 并行是核心目标，因此 Worker 工作区隔离是 P0 能力，而不是后续可选项。

非目标：

- 第一版不做通用多 Agent 平台。
- 第一版不实现复杂 DAG、长期任务调度、Web UI 或分布式执行。
- 第一版不让 Planner 直接递归调用 Codex Worker。Worker 创建由总控完成。

## 架构选择

选型：`Deterministic Orchestrator + Codex MCP Driver`

原因：

- `Codex MCP` 暴露 `codex()` 和 `codex-reply()`，适合快速创建和继续 Codex thread。
- Planner 和 Worker 都可以由总控显式启动，因此二者都能成为 Codex session。
- 总控保留确定性状态机，避免让 LLM 承担持久化、重试、错误恢复和状态一致性。
- MCP driver 的抽象可以被 App Server driver 替换，不锁死后续产品化路线。

实现决策：

- 第一版优先做确定性 MCP driver：由总控直接通过 stdio JSON-RPC 调用 `codex mcp-server` 的 `tools/call`，而不是让另一个模型自行选择 MCP tool。OpenAI Agents SDK 仍作为可选集成方向保留，但 c-orch 的主路径必须显式构造 tool name、arguments 和返回解析。
- 总控不能直接信任 PATH 里的 `codex`。本机同时存在 `/opt/homebrew/bin/codex` 和 `/Applications/Codex.app/Contents/Resources/codex`，前者可能是旧的裸安装 CLI，后者是 Codex.app 内置 CLI。`c-orch` 必须先探测并优先使用 Codex.app 内置 CLI，或允许用户通过配置显式指定。
- 实现前先做 `McpCodexDriver` spike，确认 `codex()` 和 `codex-reply()` 的返回结构，尤其是 `structuredContent.threadId`、最终文本和错误格式。
- 实现前先做 Session 可见性 spike，确认 Planner 和 Worker 通过 MCP 启动后是否出现在 Codex Session 列表里。
- 失败和超时第一版先做基础状态记录和最大返工次数，复杂恢复策略后续迭代。

已验证事实：

- 2026-05-11 使用 `/Applications/Codex.app/Contents/Resources/codex mcp-server` 验证，MCP stdio 采用 newline-delimited JSON-RPC。
- `initialize` 后发送 `notifications/initialized`，再调用 `tools/list` 能看到 `codex` 和 `codex-reply` 两个 tool。
- `tools/call` 调用 `codex` 后返回 `result.structuredContent.threadId` 和 `result.structuredContent.content`。
- 同一次 smoke 生成了 Codex rollout：`/Users/mac/.codex/sessions/2026/05/11/rollout-2026-05-11T17-16-03-019e1652-75b7-7bc3-973f-f7081e42722a.jsonl`，其中 `session_meta.source` 为 `mcp`，`originator` 为 `Codex Desktop`，说明通过 MCP 创建的 thread 会落入 Codex 的本地 session 记录。

参考接口：

- Codex MCP Server: https://developers.openai.com/codex/guides/agents-sdk#running-codex-as-an-mcp-server
- Codex exec: https://developers.openai.com/codex/cli/reference#codex-exec
- Codex App Server API: https://developers.openai.com/codex/app-server#api-overview
- Agents SDK: https://developers.openai.com/api/docs/guides/agents

## 总体流程

```text
User task
  -> c-orch stores a proposal in the plan proposal pool
  -> c-orch creates Planner Codex thread through codex()
  -> Planner returns plan, acceptance criteria, worker prompt
  -> c-orch records the plan and waits for human approval outside the execution queue
  -> Human approves the plan
  -> c-orch enqueues an execution task that points at the approved run
  -> c-orch creates Worker Codex thread through codex()
  -> Worker implements and returns result evidence
  -> c-orch sends Worker result to Planner through codex-reply()
  -> Planner accepts the work or requests a concrete Worker revision
  -> c-orch either applies, reworks, or records a system failure
```

## 当前实现状态

截至 2026-05-11，本 repo 已实现单 Worker MVP 的本地骨架：

- `c-orch doctor` 能探测 Codex.app 内置 CLI 和 PATH 里的裸 Codex CLI，并优先选择 Codex.app 内置 CLI。
- `McpCodexDriver` 使用 stdlib newline-delimited JSON-RPC 直接调用 `codex mcp-server`，支持 `tools/list`、`codex` 和 `codex-reply`。
- `RunStore` 保存 `runs/<run_id>/manifest.json`。
- `ProposalStore` 保存 `.c-orch/tasks/proposals.json`，用于 dashboard 新任务的方案池。方案池负责 Planner 生成方案和人工 approve/revise；执行队列只接收已经 approve 的 plan。
- Worker 默认创建独立 git worktree；目标 repo 必须已有可解析的 committed base ref。
- `RunOrchestrator` 支持 Planner plan、Worker 执行、diff evidence、verification output、Planner review、`revision_requested` 返工和最大尝试次数。
- `COrchRuntime` 是 dashboard 后端控制面入口。前端/HTTP handler 只负责展示状态和转发用户意图，action 校验、Codex driver 创建、Orchestrator 调用和 manifest 更新都在 runtime 层完成。dashboard server 会在进程生命周期内保留同一个 runtime，并可复用长活 MCP driver；但 MCP 内存状态只作为 cache，可靠恢复仍以 manifest、event log、Codex thread id 和 `codex exec resume` 为准。
- `task_lifecycle.py` 将 Task 作为用户级状态机、Run 作为一次执行 attempt。它负责从 `active_run_id` 收敛 task/queue 状态，并派生 `waiting_for` / `next_action`。例如 active run 进入 `FAILED` 后，task 会收敛为 `FAILED`，queue 为 `FAILED`，等待点为 `retry_task`，而不是继续卡在 `RUNNING`。`queue retry <task_id>` 和 dashboard 的“重新排队”会保留旧 `run_ids`、清空 `active_run_id`、把 task 设回 `PENDING`。当 dashboard backend 拥有执行配置时，队首 `PENDING` task 会自动触发 scheduler 创建新的 run attempt；`queue run` 仍是手动触发同一调度路径的 CLI 入口。
- `failure_policy.py` 将可恢复异常从业务状态机中拆出。比如 Planner review 的 Codex session 丢失时，run 保持 `WORK_DONE`，review attempt 记录 `FAILED_RETRYABLE`，UI/CLI 再提供重试动作。
- `McpCodexDriver.reply()` 已验证：fresh MCP server 对旧 session id 调 `codex-reply` 会返回 `Session not found`，但 `codex exec resume <session-id>` 可以恢复同一个磁盘 session。因此 reply 先走 MCP 快路径，遇到 session-not-found 时降级到 CLI resume；CLI resume 也失败后，才交给 failure policy / replacement agent 兜底。
- `c-orch run --prepare-only` 只建 manifest/worktree，不调用 Codex。
- `c-orch run` 默认会通过 MCP 创建 Planner Codex session，把方案写入 manifest，然后暂停在 `PLAN_REVIEW_REQUIRED`。
- `c-orch resume --approve-plan <run_id>` 会标记人类已通过 Planner 方案，然后创建 Worker Codex session。
- `c-orch run --auto-approve-plan` 可跳过人类方案审批点，直接延续旧的一次性执行流程。
- Dashboard 新任务默认走 proposal pool：Planner 方案被人工通过后，runtime 会把同一个 run 标记为 `PLAN_APPROVED`，再创建 execution queue task。scheduler 看到这个 task 已绑定 approved run 时，会继续该 run，而不是重建 Planner run。

## 组件

### Orchestrator

确定性控制层，负责：

- 生成 `run_id`。
- 创建 Planner thread。
- 解析 Planner 的结构化输出。
- 创建 Worker thread。
- 把 Worker 输出、diff、测试摘要发回 Planner。
- 控制 `accepted` / `revision_requested` 这两个 review 业务结果，以及系统层失败恢复。
- 保存 `runs/<run_id>/manifest.json`。
- 控制最大返工次数。

它不负责：

- 判断实现方案好坏。
- 写代码。
- 做语义 review。

这些都交给 Planner 和 Worker。

### Planner

Planner 是一个 Codex thread，不是普通 SDK agent。它的职责：

- 理解用户任务。
- 输出可执行计划。
- 定义验收标准。
- 生成 Worker prompt。
- Review Worker 输出。
- 决定是否验收通过，或给 Worker 一个具体返工指令。

模型选择：

- 优先使用 `gpt-5.5`，如果本机 model catalog 不可用则回退到 `gpt-5.4`。
- 第一版可通过配置项 `planner_model` 指定。
- 新建 run 默认使用 `model_reasoning_effort=high`；`resume` 只在显式传参时覆盖已有 manifest。
- 注意 Codex Desktop 当前会话模型可用性和 PATH 中 `codex` 的 model catalog 不是同一个边界。2026-05-11 本机 `/opt/homebrew/bin/codex` 是 `codex-cli 0.122.0`，未列出 `gpt-5.5`；直接运行 `codex exec -m gpt-5.5` 会失败，错误为该模型需要更新版本的 Codex。同机 `/Applications/Codex.app/Contents/Resources/codex` 是 `codex-cli 0.130.0-alpha.5`，列出并成功调用 `gpt-5.5`。因此实现必须支持 Codex binary 探测、模型探测和 fallback。

### Worker

Worker 也是 Codex thread。它的职责：

- 执行 Planner 下发的具体任务。
- 尽量保持改动范围窄。
- 运行验证命令。
- 输出变更摘要、验证证据和问题说明。

模型选择：

- 默认 `gpt-5.3-codex`。
- 返工超过阈值时，当前 MVP 进入 `FAILED`；升级模型或拆任务后续再作为明确功能设计。

工作区隔离：

- 每个 Worker 默认使用独立 git worktree，避免多 Worker 并行时互相覆盖改动。
- 总控负责创建 worktree、记录 worktree path、收集 diff，并在合并前让 Planner review。
- MVP 即使只跑单 Worker，也应把 worktree 抽象放进数据模型，避免后续并行改造时重写状态层。

### CodexDriver

总控只依赖统一 driver 接口：

```text
start_session(role, model, cwd, prompt) -> SessionResult
reply(thread_id, prompt) -> SessionResult
```

第一版实现：

```text
McpCodexDriver
  -> codex(prompt, model, cwd, sandbox, approval-policy)
  -> codex-reply(threadId, prompt)
```

第一版通过直接 MCP stdio client 启动 MCP server：

```text
<resolved_codex_binary> mcp-server
  -> initialize
  -> notifications/initialized
  -> tools/call { name: "codex", arguments: ... }
  -> tools/call { name: "codex-reply", arguments: ... }
```

如果后续发现 Agents SDK 暴露了足够直接的 local MCP tool-call API，可以在 `CodexDriver` 后面补一个 `AgentsSdkCodexDriver`；但主流程不依赖模型中介来决定调用哪个 MCP tool。

Codex binary 解析顺序：

```text
1. --codex-bin / C_ORCH_CODEX_BIN
2. /Applications/Codex.app/Contents/Resources/codex
3. PATH 中的 codex
```

解析后必须记录：

```text
codex_binary_path
codex_cli_version
available_models
planner_model_selected
worker_model_selected
```

后续实现：

```text
AppServerCodexDriver
  -> thread/start
  -> thread/name/set
  -> turn/start
  -> thread/read
  -> turn/interrupt
```

## 状态机

当前业务状态：

```text
NEW
PLANNING
PLAN_READY
PLAN_REVIEW_REQUIRED
PLAN_APPROVED
WORKING
WORK_DONE
REVIEWING
REVISION_REQUESTED
APPROVED
FAILED
```

状态转移：

```text
NEW -> PLANNING
PLANNING -> PLAN_READY
PLAN_READY -> PLAN_REVIEW_REQUIRED
PLAN_REVIEW_REQUIRED -> PLAN_APPROVED
PLAN_APPROVED -> WORKING
PLAN_READY -> WORKING
WORKING -> WORK_DONE
WORK_DONE -> REVIEWING
REVIEWING -> APPROVED
REVIEWING -> REVISION_REQUESTED
REVIEWING -> FAILED
REVISION_REQUESTED -> WORKING
```

Planner review 的正常业务输出只允许：

```text
accepted
revision_requested
```

异常恢复不是业务状态。MCP、Codex session、JSON 解析、验证命令或 apply
失败等系统问题由 c-orch 的 failure policy、review attempts 和 events 记录。
例如 review 调用失败后，run 回到 `WORK_DONE`，并通过 `FAILED_RETRYABLE`
review attempt 表示可以重试复核。

返工规则：

- `revision_requested` 后优先复用同一个 Worker session。当前实现先尝试 MCP `codex-reply()`；如果 fresh MCP server 不认识旧 session id，则自动用 `codex exec resume <session-id>` 继续同一个 Codex session。只有 CLI resume 也失败时，才在同一 worktree 中启动 replacement Worker 执行同一个返工 prompt。
- 默认最多返工 3 次。
- 超过 3 次后进入 `FAILED`。

## 数据模型

每个 run 保存一个 manifest：

```json
{
  "run_id": "2026-05-11-001",
  "cwd": "/Users/mac/projs/target-project",
  "user_task": "Implement ...",
  "status": "REVIEWING",
  "planner": {
    "thread_id": "019...",
    "model": "gpt-5.4",
    "codex_binary_path": "/Applications/Codex.app/Contents/Resources/codex",
    "status": "ACTIVE"
  },
  "workers": [
    {
      "id": "worker-1",
      "thread_id": "019...",
      "model": "gpt-5.3-codex",
      "worktree_path": "/Users/mac/projs/.c-orch/worktrees/2026-05-11-001/worker-1",
      "status": "DONE",
      "attempt": 1
    }
  ],
  "acceptance_criteria": [
    "Tests pass",
    "No unrelated changes",
    "Behavior matches request"
  ],
  "verification_commands": [
    "npm test"
  ],
  "review": {
    "decision": "revision_requested",
    "reason": "Missing regression test",
    "next_worker_prompt": "Add a regression test for ...",
    "evidence_files": [
      "runs/2026-05-11-001/evidence/git-diff-summary.md",
      "runs/2026-05-11-001/evidence/git-diff.patch"
    ]
  },
  "created_at": "2026-05-11T16:00:00+08:00",
  "updated_at": "2026-05-11T16:20:00+08:00"
}
```

第一版用文件系统保存：

```text
runs/
  <run_id>/
    manifest.json
    planner-output.json
    worker-1-output.json
    review-1-output.json
    evidence/
      git-diff-summary.md
      git-diff.patch
      test-output.txt
```

## 输出契约

Planner 初始输出必须包含 JSON：

```json
{
  "status": "plan_ready",
  "summary": "Short plan summary",
  "acceptance_criteria": [
    "Criterion 1"
  ],
  "worker_prompt": "Worker instructions",
  "verification_commands": [
    "pytest"
  ],
  "risk_notes": [
    "Risk note"
  ]
}
```

Worker 输出必须包含 JSON：

```json
{
  "status": "work_done",
  "summary": "What changed",
  "changed_files": [
    "src/example.py"
  ],
  "verification": [
    {
      "command": "pytest",
      "status": "passed",
      "summary": "All tests passed"
    }
  ],
  "blockers": []
}
```

Planner review 输出必须包含 JSON：

```json
{
  "decision": "accepted",
  "reason": "All acceptance criteria are satisfied.",
  "next_worker_prompt": null
}
```

返工时：

```json
{
  "decision": "revision_requested",
  "reason": "Missing regression coverage.",
  "next_worker_prompt": "Add regression tests for ..."
}
```

## Prompt 策略

总控给 Planner 的初始 prompt 应包含：

- 用户原始任务。
- 当前 cwd。
- Planner 角色边界：只计划和 review，不直接改代码。
- 输出 JSON 契约。
- 验收标准必须可验证。
- Worker prompt 必须足够自包含。

总控给 Worker 的 prompt 应包含：

- Planner 生成的任务说明。
- 验收标准。
- 验证命令。
- 修改范围约束。
- 输出 JSON 契约。
- 明确说明它不是唯一 agent，不应回退或覆盖他人无关改动。

总控给 Planner review 的 prompt 应包含：

- Planner 原计划。
- Worker 输出。
- 当前 git diff 摘要。
- git diff 详情文件路径，例如 `runs/<run_id>/evidence/git-diff.patch`。
- 验证命令结果摘要和详情文件路径。
- 要求输出 `accepted` 或 `revision_requested`。异常恢复由 c-orch 处理，不由 Planner review JSON 表达。

证据策略：

- 总控直接把 git diff 摘要放进 review prompt，避免 Planner 为了判断高层风险还要先读大文件。
- 总控同时把完整 diff 写入 evidence 文件，让 Planner 在需要细看时读取文件。
- 测试输出也采用同样策略：prompt 内放摘要，文件内放详情。

## CLI 形态

第一版命令：

```bash
c-orch run --cwd /Users/mac/projs/example "Implement feature X"
```

常用参数：

```text
--codex-bin /Applications/Codex.app/Contents/Resources/codex
--planner-model gpt-5.4
--worker-model gpt-5.3-codex
--max-rework 3
--sandbox workspace-write
--approval-policy never
--runs-dir runs
--worktrees-dir .c-orch/worktrees
```

辅助命令：

```bash
c-orch status <run_id>
c-orch resume <run_id>
c-orch show <run_id>
```

## 错误处理

第一版需要处理：

- MCP server 启动失败。
- 解析到旧版 Codex CLI，导致目标模型不可用。
- `codex()` 没有返回 `threadId`。
- `gpt-5.5` 等指定模型在 Desktop 可用但 CLI/MCP 不可用。
- Planner 输出不是合法 JSON。
- Worker 输出不是合法 JSON。
- Worker 执行失败或超时。
- Planner review 不给明确 decision。
- 返工次数超过阈值。
- Worker worktree 创建失败或合并冲突。

处理策略：

- 所有失败写入 manifest。
- 记录实际使用的 Codex binary 路径和版本。
- 模型不可用时按配置 fallback，并把实际使用模型写入 manifest。
- 可重试的输出解析错误，给同一 thread 发 `codex-reply()` 要求按 schema 重发。
- Worker 失败时先让 Planner review 失败证据，再决定返工或失败。
- 总控崩溃后可通过 manifest 和 threadId 恢复。

## Session 可见性

MCP 方案下，Planner 和 Worker 都通过 `codex()` 创建，因此它们都有各自的 Codex thread。

第一版可见性边界：

- 可以在 Codex session/thread 记录中看到 Planner 和 Worker。
- MCP driver 未必能原生命名 thread。
- 总控必须在 manifest 里记录 threadId 和 role 映射。

产品化增强：

- 使用 App Server driver 的 `thread/name/set` 给 session 命名。
- 使用 `thread/list`、`thread/read` 对齐 Codex Desktop 的列表和历史。
- 使用事件流展示实时进度。

## 后续迁移到 App Server

为了避免重写业务逻辑，第一版必须把 Codex 调用封装在 `CodexDriver` 后面。

迁移收益：

- 更明确的 thread/turn 控制。
- 可设置 thread 名称。
- 可读取完整历史。
- 可订阅实时事件。
- 可中断、fork、compact、review/start。

迁移时保持不变：

- Orchestrator 状态机。
- manifest 数据模型。
- Planner/Worker 输出契约。
- prompt 模板。

只替换：

- `McpCodexDriver` -> `AppServerCodexDriver`

## MVP 验收标准

MVP 完成时应满足：

- `git init` 后项目可运行本地 CLI。
- `c-orch doctor` 能识别 Codex.app 内置 CLI 和 PATH 中裸安装 CLI，并说明实际使用哪一个。
- `c-orch run` 能创建 Planner 和 Worker 两个 Codex thread。
- manifest 能记录 `planner.thread_id` 和 `worker.thread_id`。
- manifest 能记录每个 Worker 的 worktree path。
- Planner 能输出 plan、acceptance criteria 和 worker prompt。
- Worker 能执行任务并输出结构化结果。
- Worker 默认在独立 worktree 中执行。
- Planner 能 review Worker 结果并输出 `accepted` 或 `revision_requested`。
- Planner review prompt 包含 git diff 摘要，并能引用完整 diff 文件。
- `revision_requested` 时总控能复用 Worker thread 返工；Worker thread 丢失时能在同一 worktree 中启动 replacement Worker。
- 超过最大返工次数时 run 进入失败状态。

## 风险

- MCP 是高层工具接口，运行时控制粒度弱于 App Server。
- Planner/Worker JSON 输出可能不稳定，需要 schema 修复回合。
- Codex session 列表中 thread 命名能力有限，需要 manifest 补足。
- Codex Desktop 当前会话可用模型和 Codex CLI/MCP 可用模型可能不一致，需要运行时探测。
- 长任务的实时可观测性不足，产品化阶段应迁移到 App Server。
- 多 Worker 并发会引入工作区冲突，因此 MVP 阶段就应支持 git worktree 隔离。

## 实施顺序

1. 建立 Python CLI 和配置文件。
2. 实现 `doctor`，探测 Codex binary、版本、模型目录和 `gpt-5.5` 可调用性。
3. 实现 `RunStore`，读写 manifest。
4. 实现 `CodexDriver` 接口。
5. 实现 `McpCodexDriver`。
6. Spike MCP 返回结构和 Session 可见性。已完成。
7. 实现模型探测和 fallback。
8. 实现 Worker worktree 创建和 diff evidence 采集。
9. 实现 Planner 初始 prompt 和 JSON 解析。
10. 实现 Worker prompt、执行和 JSON 解析。
11. 实现 Planner review 和返工循环。
12. 加入 `status`、`resume`、`show` 命令。
13. 补最小测试：状态转移、manifest 读写、JSON 解析失败修复、worktree path 记录。
14. 评估是否切到 App Server driver。
