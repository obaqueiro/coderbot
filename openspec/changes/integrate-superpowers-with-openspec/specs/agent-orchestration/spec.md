## Purpose

Defines how coderbot combines OpenSpec planning authority with a managed
Superpowers engineering workflow across both supported headless coding agents.

## ADDED Requirements

### Requirement: Managed Superpowers runtime
Coderbot SHALL provide a pinned Superpowers runtime to both Claude Code and
OpenCode without requiring configuration in the target repository or a
user-level plugin installation. The same bridge skill and Superpowers version
SHALL be used for new sessions and resumed sessions.

#### Scenario: Claude Code session starts or resumes
- **WHEN** coderbot invokes or resumes a Claude Code session
- **THEN** the invocation loads the coderbot bridge and the pinned Superpowers plugin from coderbot-managed paths

#### Scenario: OpenCode session starts or resumes
- **WHEN** coderbot invokes or resumes an OpenCode session
- **THEN** its runtime configuration loads the coderbot bridge and the same pinned Superpowers plugin

### Requirement: Fail-closed runtime validation
Coderbot SHALL verify that the selected agent can access the managed bridge and
required Superpowers installation before beginning autonomous task work. It
SHALL report a configuration error rather than silently running an OpenSpec-only
workflow when either component is unavailable.

#### Scenario: Managed plugin is unavailable
- **WHEN** coderbot starts with a selected agent and a required managed plugin path or manifest is unavailable
- **THEN** coderbot stops before selecting a backlog item and reports the missing runtime component

### Requirement: Framework authority boundaries
The bridge SHALL make OpenSpec change artifacts and CLI status authoritative for
requirements, design, and task completion. Coderbot SHALL remain authoritative
for the existing feature branch, persisted lifecycle state, email approvals,
pull-request creation, and merging. Superpowers SHALL provide engineering
discipline within those boundaries.

#### Scenario: Agent plans a change
- **WHEN** an agent explores or proposes a coderbot task
- **THEN** it records the resulting proposal, design, specifications, and tasks only in the OpenSpec change and does not create a parallel Superpowers plan or design document

#### Scenario: Agent begins implementation
- **WHEN** coderbot resumes an approved task on its existing feature branch
- **THEN** the agent works in that checkout without creating another worktree or independently choosing how to merge or finish the branch

#### Scenario: Pull request is ready to open
- **WHEN** every pre-PR gate and OpenSpec archival has completed
- **THEN** coderbot itself pushes the feature branch and creates the pull request without delegating those side effects to the coding agent

### Requirement: Phase-specific engineering discipline
Coderbot SHALL explicitly direct the agent to use the bridge in each planning
and implementation phase. Exploration SHALL use brainstorming discipline,
implementation SHALL use test-driven development, and technical failures SHALL
use systematic debugging before fixes are attempted.

#### Scenario: Exploration needs a material decision
- **WHEN** brainstorming identifies an ambiguity that changes scope, observable behavior, compatibility, or acceptance criteria
- **THEN** the agent requests that decision through coderbot's user-input sentinel instead of guessing

#### Scenario: Implementing a behavior change
- **WHEN** an approved OpenSpec task adds or changes behavior
- **THEN** the agent writes and observes an appropriate failing test before writing the minimal implementation and records task completion only after its verification passes

#### Scenario: A test or runtime check fails
- **WHEN** implementation or verification encounters a technical failure
- **THEN** the agent investigates and identifies the root cause before applying a fix
