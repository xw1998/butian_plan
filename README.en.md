# Butian Plan

[简体中文](README.md) | **English**

> **Practice is the sole criterion for testing truth.**

AI can answer questions. Working with it over time asks for more: remembering your requirements, resuming unfinished work, reusing methods that worked, learning from failures, and carrying complex tasks through to a result you can review.

**Butian adds that working layer to the AI application you already use.** Through MCP, your host AI can access tools for tasks, memory, context recovery, evidence, and collaboration. Butian preserves progress and experience between interactions while your familiar AI handles reasoning and communication. No additional Ollama installation or local model is required.

## Shortest path to first use (read this first)

The sections after this one explain mechanisms; you do not need to read them before a first run.

**Three steps, about three minutes**

1. Install once: `python -m pip install -e .` (Python 3.11+, standard library only, no Ollama and no model API key).
2. Connect once: call `butian_connect` over MCP (loopback has no token; with more than one member you must pass `member_id` explicitly), then `butian_context` for the current member, mode, visible tasks and next legal action.
3. Deliver once: `butian_goal_submit` records the goal and acceptance criteria → copy the `next_actions` returned by `butian_context` → `butian_evidence_record` registers real evidence → `butian_task_handoff` (or `butian_execution_submit`) delivers.

Only three words matter: **connect → mode → context** (`butian_connect`, `butian_mode`, `butian_context`). There are 124 tools, but you do not need the list up front: `tools/list` returns all of them in one page — including the ones that move a task forward and close it out (`task_claim`, `task_transition`, `task_review`, `execution_submit`, `context_compose`, `evidence_record`, `task_handoff`) — `butian_context` returns ready-to-copy arguments for the current state, and `butian_tools_catalog` (`query` substring filter) plus `butian_tool_schema` answer “which tool, which parameters”.

The one thing that trips people up: **`butian_goal_submit` only registers a goal, it does not mean the work is done.** A task counts only after it was actually performed and left verifiable evidence.

### Do not use Butian for light tasks

Single-turn work — one question, one paragraph, one code completion — is faster with the host AI alone. A full Butian cycle has fixed overhead: goal, context, evidence, handoff, review. Its payoff is continuity across sessions, reusable experience, evidence-based review and team separation of duties, not raw generation speed.

The test is simple: use it when the work must continue later, must leave reviewable evidence, or needs several members to divide and cross-check.

### MCP transport: Chinese text, tokens and duplicate goals

- **UTF-8 end to end**: both the MCP server and the CLI read and write bytes as UTF-8 instead of inheriting the Windows ANSI code page. Chinese stays readable even when the host does not set `PYTHONUTF8`, and submitting Chinese no longer fails with `surrogates not allowed`; lone surrogates that arrive from a mis-decoding peer are repaired reversibly before anything is stored or echoed.
- **`token` is optional**: loopback needs none and remote member access is the only case that does. It defaults to the value from `butian_connect` and is no longer listed in `required`, so you stop passing empty strings to satisfy the schema.
- **Duplicated fields no longer fail**: when `acceptance_criteria` appears both at the top level and inside `problem`, lists are merged and scalars take the top-level value, echoed back as `problem_fields_merged` instead of raising `problem_field_conflict`.
- **Duplicate goals are idempotent**: resubmitting the same unfinished goal in the same mode returns the existing task (`created: false`, `reused_existing: true`) with `next_actions`, instead of leaving a second invisible `todo`. Team and solo goals deduplicate separately.
- **Submission returns the entry point**: `butian_goal_submit` includes `next_actions` with the advancing call (and, in team mode, the host-execution step for each subtask whose assignee has no Provider), so nothing has to guess `revision` or `operation_key`.

### Team mode works without a Provider

Without a Provider, team subtasks do not run by themselves — but that is not a dead end. `butian_task_team_run` returns those subtasks as `awaiting_external_execution` with the whole loop spelled out in `next_actions`, and `host_execution_reason` says which case applies (task created with `execution_mode=host`, member has no Provider, or Provider is `manual`).

1. `butian_connect(member_id="<assignee>")` to act as that member — output and memory stay under its name, it is not ghostwriting;
2. `butian_execution_export` returns the execution context bound to the task revision and moves eligible tasks to running;
3. the host AI performs the work, then `butian_evidence_record` registers the evidence;
4. `butian_execution_submit` with `expected_revision`, `problem_revision` and a unique `submission_id` returns the task to review;
5. switch back and call `butian_task_team_run` to aggregate; the team goal reaches final review only after the dispatcher reviews the children.

A team parent cannot be exported before all children are `done` (`team_children_not_reviewed`), so the context only offers export for subtasks — never a call that is guaranteed to be rejected.

## What can Butian improve?

- **Remember more of what matters to you.** Store requirements, corrections, project background, and confirmed findings as searchable long-term memory. Bring relevant information back into later tasks and spend less time repeating yourself.
- **Keep long-running work moving.** Preserve goals, progress, evidence, open questions, and next steps. After context compression, a handoff, or a service restart, the AI has a concrete starting point for continuing the work.
- **Reuse successful methods.** Capture what was actually done, the environment it worked in, and the observed result. Similar tasks can draw on verified procedures, reducing repeated searches and trial and error.
- **Turn mistakes into better practice.** Record failure conditions and counterevidence. Conflicting experience can be disputed, revised, and tested again; outdated methods can acquire a new version instead of being reused indefinitely.
- **Coordinate complex work.** Let a lead agent organize research, implementation, testing, and review. With suitable sub-agent execution interfaces, it can assign tasks, collect handoffs, request revisions, and assemble the final delivery.
- **Find the next useful question.** The “dreaming” workflow organizes accumulated material so the host AI can propose connections, contradictions, and candidate hypotheses, then arrange verification. Verified findings can inform subsequent work.

