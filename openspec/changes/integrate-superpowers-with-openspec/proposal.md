## Why

Coderbot currently uses OpenSpec to define and track changes, but its coding
agents are not consistently required to use test-first development, systematic
debugging, fresh verification, or an internal review before opening a pull
request. Integrating a pinned Superpowers runtime gives both supported agents
the same engineering discipline while preserving OpenSpec as the source of
truth and coderbot as the lifecycle controller.

## What Changes

- Pin compatible OpenSpec and Superpowers versions in the coderbot image and
  load Superpowers deterministically for both Claude Code and OpenCode.
- Add a shared coderbot/OpenSpec bridge skill that defines framework ownership
  and adapts Superpowers to coderbot's headless, email-driven lifecycle.
- Use Superpowers brainstorming during exploration while continuing to create
  proposal, design, specification, and task artifacts exclusively in OpenSpec.
- Require OpenSpec apply tasks to follow Superpowers test-driven development
  and systematic debugging practices.
- Add persisted verification and internal-review phases before the existing E2E
  and pull-request gates.
- Return fixes made after a failed E2E run through verification and internal
  review so they cannot bypass quality gates.
- Sync and archive the OpenSpec change on the feature branch after all quality
  gates pass, allowing the archive and implementation to land atomically in the
  same pull request.
- Fail closed when the managed Superpowers runtime or required quality evidence
  is unavailable instead of silently falling back to the old workflow.

## Capabilities

### New Capabilities
- `agent-orchestration`: Defines deterministic Superpowers provisioning, the
  OpenSpec/Superpowers/coderbot ownership boundaries, and phase-specific agent
  discipline for both supported coding agents.

### Modified Capabilities
- `quality-gates`: Adds mandatory pre-PR verification and internal review,
  requires E2E repairs to repeat those gates, and requires validated OpenSpec
  archival before pull-request creation.

## Impact

- `Dockerfile` pins OpenSpec and installs a pinned Superpowers package.
- A new source-controlled agent plugin exposes the bridge skill to both
  supported coding agents.
- `agent_runner.py`, `claude_runner.py`, and `entrypoint.sh` change how agent
  runtime configuration is supplied and validated.
- `prompts.py`, `main.py`, and `config.py` gain phase contracts, persisted
  quality states, retries, and archive recovery behavior.
- Tests expand to cover runner configuration, prompt contracts, state-machine
  transitions, recovery, and bridge-skill behavior.
- `README.md` and `.env.example` document the combined workflow and bounded
  retry configuration.
