# Butian Plan

[简体中文](README.md) | **English**

> **Practice is the sole criterion for testing truth.**

AI can answer questions. Working with it over time asks for more: remembering your requirements, resuming unfinished work, reusing methods that worked, learning from failures, and carrying complex tasks through to a result you can review.

**Butian adds that working layer to the AI application you already use.** Through MCP, your host AI can access tools for tasks, memory, context recovery, evidence, and collaboration. Butian preserves progress and experience between interactions while your familiar AI handles reasoning and communication. No additional Ollama installation or local model is required.

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

Research paper **(currently in Chinese)**: [Computational Modeling of Human Experience Formation through Practice](docs/论文/补天多轮经验机制-IEEE.md) · [Word document](docs/论文/补天多轮经验机制-IEEE.docx). The paper describes the formation, revision, verification, and reuse of experience, together with the corresponding experiments.

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
# Standard-library test entry points; pytest is not required for these commands.
python tests/test_evolution.py
python tests/test_provider_runtime.py
```

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