These improvements operate through external memory, tools, and workflows, and require the host AI to use them during tasks. Butian keeps experience available for future work; it does not directly update the underlying model’s parameters.

## Where does it help most?

**Long-term software and engineering projects.** Finish a module today and resume from the remaining work next time. Preserve a tool-specific failure together with its version, environment, and verification steps for future tasks.

**Recurring work.** Build reusable methods for reports, research, troubleshooting, and similar tasks. Refine those methods as exceptions appear.

**Complex tasks that need specialization and review.** Give research, implementation, testing, and review clear responsibilities, with a lead agent bringing the results and evidence together.

## How it works

A lead agent communicates with you, clarifies the goal, breaks down the work, coordinates execution and review, and delivers the result. Sub-agents take on specialized roles. For simpler tasks, a single agent can work directly with you using its own persistent memory.

```text
You
 ↕
Lead agent
 ├─ Research agent
 ├─ Implementation agent
 ├─ Testing agent
 └─ Review agent
```

Research paper: **Computational Modeling of Human Experience Formation through Practice**.

- English: [Markdown](docs/论文/补天多轮经验机制-English.md) · [Word](docs/论文/补天多轮经验机制-English.docx) · [PDF](docs/论文/补天多轮经验机制-English.pdf)
- 中文：[Markdown](docs/论文/补天多轮经验机制.md) · [Word](docs/论文/补天多轮经验机制.docx) · [PDF](docs/论文/补天多轮经验机制.pdf)

The paper describes experience formation, revision, verification, reuse, experimental costs, and limitations.

## Features and mechanisms in detail

The following describes the current business modules. The host AI supplies reasoning and content generation; Butian persists state, coordinates execution, checks admission, and records evidence. Actual execution depends on the configured host, provider capabilities, and current permissions.

### 1. Agent identity, roles, and problem-solving strategies

Members have independent IDs, names, persona descriptions, responsibilities, providers, models, and default strategies. Initialization imports the ten public profiles and their method knowledge idempotently, preserving existing identity changes and user memories. Identity participates in task ownership, memory access, tool ownership, and evidence checks.

Eight strategies cover experience reuse, model building, evidence review, experimental iteration, design synthesis, risk-first exploration, user context, and adversarial audit. Strategies can be recommended or composed from task features. Dispatcher overrides retain the strategy version, stages, reason, and decision maker; execution records the stages actually used. A strategy changes investigation order and cannot bypass evidence, acceptance, or authorization. No strategy is claimed to have established statistical superiority.

### 2. Problem definitions, clarification, and corrected requirements

Tasks retain structured problem definitions, including goals, constraints, and acceptance requirements, with revisions. Questions create persistent records and a waiting-user state; answers link to recovery. When requirements change, task revisions, problem revisions, and execution bindings prevent an old result from silently satisfying the new request.

Exported execution packages bind the actor, task, problem revision, context, and explicitly referenced methods. Method content and verification information are snapshotted and checked again at submission. Archived, disputed, replaced, or altered methods cannot masquerade as the original verified procedure. Recovery checks current eligibility rather than assuming an old session remains valid.

### 3. Solo work, team coordination, and review

Solo mode assigns a goal to one member. Team mode lets a dispatcher organize plans and subtasks, assign workers, collect structured handoffs, and assemble a delivery. Both host-driven teamwork and optional provider execution are supported. Peer questions, answers, methods, and verification records support learning between members.

The task state machine covers todo, ready, claimed, running, review, and done, plus blocked, failed, waiting_user, cancelling, cancelled, provider_lost, and unknown. Transitions check allowed paths and revisions. Submitted work requires review before approval; reviewers can request bounded rework. A tool response, agent narrative, or successful echo is not itself acceptance evidence.

### 4. Context budgets, handoffs, and restart recovery

Context composition budgets current instructions, task state, recent events, relevant memory, and historical indexes, reporting trimming. Explicit methods also consume a budget. Requirements, corrections, open questions, and next steps must be persisted rather than entrusted solely to the conversation window.

Snapshots retain parent relationships, event ranges, and integrity information. Handoffs include task revisions, state, environment, evidence, unfinished work, and next steps. Recovery distinguishes completed work, active executions, and uncertain outcomes. Unconfirmed side effects remain unknown rather than being reported as success or automatically replayed.

### 5. Independent memory, practice feedback, and recall audits

Members have isolated memory spaces containing methods, experience, lessons, provenance, environment, evidence, applicability, and invalidation conditions. Recall uses the query and environment. Unconfirmed software versions remain unknown rather than silently merging behavior across incompatible environments.

Practice records success, failure, or unknown with independent observation identifiers and duplicate checks. Repeated outcomes use log1p saturation, limiting the benefit of repetition. Recall feedback records which memories were used, useful, harmful, or missed and retains supporting evidence, making practical usefulness auditable.

User objections can become counterevidence leads. Disputes require checking inputs, versions, configuration, reproduction, and alternative explanations. An unverified objection is not conclusive falsification, while a disputed old method must not continue as an unquestioned fact.

