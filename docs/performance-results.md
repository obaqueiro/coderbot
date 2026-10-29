# Resultados de aceptación inicial

Comandos reproducibles:

```sh
venv/bin/python scripts/benchmark_runtime.py
python3 scripts/test_trusted_runtime.py
venv/bin/python -m unittest discover -s tests -t .
```

- Replay sintético del incidente: 6590 s de espera antigua; política residual
  de permiso con presupuesto de 10 s. Reducción modelada: 99,85% de esa espera.
- Runner real: una ejecución completa y reutilización al recuperar el mismo
  contenido/entorno; una grabación aunque la entrega se reintente.
- Fallo idéntico en baseline: gate pass con preexisting, sin reparar código viejo.
- Integración headless con proveedor HTTP local: read/write/bash externos para
  build/general/explore con restricciones heredadas deny, reanudación y dos
  arranques. Aprobada en OpenCode 1.18.18 Linux y 1.18.30 macOS.
- Suite completa inicial: 655 tests en 28,28 s; aprobada con un skip GNU stat
  específico de macOS. Las pruebas de cancelación verifican reutilización de
  un puerto abierto por un proceso nieto y cleanup ante timeout/shutdown.

El porcentaje modelado no mide throughput real de Azure ni demuestra 80% de
mejora del ciclo productivo completo. Esa medición requiere tareas comparables
en staging/producción separando esperas humanas, outages y calidad del resultado.

## Staging Linux

Imagen `codebot-performance-staging:dfb1375` construida en Azure sin reemplazar
el servicio activo. OpenCode 1.18.18 pasó integración real principal/general/explore,
read/write/bash externos, reanudación, dos arranques y puente HTTP. Diecinueve
pruebas de checks/baseline/artifacts/entrega/cancelación/métricas pasaron en Python
3.12.13, incluyendo cleanup de nietos y recursos. No se usaron credenciales de
Jira/Slack/Azure en esas pruebas.

## Controlled baseline: fake-agent walk (2026-10-28, before the streamlining stages)

`tests/test_fsm_walk.py` drives the real tick loop for one trivial task (add a pure
function and its unit test) with an agent that never errs, real git, OpenSpec 1.9.0 and
controller checks, on the image's Python 3.12. It isolates coderbot's own overhead from
model behaviour:

| Measure | Value |
|---|---|
| Model calls | 8 (5 working turns: explore, propose, implement, verify, internal review; 3 utility: language, reply routing, architecture report) |
| Prompt characters sent to working turns | ~71.5k |
| Prompt characters sent to utility calls | ~11.2k |
| Messages sent to the human | 13 |

Each later stage re-runs the same walk; the targets are fewer working turns (no agent
verify turn), far fewer prompt characters, and no utility calls for language or routing
of keyword replies.

## After the streamlining stages: same walk (2026-10-29, branch `streamline-stage5`)

Same test, same fixture, same image (`coderbot-test`, Python 3.12), run with
`CODERBOT_TEST_IMAGE=coderbot-test scripts/test.sh tests.test_fsm_walk`. The numbers come
from the fake `claude`'s call log (`$WALK_DIR/claude.log`, one row per call with the
prompt length and the `--append-system-prompt` length).

| Measure | Baseline (28 Oct) | Now (29 Oct) |
|---|---|---|
| Working turns | 5 (explore, propose, implement, verify, internal review) | 4 (explore, propose, implement, internal review); VERIFYING ran in the controller |
| Prompt characters, working turns | ~71.5k (everything was in the user prompt) | 15.6k user prompt + 9.7k system prompt (2,416 per turn) = ~25.2k |
| Utility calls | 3 (language, reply routing, architecture report) | 2 (language 0.9k, reply routing 2.8k) = ~3.7k chars |
| Messages sent to the human | 13 | 15 |
| Sessions | one `--resume` chain | planning (explore, propose resumes it), implementer (fresh at approval), reviewer (fresh) |
| States visited | — | IDLE, EXPLORING, PROPOSING, WAIT_APPROVAL, IMPLEMENTING, VERIFYING, INTERNAL_REVIEW, E2E, ARCHIVING, OPEN_PR, WAIT_MERGE, IDLE |

Notes on the numbers:

- The baseline predates the system prompt, so compare the totals: ~71.5k → ~25.2k
  (−65%). The review turn alone is 8.6k of the 15.6k, because it carries Superpowers'
  `code-reviewer.md` template.
- The language call still runs because the walk has one backlog item, so the agent
  PICK call (which now returns the language) never happens. With a real pick it is
  skipped.
- The routing call is the walk's `approve` reply. That word is not in the
  deterministic reply list, so it costs one classifier call.
- Messages went up by two. They are all distinct; none is a re-send. They include the
  new draft-PR link and a "Classifying your message" receipt for the `approve` reply.
  Subjects repeat because each lifecycle step sends a milestone ("I'm running checks
  and review") and then a short banner per state it enters ("Verification ·
  INTERNAL_REVIEW", "Verification · E2E", "Archiving · OPEN_PR"). Those state banners
  are the cheapest place to cut messages further.
- The system prompt is 2,416 characters; the plan's target was ≤2,000.

Not measured here: wall time per phase, model cost and review findings. They depend on
the model, so they need a live canary run (the plan's Haiku, Sonnet and OpenCode
canaries).