### 6. Aging, rewarming, revision chains, and skill evolution

Memory moves through hot, warm, cold, and archived stages. Maintenance considers age, verification, repeated failure, contradiction, dependency drift, and environment changes. Cooling preserves history; rewarming needs new practice and evidence. Improved methods retain revision chains instead of merely restoring an old score.

Memory composition references component memories and records combination benefits, redundancy, and conflicts. Domain skills retain stages, marginal changes, contradictions, new environments, independent successes, composition successes, and regression evidence. Validation batches track samples, passes, failures, and evidence. This is runtime knowledge and method evolution, not neural parameter training.

### 7. Dreaming, distillation, abstraction, and verification

Dreaming uses existing fragments and is gated by new material, minimum fragment counts, intervals, locks, operation limits, and cost budgets. Material is organized using weight, evidence, and recent verification. Maintenance can dispatch work for the host AI; it does not acquire a model automatically.

The workflow supports candidate generation, distillation, merge and conflict records, abstraction, applicability and failure conditions, hypotheses, verification actions, and observations. Dream outputs remain candidates until tested. Subsequent real tasks provide the independent practice needed for experience promotion; counterexamples trigger dispute and revision.

### 8. External sources and traceable extraction

Sources record locators, titles, SHA-256 hashes, capture time, and metadata. Explicit public-URL capture stores and verifies body snapshots with bounded reading. A learning queue records claims, leases, renewals, runs, and failures. Address, redirect, and response-size checks do not constitute complete network isolation.

Claims retain conditions, limitations, predictions, and falsifiers. Extraction tasks bind their original source; publication checks source identity and quotations to prevent substituted sources or fabricated citations. Claims can become priors for separately prepared practice hypotheses. Captured text and initial method cards remain unverified knowledge rather than demonstrated experience.

### 9. Hypotheses, exploration budgets, and observations

Hypotheses retain priors, rationale, conditions, predictions, and falsifiers. Actions declare cost, risk, and expected information gain and require explicit authorization before starting. Creating an action or having budget does not grant tool permissions. Global and member policies limit action counts, declared costs, and risks within a time window; started attempts remain charged after failure or unknown outcomes.

Observations use independent identifiers. Support and rejection require valid evidence accessible to the caller. Changed sources are rechecked before validation. Beta updates, information gain, and Thompson Sampling have mathematical modules and research controls; they are not automatically the posterior behind every production confidence field or a complete Bayesian RL system.

### 10. Evidence ownership, access, and validity

File evidence records its path, owner, content fingerprint, and optional expiry. Owners can grant or revoke member access. Reviews, observations, tool verification, and workflow admission apply the relevant evidence checks. Changed content at the same path, expiry, or revoked access can invalidate earlier proof.

The ledger supports traceability but does not replace business judgment: an existing file is not necessarily a correct result, and a self-reported success is not independent verification. Current actors and task ownership participate in access checks; old permissions do not automatically transfer to a new task or version.

### 11. Owned tools, economic admission, and execution

Tools retain owners, purposes, source versions, manifests, input/output contracts, and hashes. Proposals record costs and expected benefits. Verification, economic admission, and lifecycle state determine eligibility; bundled tools begin as draft. A new version requires the applicable checks again.

Before execution, checks cover actor, task, version, source and contract, verification, circuit breakers, host authorization, and budgets. Records retain inputs, outputs, duration, state, and audit information. Running executions can be queried or cancelled; audit replay and recovery preserve uncertainty. Cancellation and timeout claims depend on actual backend results. Work-directory accounting occurs before and after execution and is not an operating-system runtime hard limit; isolation guarantees depend on the backend's real capabilities.

### 12. Sharing, trials, repair, and escalation

Owners share tools with named recipients, who accept and trial them with evidence. Sharing does not transfer the owner's host authorization. Issue workflows cover reporting, triage, requests for information, responses, duplicate links, repair versions, confirmation by the original reporter, and independent security review.

Due issues can generate escalation work items for eligible members to claim, resolve, or return, with persistent linkage and deduplication. Circuit breakers prevent continued use of affected versions and require recovery conditions. Archival checks active executions and workflow references. Restoration does not bypass unresolved faults, invalid evidence, or current admission requirements.

### 13. Portable tool bundles and versioned workflows

Bundles export source, continuous versions, and allowlisted public contracts with source, manifest, and bundle hashes. Imports check format, size, versions, and declarations and create draft tools requiring fresh verification and authorization. Private memories, runtime databases, and host permissions do not migrate. License and scan declarations are caller assertions, not proof of an external security scan.

Workflows pin tool versions and source/contract hashes and declare permissions, failure conditions, total timeout, and output budgets. Every step uses existing tool admission and auditing. Activation requires real composite validation from at least two different tasks with nonoverlapping evidence content. Individual tool verification is insufficient. Invalid dependencies or evidence require reassessment. Interrupted work becomes unknown without side-effect replay; multi-step dry runs without real intermediate output cannot claim successful rehearsal.

### 14. Providers and host-application integration

The provider layer negotiates capabilities and offers local-process, HTTP, MCP, and manual handoff paths. Start, consume, cancellation, session ownership, and task revisions remain bound. Host MCP mode uses the current AI's reasoning and tools without deploying another model. Independent execution requires the corresponding optional provider configuration.

Missing capabilities return unsupported or unknown. Echo providers prove transport and state handling only. Real model responses, browser interaction, Office rendering, device work, and external services require capable hosts or backends with their own evidence. Historical member migration is explicitly disabled; initial public-asset import is not user-history migration.

### 15. Local services, transactions, audits, and backups

CLI, stdio MCP, and local HTTP share the runtime. The Web workspace exposes Overview, Tasks, Verification, Memory, Evolution, and Members. SQLite stores tasks, events, execution ledgers, and member indexes; separate member stores retain knowledge. Critical operations use locks, write reservations, or savepoints. There is no universal atomic transaction across independent databases, filesystems, and service events; recovery records address partial completion.

Backup creation, verification, and restoration are supported, with restoration into a new destination to avoid overwriting the live runtime. Integrity and path/link/reparse-point checks protect boundaries subject to platform capabilities. Runtime databases and credentials remain local user state; the public exchange database contains public profiles, knowledge, and tools only.

### 16. Initial knowledge, research, and evidence boundaries

The ten profiles include method cards, tool guides, and professional tools. Imports remain candidate knowledge with zero confidence and draft tools. The asset catalog is authoritative for current contents: `butian/assets/catalog.json` records sources and `butian/assets/professional_tools.json` records contracts. Real practice creates experience later.

The paper and scripts report mechanisms, costs, and limitations. Deterministic-host controls, mathematical simulations, production-interface replay, nonblind agent pilots, and confirmatory model experiments must remain distinct. Research modules are not a product prerequisite. Module availability, focused test success, and acceptance across all product scenarios are separate claims.

### 17. From one task to reusable experience

The loop begins with a concrete problem, not a saved summary. The host records the goal, environment, constraints, and acceptance conditions, then retrieves potentially applicable methods. Retrieval supplies candidates only: entity, software version, prerequisites, conflicts, and invalidation conditions still require checking. Conflicts require renewed evidence; unavailable sources remain unknown. After real execution, the host preserves the first outcome, tool output, and auditable evidence, then reports success, failure, or unknown without rewriting the first answer after correction.

Production `record_practice_outcome` requires a nonempty independent observation key, and success requires an evidence reference. The service checks ownership and content fingerprints. Keys, references, and evidence digests constrain duplicate counting; renaming a file or observation does not create independent practice. This interface promotes candidate or pending_validation to warm only after at least two deduplicated successes and no failures. Failure of mature experience produces disputed; unknown contributes neither success nor failure. These are program gates, not proof of statistical independence or universal validity, and direct low-level writes do not establish that the gate was followed.

Revisions preserve parents, reasons, environments, sources, and evidence. Old rules cannot establish validation in a new version, and counterexamples cannot be erased to restore confidence. Bounded production-confidence adjustments, weight updates, and the optional Beta module are distinct mechanisms. Warm means that an interface threshold was met, not that a claim was proved. Feedback must actually be submitted by the host; conversation text alone does not tell the service whether an action succeeded.

### 18. Host responsibilities and failure handling

In host-driven operation, the existing AI application supplies the model, session, and tools; Butian supplies identity, task state, candidate context, execution packages, evidence, and feedback interfaces. An execution reads the current task and problem revision, obtains or restores valid execution eligibility, composes context, performs real actions, and submits results for handoff and review. User corrections require resuming against the revised goal. An old execution package, tool validation, or session does not automatically authorize work on the new revision.

Active Provider operation first checks negotiated capabilities and authorization, then starts an independent executor. Consumption and cancellation remain bound to the same task, revision, and session. Successful transport proves delivery, not business acceptance. Timeouts, lost backends, and unconfirmed termination preserve the actual unknown/provider_lost state rather than fabricating cancelled or done. Recovery checks committed records and unresolved actions; side-effecting operations must not be replayed unconditionally.

Single-agent tasks and team subtasks share these evidence boundaries. The lead checks revision, outcome, sources, unfinished work, and next steps before choosing approval, rework, blocking, or user input. Self-reported success is not a final conclusion. Browser interaction, Office layout, and device operation still require corresponding host capabilities; assigning a persona does not create them.

### 19. Connecting sources, hypotheses, and tool admission

External material enters as a located, hashed source snapshot. Extracted claims retain quotations, conditions, limitations, and falsification criteria. Dream consolidation organizes existing material into summaries, abstractions, and hypotheses, with content reasoning supplied by the host. Proposal, scheduling, budgeting, authorization, action start, and observation recording are separate steps; none substitutes for the next. Supported concerns the current verification scope and creates a candidate, while later real practice must still accumulate through the experience interface.

Importing tool source does not authorize execution. Ownership, version, input/output contracts, source hashes, validation eligibility, economic admission, circuit-breaker state, task, budget, and host authorization jointly govern admission. Workflows additionally pin dependency versions and require real combined validation. Packages transfer permitted public assets and contracts, not private memory, runtime databases, credentials, or host authorization. Changed material, expired evidence, revoked access, or invalid dependencies require rechecking; one past pass is not permanent eligibility.

### 20. Deployment, data boundaries, and research conclusions

Initialization creates local runtime state, profiles, and public initial assets. CLI, stdio MCP, and local Web/API expose the same runtime. Use MCP for host calls and configure a Provider only when actively dispatching an independent agent. Runtime databases, original evidence, execution directories, and authorization remain locally managed by the deployer. Restore verified backups into a new destination. This does not itself provide an OS sandbox or complete public-network isolation; actual isolation depends on deployment and backend capabilities.

This README is the self-contained public explanation. Internal requirements history, development acceptance records, and local logs are not public manuals, and documentation allowlists must not be widened merely to repair links. The paper separately reports deterministic rule reuse, interface controls, numerical controls, and dream replay. Fewer source reads do not imply fewer total operations or lower end-to-end latency. Synthetic tasks without model calls do not establish improved model ability, and normal close/reopen does not establish arbitrary power-loss recovery. Runtime experience changes context and method selection, not model parameters automatically.


## Core capabilities

- **Single-agent mode:** work directly with one agent on a focused task.
- **Team mode:** coordinate specialized agents through a lead agent.
- **Configurable agent profiles:** define names, personas, roles, responsibilities, models, and tool permissions.
- **Independent memory:** maintain separate long-term experience and lessons for each agent, with hot, warm, cold, and archived stages.
- **Task lifecycle:** decompose, dispatch, execute, review, revise, block, or cancel work.
- **General provider interface:** integrate agent runtimes, model APIs, local models, MCP tools, or human execution through capability negotiation.
- **Initial team:** initialize ten agents with independent memory, learned method cards, owned tools and configurable roles.

## Single-agent and team workflows

Single-agent mode suits clear, focused tasks. The selected agent works directly with you and uses its own memory.

Team mode suits work that benefits from specialization. Each sub-agent acts under its assigned identity and responsibility. The lead agent gathers reviewable evidence before treating a contribution as a confirmed result.

Choose the workflow to match the task. Persistent tasks and memory help preserve continuity as the work progresses.

The web console's **Submit goal** panel is one form: the **Solo / Team** switch sits at the top and toggles in place, so anything already typed (goal, acceptance criteria) survives the switch. In team mode each member is picked with a checkbox and carries its own subtask field, so work is paired member-to-subtask and the old "subtask line count must equal member count" failure cannot happen. Delivery mode (current AI app / manual, or a configured provider) is shared by both modes: a team goal can be dispatched without any provider bound, and it simply waits for external execution.

## Keep context from disappearing

Butian stores task state, corrections, open questions, evidence, handoffs, and next steps in structured state packages and an append-only event log.

When a conversation approaches its context limit, context is organized into current instructions, active tasks, recent events, relevant memory, and historical indexes. Lower-priority history can be trimmed while preserving current constraints and unfinished work. Snapshots retain parent relationships, event ranges, and integrity information to support recovery after a restart or reconstruction from events.

Agent handoffs carry task versions, status, evidence, environment, remaining work, and next steps. Disproved findings, incompatible environments, and low-confidence hypotheses are marked before they enter working context.

## Experience and lessons that accumulate

Successful procedures and failed attempts become practical assets that can support:

- Better prompts and working methods.
- Selection of suitable models, tools, and sub-agents.
- Improved decomposition, review, and verification strategies.
- Reuse of verified solutions under matching conditions.
- Recognition of known failure conditions.

Experience involving tools or external software is tied to its environment: software and model versions, operating system, dependencies, configuration, reproduction steps, evidence, verification time, scope, and invalidation conditions. Unconfirmed versions are marked `unknown` so behavior from different environments is not silently merged.

Recorded outcomes feed experience and lesson updates. The system retains methods and environments, uses diminishing-return weighting for repeated outcomes, and records user objections as counterevidence for evaluation. Conflicts can lead to disputed status, reduced weight, revision, and renewed verification. Insufficient evidence remains explicitly uncertain.

## Dreaming: turn past work into new leads

Dreaming is a memory-maintenance workflow gated by accumulated material, a minimum interval, new fragments, a lock, and operation and cost budgets.

The host AI uses this material to identify:

- Connections between experiences.
- Unresolved knowledge gaps.
- Contradictory findings.
- Candidate hypotheses worth testing.

Dream outputs begin as hypotheses. They need verification before they can support reliable experience. Butian supplies the workflow and records; the host AI supplies the summaries, abstractions, and proposed hypotheses.

Memory also ages. Less-used experience can move into cold storage and return as a historical reference requiring fresh verification. A method can evolve through successive versions as later practice improves its steps, tools, prerequisites, and checks. The goal is a growing, revisable body of practical experience.

## Lightweight deployment

The core runtime uses **Python 3.11+** and built-in SQLite, with no third-party Python dependencies. It provides a local web workspace, a CLI, and a stdio MCP server.

Connect an MCP-capable host application to use its existing AI. Configure an optional provider when Butian needs to launch independent sub-agents. Unsupported capabilities return `unsupported` or `unknown` explicitly.

## Quick start

### Requirements

- Python 3.11+.
- SQLite, included with Python.

### Run from the repository directory

```bash
# Initialize the runtime (default: ~/.butian; override with BUTIAN_HOME).
python -m butian.cli init

# Add an agent.
python -m butian.cli member add m1 Alice

# Initialization creates ten agents and imports their initial method knowledge and tools.

# Start the local HTTP API and web workspace.
python -m butian.cli serve --host 127.0.0.1 --port 8765
# Open http://127.0.0.1:8765 in your browser.

# Or start the stdio MCP server.
python -m butian.cli mcp
```

### Connect your AI application

For a host that supports stdio MCP, use:

- **Command:** `python` (the interpreter that can import this project).
- **Arguments:** `["-m", "butian.cli", "mcp"]`.
- **Working directory:** the repository directory.
- **Environment:** set `BUTIAN_HOME` to your chosen runtime data directory.

Adapt these values to your host application’s MCP configuration format.

Your host AI handles the user’s goal and generates responses while calling Butian’s task, context, evidence, review, and memory tools. This path needs no `providers.json`, additional model API credentials, or Ollama. An optional provider is needed only when Butian launches independent sub-agents. Applications without MCP, API, or process integration require manual handoff.

### Web workspace

The workspace includes Overview, Tasks, Verification, Memory, Evolution, and Members. Evolution brings together skill stages, memory synthesis and maintenance, version chains, dream workflows, hypothesis queues, and verification batches.

### Useful CLI commands

```bash
python -m butian.cli evolution skills m1
python -m butian.cli evolution dream-run m1 --min-fragments 1
python -m butian.cli evolution history m1
```

### Focused tests

```bash
# Focused run while changing code: usually 1–3 minutes.
BUTIAN_MAINTENANCE_ENABLED=0 BUTIAN_OPERATOR_ACCEPTANCE=1 \
    python -m pytest tests/test_usability_next_steps.py tests/test_team.py -q

# Standard-library test entry points; pytest is not required for these commands.
python tests/test_evolution.py
python tests/test_provider_runtime.py

# Full regression (both variables matter: maintenance worker and real execution backend).
# About 1100 tests, 25–30 minutes locally; most of the time goes to real subprocess
# execution, SQLite transactions and restart/recovery cases.
BUTIAN_MAINTENANCE_ENABLED=0 BUTIAN_OPERATOR_ACCEPTANCE=1 \
    python -m pytest tests --ignore=tests/acceptance -q

# Real-browser acceptance for the web “submit goal” form: solo/team switching,
# one subtask per member, and the duplicate-goal hint. Requires playwright and chromium;
# exit code 0 = passed, 1 = assertion failed, 2 = dependency missing. About 1 minute, 21 checks.
python tests/acceptance/web_ui.py
```

The regression gate is “zero skips”: when the machine cannot create symlinks, the
redirect-point tests fall back to junctions (`tests/fs_redirect.py`) instead of skipping.

The paper research scripts (`docs/论文/prepare_runs.py`, `docs/论文/statistics_report.py`,
`docs/论文/任务包.v1.json`) and the `docs/论文/fixtures/` snapshot they depend on ship with the
repository, so `tests/test_research_preparation.py` and `tests/test_research_statistics.py`
run in a fresh clone without asking anyone for extra material.

Avoid heavy IO such as a repository-wide `grep` during a full run: local `local-command`
Provider tests occasionally time out spawning their subprocess under load and briefly show
`unknown`/`blocked` for one team case; rerunning that case recovers, it is not a state-machine bug.

The research experiment module is separate from the user-facing product workflow.

## Project status

The repository includes the SQLite runtime, HTTP API, MCP tools, CLI, web workspace, memory synthesis, skill evolution, memory aging, version chains, and hypothesis verification.

End-to-end outcomes depend on the host AI and connected tools. Real model understanding, full browser workflows, cross-platform integration, and autonomous improvement require their own execution evidence. A local echo provider verifies transport and state handling. Tests are in `tests/`.

## Repository

https://gitee.com/xw19981010/butian_plan

## The name

“Butian” comes from the Chinese story of Nüwa repairing the sky. The project connects AI tools and preserves practical experience to help address the limitations of an individual model.

## License

Released under the [MIT License](LICENSE).

## Initial knowledge and owned tools

The built-in team covers embedded systems (Xu Wei), instrumentation and DSP (Shen Heng), architecture (Lu Zheng), data analysis (Song Xi), learning and verification (Su Yan), interaction design (He Xi), software and testing (Chen Mo), literature research (Lin Zhi), documents and office work (Wen Lan), and Web and visualization (Ye Qing).

The initial assets contain **32 professional method cards and 19 owned tools**, with recallable usage guides. Methods are synthesized from useful local skills and public TDD and Web testing materials into decision steps, applicability, failure boundaries and verification procedures. `butian/assets/catalog.json` records sources; `professional_tools.json` records ownership, dependencies and input contracts.

Specialized tools cover stack budgets, toolchain checks, sampling sanity, uncertainty, text detectability, CSV structure and identifier preservation, bibliography normalization and DOI deduplication, PDF inspection, and static HTML checks. Each agent also owns a foundation helper for statistics, independent uncertainty contributions, checklists and text fingerprints. PDF inspection optionally requires `pdfplumber`; image analysis optionally requires OpenCV. Browser interaction, Office rendering and proprietary online services remain host capabilities.

Initial knowledge is **unpractised**, with candidate status and zero confidence; tools remain draft and require the existing authorization and execution backend. Imported cards do not automatically count as fresh practice material for dreaming. These assets do not represent model parameter training or successful practical validation. Existing identities and user memories are preserved.

`butian/assets/initial_knowledge.sqlite3` is a portable public interchange database containing only profiles, method knowledge and tool source. It contains no user runtime or credentials. Rebuild it, or create a clean runnable runtime, at a new absolute path:

```bash
python -m butian.assets.build /absolute/new/initial.sqlite3
python -m butian.assets.export /absolute/new/runtime
```

Both commands refuse to overwrite existing targets. Normal service startup imports package JSON and tool resources idempotently. Runtime authentication state is generated locally rather than shipped as a shared database.

## v0.1.7 update

- Password-free member selection on loopback consoles; remote authentication remains required.
- Team totals and per-member memory statistics, including knowledge, experience, lessons, pending validation, recall, adoption, feedback, skills, dream runs, distillation, abstraction, maintenance and validation samples.
- Local evolution selection switches the working identity to prevent mismatched-member access errors.
- Error avoidance remains unrecorded without independent evidence; initial candidate knowledge does not count as verified experience.
- Package and MCP client/server versions are synchronized to 0.1.7. Update a source installation with `python -m pip install -e .` and reconnect the AI application to reload MCP code.

[Repository](https://gitee.com/xw19981010/butian_plan) · [v0.1.7 source](https://gitee.com/xw19981010/butian_plan/tree/v0.1.7)

## Local console entry and upgrades

Use **http://127.0.0.1:8765/** with `python -m butian.cli serve --host 127.0.0.1 --port 8765`. Select a member by name and role and enter the workspace without a token. Member selection determines the working identity and data scope.

After tasks finish, stop the old console with `Ctrl+C` in its terminal, update the source, run `python -m pip install -e .`, and restart with the same command. Stopping a task does not stop its HTTP server; updating files or refreshing a browser does not reload Python. If the port is occupied, close the original service rather than switching ports.

Responses include `X-Butian-Version` and `Cache-Control: no-store`. If a token field remains, check the running version and bind address. Non-loopback bindings such as `0.0.0.0` retain authentication. Reconnect MCP clients after updating.

### MCP discovery and recovery

Use `butian_tools_catalog(offset, limit, prefix)` for compact pages (at most 30 entries), then `butian_tool_schema(name)` for one complete schema. Missing fields and top-level type errors return `invalid_tool_arguments` with field-only MCP tool-error `details.issues`, without echoing credentials or argument values. Business errors retain their original codes. Task state, revision and recovery hints are disclosed only after authentication and task-visibility checks.

Plain task creation records `todo`, not execution. Manual delivery follows ready transition, claim, running transition with refreshed revision, actual execution, evidence registration, handoff and authorized review. Suggested request fragments still require authentication and idempotency keys. For standard host execution, use `butian_goal_submit` with acceptance criteria and then `butian_execution_export`; eligible host tasks can advance automatically. `external_execution_required` is an execution-mode mismatch, not merely a missing running transition. Never silently recreate an existing task to evade this condition.

Experiment tools remain unavailable to member identities through the current MCP endpoint. Schema discovery explicitly reports this restriction; this update does not introduce an administrator grant or identity bypass. Evidence registration and SHA256 validation establish file integrity, not scientific validity or review approval. Reference registered evidence IDs, not file paths.

### Model usage skill and dream accounting

See `skills/butian-workflow/SKILL.md` for schema discovery, host/manual execution, evidence, delivery, permissions and recovery. Install through the host skill mechanism; no member credentials or fixed connector IDs are included. Dream completion counts include only per-member `completed` rows; scheduled checks recorded as `skipped` are separate. Automatic sweeps that find nothing to do (`insufficient_fragments`, `interval_not_elapsed`) are not written to the database: one row per member per interval would push real `completed` runs out of the dashboard and the recent-20 list, and would inflate `dream_skipped` for no work done. Manually triggered skips are still recorded, and each result carries a `recorded` flag saying whether it was persisted. Recent evolution history is not a lifetime total.

Dreams belong to the member who triggered them: one member running a dream only changes that member's counters and records. The scheduled maintenance loop (`BUTIAN_MAINTENANCE_ENABLED`, on by default) performs memory maintenance, queue dispatch and due checks, and by default does **not** run dreams for any member, so one member's dream no longer raises everyone else's count. Set `BUTIAN_AUTO_DREAM=1` to restore automatic dreaming; such runs carry `source="auto"`, manual runs carry `source="manual"`, and legacy rows show an unrecorded source. Dashboards report `dream_auto` separately.

## v0.1.8 update

- Dream counts now include only `completed` runs. Gated `skipped` checks are reported separately in both member and team totals; recent history is explicitly not a lifetime total.
- Added `butian_tool_schema` for one tool's parameters and `butian_tools_catalog` for a compact paginated catalog. Tool errors provide visible task state, missing conditions and follow-up actions without relaxing identity or authorization checks.
- Included the [butian-workflow skill](skills/butian-workflow/SKILL.md) for task execution, evidence registration, delivery review, learning feedback and member-specific dream boundaries.
- The default dream gate requires at least three eligible new fragments. Imported initial knowledge remains recallable but is excluded from fresh practice material. `insufficient_fragments` means the gate lacks material; a skipped check is not a completed dream.
- Package and shared MCP version: **0.1.8**. Restart the HTTP backend and reconnect MCP after updating a source installation; these are separate processes.

[v0.1.8 source](https://gitee.com/xw19981010/butian_plan/tree/v0.1.8)

### Collaboration preflight (development)

Call `butian_collaboration_preflight` with an authenticated `actor_member_id`, optionally a target `member_id` and visible `task_id`. Direct MCP requires that actor's token; a trusted local bridge may inject it. This read-only check reports member boundaries, memory readability, Provider configuration and existing task/reviewer eligibility. Unauthorized cross-member requests are rejected before inspecting target bindings or memory.

Configuration availability is not execution success: `execution_verified=false` stays explicit. Evidence registration/hash verification and independent review remain unverified until actually performed. `butian_status` now distinguishes liveness from collaboration readiness. Standard MCP tool discovery returns at most eight complete schemas per page, with common discovery/goal tools first. Follow `nextCursor`; hosts that aggregate all pages may still display every tool. Use `butian_tools_catalog` for compact discovery. The built-in MCP Provider consumes pagination.

The current authorization model has no cross-member delegation grant. Real member execution requires its authenticated connection and an available Provider. Task review is restricted to its session dispatcher or assignee; an arbitrary third reviewer is not automatically authorized. Preflight does not grant access, switch identities or certify independent execution.

`butian_collaboration_start` is the minimal high-level execution entry: it runs read-only preflight first, creates no task while clarification, Provider, or authorization conditions are missing, and only then creates and starts the authenticated member's Provider task. It never fabricates analysis output or treats startup as independent review. In the current isolated acceptance store, unbound members explicitly return `provider_unavailable`.

## AI entry points (from 0.1.8)

Three calls replace re-sending credentials, session ids and subtasks on every request:

1. `butian_connect` binds the connection to one member. Local trust needs no token; with several members the server requires an explicit choice and never guesses. Later member-scoped calls inherit the identity.
2. `butian_mode` reads or switches solo/team **without a session_id**. The change applies to upcoming goals; running tasks are preserved.
3. `butian_context` returns the current member, mode, visible tasks with revisions, and the next legal action.

Submitting the same goal text again for the same operator in the same mode does not fail and does not create a second task: the response carries `created: false`, `reused_existing: true` and `duplicate_of` (the existing `task_id` and status), plus `next_actions` to keep going. Two parallel tasks that cannot see each other is the real hazard; change one word of the goal when you genuinely want a parallel run. Deduplication is per mode, so a solo submission with the same wording is never absorbed into a team root task.

When the executor never confirmed the outcome (`status` is `unknown`, `provider_lost`, or `failed`), the response carries `trustworthy: false`, a `diagnosis` (`reason` plus why the result cannot count as done), and `next_actions` (call `butian_context` first for the legal next step, then `butian_provider_start` with a fresh session). Such a result must not be written into review, and the task status must not be hand-edited to bypass confirmation.

The loopback console also accepts `/api/goals/host-submit` and the other tool-backed endpoints without a token (the server binds the local operator for that request), **but an explicitly supplied token is still verified**: a wrong token fails, because "no token locally" is not the same as "any token is fine".

`butian_goal_submit` may omit `mode` (uses the current one). In team mode, omitting `subtasks` produces a deterministic role-based split (`plan_source=deterministic_role_split`, explicitly not model-generated); omitting `member_ids` drafts every other member and reports `worker_selection=all_other_members`. **Passing `member_ids` in solo mode now fails loudly with `member_ids_require_team_mode` and a switch-mode action instead of silently dropping them.**

`butian_context` returns `next_actions` keyed by task status instead of always selling a new goal: `todo->ready`, `ready->claim`, `claimed->running`, `running->review` each come with a ready-to-copy `task_id`, `expected_revision`, `target` and `operation_key`. When an actionable task exists no new-goal hint is offered. A dispatcher is never told to do another member's work (it gets `butian_task_tree` for progress only), and review suggestions only cover tasks not owned by the caller.

The first tool page is fixed to status / connect / context / mode / goal_submit / task_list / catalog / schema; further tools follow `nextCursor`. `butian_tools_catalog` accepts `query` (case-insensitive substring over name and description) and returns `catalog_query_no_match` with usable entry points when nothing matches. `butian_task_claim` defaults `member_id` to the connected member and `butian_task_transition` derives `operation_key` from `task:revision:target` when omitted; `member_id`, `actor_member_id` and `token` stay required in the schemas, but `butian_connect` makes them injectable so a caller states its identity once.

Identity is stated once: `butian_connect` injects `member_id`/`actor_member_id`/`token` into every later call (they stay required in the schemas because they are genuinely required without a connection, and calling before connecting returns a ready-to-copy `butian_connect` next action). Only `butian_hypothesis_action_grant`, `butian_tool_security_review`, `butian_tool_repair_confirm`, `butian_dispatcher_set` and `butian_host_authorization_*` are never injected, because there `member_id` names someone else or an independent role — defaulting it to yourself would turn authorization and independent review into self-approval. Optional arguments sent as `null` count as absent instead of failing type validation.

`butian_goal_submit` accepts problem-model fields at the top level (for example `acceptance_criteria`) as an equivalent of nesting them under `problem`; conflicting values across the two places return `problem_field_conflict`, and **misspelled or unknown arguments return `unknown_argument` with the accepted names** instead of being silently ignored (previously a top-level `acceptance_criteria` was dropped and the goal stalled in `needs_clarification`).

Errors always say what is missing and which call comes next: missing objects (`knowledge_source_not_found`, `source_snapshot_not_found`, `memory_not_found`, `tool_version_not_found`, ...) point at a tool that lists or creates them; `butian_knowledge_source_add` registers only a hash, so reading its content requires `butian_knowledge_source_verify` first (a hash match also stores the readable snapshot) or `butian_knowledge_source_capture`; `butian_task_tree` returns `payload` already parsed instead of a double-encoded string.

Convenience does not relax boundaries: cross-member access still returns `member_memory_access_denied`, shared deployments can restore per-call tokens with `BUTIAN_MCP_TOKEN_REQUIRED=1`, and a started Provider is neither a completed execution nor an independent review.

```bash
butian mcp                               # local stdio, no token by default
BUTIAN_MCP_TOKEN_REQUIRED=1 butian mcp   # shared deployment, token enforced
```
