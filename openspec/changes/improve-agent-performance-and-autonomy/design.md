## Context

Ver `proposal.md` para motivación y las seis delta specs para contratos. La aplicación Python tiene una FSM en `src/main.py`, runners Claude/OpenCode y persistencia JSON; `activity.py` conserva solo el turno actual y consulta SQLite de OpenCode. El runtime desplegado fija OpenCode 1.18.18, cuyo CLI atiende permisos únicamente del sessionID principal. Los hijos conservan herramientas en running al solicitar acceso externo. El entorno se considera de confianza total por decisión explícita del usuario.

`VERIFYING` acepta fallos reproducidos en main, mientras `do_e2e` exige exit code cero. Prompts y skills favorecen suites y reviews repetidas. `evidence.py` busca rutas fijas y regraba; los harnesses externos pueden repetir preparación, publicar fuera del checkout y compartir recursos. Coderbot es un controlador genérico de repos externos: este cambio mejora su invocación y recogida de resultados, sin modificar aplicaciones destino ni exigirles flags nuevos.

## Goals / Non-Goals

**Goals:**
- Resolver primero bloqueos de permisos y presupuestos, con incrementos verificables.
- Trasladar ejecución/reutilización de checks y efectos externos al controlador, dejando al LLM implementación y juicio de ingeniería.
- Mantener compatibilidad con tareas activas, runners, canales y backlogs existentes.
- Atribuir latencia sin registrar razonamiento privado ni secretos.

**Non-Goals:**
- Cambiar el modelo principal a ciegas, quitar aprobación de propuestas/merge o garantizar una aceleración porcentual sin benchmark comparable.
- Modificar código, configuración o harness de la aplicación destino. No elevar workers sobre fixtures compartidas sin aislamiento existente.
- Garantizar exactly-once en APIs que no permiten reconciliación; se requiere identidad durable y best-effort documentado para esas ventanas.

## Decisions

### 1. Política efectiva de confianza total y puente de eventos

Configurar permiso global allow y normalizar overrides operativos de todos los agentes en el hook de configuración gestionado, incluidos los perfiles de utility (sin herramientas por selección, no por permiso interactivo). Asegurar la política de sesiones reanudadas y descendientes mediante adapter de runtime que responda permisos residuales de la familia completa. Claude conserva su modo sin permisos y ejecución no-root compatible. Las decisiones funcionales usan contratos de la FSM, nunca prompts interactivos de herramientas.

El adapter de OpenCode evolucionará de CLI JSON filtrado a servidor local administrado y SDK/event stream mediante un puente Node acotado. Permite observar tool start/end, permisos, session status y errores hijos; el servidor se inicia por intento inicialmente para simplificar lifecycle. Mantener el backend CLI como compatibilidad solo con la misma política efectiva y prueba de no bloqueo; no anunciar soporte de recuperación de permisos hija con eventos filtrados. Fijar dependencias y validar schema/API de la versión instalada antes de integrar. Una actualización upstream se evalúa mediante fixture de regresión, sin asumir que latest resuelve el defecto.

Alternativas: allowlists dejan más rutas de bloqueo y contradicen confianza total; --auto solo reproduce el defecto; un prompt de "no pidas permisos" no cambia el runtime.

### 2. Operation supervisor común

Introducir `operations.py` para registro de operaciones, proceso/grupo, deadline monotónico, cancelación y heartbeat de progreso. `turn_control.py` se convierte en control de intento, no un único CLI; KICK cancela operación activa y recursos propios, conserva aprobación y reanuda desde checkpoint. Cleanup TERM con gracia, KILL, wait/reap y callbacks de recursos; aislar Compose por run y evitar down global sobre servicios ajenos.

Perfiles iniciales configurables: utility 30 s, herramienta local 45 s, llamada de proveedor 180 s, subagente 600 s, E2E 900 s, turno 1800 s. Los perfiles solo se habilitan tras medición y permiten overrides por comando/repositorio; instalaciones y tests explícitos no heredan el presupuesto de read. Un deadline agregado incluye fallback y backoff. Detectar progreso con eventos de operación y subprocess; silencio de stdout no basta para matar un test vivo dentro del presupuesto. Permiso residual se resuelve inmediatamente; error del puente termina con diagnóstico, nunca espera humana operativa.

Healthcheck mide vida del supervisor y estado de operación; un turno bloqueado no mantiene saludable la tarea indefinidamente. El watchdog recupera antes que el hard restart. Evitar sumar margen de tick y stale hard como una espera de más de cuatro horas.

### 3. Store durable e identidades de contenido

Crear `execution_store.py` con SQLite WAL propio, migraciones versionadas aditivas y registros de task, phase_attempt, operation, check_run, finding, checkpoint, artifact y delivery_step. Retener state.json como cursor compatible de FSM; reconciliar ambos por task/attempt id sin convertir dos escrituras en una transacción ficticia. Registrar resultado completo y cursor durable del store antes de notificar; state.json puede reconstruir su siguiente acción desde ese registro.

Identidad de check: repo/task + snapshot de entradas + argv/cwd normalizado + fingerprint de entorno + versión de contrato. Snapshot hashea tracked y untracked relevantes con exclusiones de outputs, caches y secretos; incluye diff respecto a base y no depende de status. Inputs desconocidos usan snapshot conservador de código/config completa; documentación solo queda excluida cuando no es entrada declarada. Fingerprint registra versiones, lockfiles y configuración relevante; valores sensibles se comparan mediante digest local no exportado. Registrar comandos/redacciones sin prompts o credenciales.

Resultado completo es reusable; interrupted/running no. La recuperación reconcilia archivos reales y resultados antes de continuar. Evidencia RED/GREEN se liga a tarea/check/contenido y viaja en handoffs; skills no borran trabajo válido por interrupción. HOLD conserva capacidades, session ids, checkpoints y presupuesto; persistir restauración antes de eliminar hold.

Alternativa: HEAD+porcelain no detecta cambios de contenido sobre archivos ya modificados y prohíbe reutilización documental válida.

### 4. Check plan declarativo y runner determinista

Crear `checks.py` y `check_plan.py` con contrato de argv, cwd, inputs, env keys, suites, deadline, resources, reporter y preparation. Configuración opcional por target y adaptadores de detección para unittest, node test, lint/build/types y e2e/run.sh. El agente propone checks en JSON estructurado; el controlador valida formato, registra y ejecuta. Confianza total significa que no se añade aprobación de comandos. No inferir que el mismo flag funciona en pytest, unittest, Playwright y Newman.

Secuencia: IMPLEMENTING focalizado → VERIFYING focalizado/OpenSpec → INTERNAL_REVIEW fresca → E2E/final suite canónica → ARCHIVING → OPEN_PR. Mantener nombres FSM como compatibilidad, pero E2E se vuelve final gate de todas las suites completas aplicables; sin harness también se ejecutan checks completos pertinentes antes de archivar. Contratos textuales previos se aceptan durante migración, sin convertir una declaración LLM en resultado de comando comprobado.

Tras review fixes, feedback/threads/conflictos o cambios posteriores, invalidar por inputs y llevar una continuación durable por CHECK_FINAL hacia push/finalización correspondiente. Una regresión final inicia reparación focalizada y re-review del diff de reparación; luego se repiten checks afectados y suites cuyo fingerprint cambió. Documentos de OpenSpec/archivo no fuerzan rerun de aplicación salvo inputs declarados.

Cada check guarda JUnit/JSON nativo cuando esté disponible. Adapter de salida texto versionado como fallback, con unknown explícito; exit no cero sin identidad comparable no se considera preexisting. Baseline usa worktree del `base_sha` inmutable y entorno comparable, almacenado/reutilizado por identidad; solo ejecutar baseline necesario para confirmar fallos no conocidos. Comparar test id + firma de error normalizada de campos volátiles, sin normalizar diferencias semánticas. Guardar baseline separado de feature y recursos; infraestructura o entorno incomparable bloquea sin atribuirlo al código.

Alternativas: suite completa por fase multiplica arranques; solo CI contradice decisión de suite final local; aceptar preexisting desde prosa LLM oculta regresiones.

### 5. Contexto y concurrencia proporcionales

Crear `handoff_context.py` con requisitos aprobados, decisiones, diff summary, findings, checks válidos, checkpoint y acción pendiente. Sesión nueva para review y fases con contexto distinto; dentro de implementación rotar/compactar al presupuesto configurable (objetivo inicial 80k tokens estimados/observados, no depender de estimación exacta). No copiar dumps de logs; referencias a artifacts y resúmenes limitados. Preservar mapping de sesiones por fase/intent para recovery.

Prompts y bridge skill coherentes: TDD útil para comportamiento, focalizados, suite final propiedad de controller, review independiente sin verificación completa duplicada, recuperación de trabajo RED/GREEN válido. Delegar unidades independientes que amortizan overhead; límite inicial 3 workers LLM configurable. Locks por puertos, checkout write ownership, Compose, imágenes y preparación; tests externos concurrentes solo con recursos distintos. No hacer ediciones simultáneas en mismos archivos.

### 6. Preparación y contrato genérico del harness

Crear `preparation.py` y contrato opcional versionado del harness: prepare fingerprint, suites/providers, resources y artifact manifest. Preinstalar plugins/dependencias de puente en Dockerfile; separar registry público y scopes privados configurados para preparación sin auth privada global innecesaria. Verificar instalaciones antes de marcar prepared. No modificar ni imprimir tokens del registry.

Harness legacy conserva ./run.sh y detección; su caché solo se utiliza si preparación separada verificable ya disponible. Los grupos de specs/proveedores, recursos y outputs externos se declaran mediante contratos del destino, nunca mediante inventarios o adapters de una aplicación particular dentro de Coderbot. No cambiar run.sh, fixtures ni configuración del destino. Las instalaciones internas de un harness legacy quedan a cargo de ese harness; solo se optimizan las llamadas que realiza Coderbot. Workers siguen 1 cuando el target comparte recursos. El contrato opcional se consume únicamente si ya existe, sin requerir añadir archivos al destino.

### 7. Artifacts y finalización idempotente

Manifest contiene run id, snapshot, exit/result, test/spec id, kind, path, size/hash y timestamp. Preferir demo capturada durante checks finales; si no existe, una ejecución focalizada de evidencia por snapshot/params. Newman y Playwright usan mismo envelope con kind específico. Compatibilidad legacy compara path+mtime+size/hash, nunca conjuntos de nombres ni fallback a clips viejos. Los artifacts de fallos quedan como diagnóstico identificado, no evidencia aprobada.

`finalize_pr` usa delivery_step para RECORD, CONVERT, UPLOAD y NOTIFY; claves estables y reconciliación por PR/drive metadata/thread cuando posible. ffmpeg timeout degrada a raw clips válidos o ausencia de evidencia; subprocess TimeoutExpired normaliza bytes/str. Ningún error de conversión/subida vuelve a programar o ejecutar suite. Mensaje final informa ausencia de evidencia y conserva gates y decisión humana.

### 8. Utilities y comunicación

Plantillas estáticas por idioma para fases, pings, errores y STATUS; texto del agente ya localizado no se traduce otra vez. Traducción dinámica en una llamada conjunta de asunto/cuerpo cuando necesaria, resultado durable compartido con trail. Comandos explícitos y candidato único deterministas. Utility profile separado sin coding tools/plugins/contexto de programación, presupuesto 30 s y fallback determinista donde no altere semántica; modelo/esfuerzo configurables, sin cambiar principal automáticamente.

### 9. Review loops y observabilidad

Deadline de WAIT_REVIEW se comprueba antes de cualquier return, también threads unknown; conflicts respetan has_code_review. Preservar output/RESOLVE en continuaciones y checkpoints de push. Revisar post-review y HOLD para identidad final consistente.

Store mantiene eventos sanitizados, contadores de contexto/cache/LLM utilities, retry reasons, tool start/end, espera humana y cleanup. Exportador produce spans jerárquicos y tiempos exclusivos/solapados con unknown; STATUS toma snapshot independiente sin llamada LLM ni mutación FSM. Retención configurable con compactación de eventos terminados y conservación de resúmenes/checkpoints activos. Fallo de telemetry no detiene operaciones; fallo de checkpoint requerido sí produce error recuperable.

Benchmark deterministic end-to-end con fake provider y subprocess reales ligeros, fallo de permiso hijo simulado, bloqueo, baseline con fallo, crash y retry de entrega. Comparar misma carga/latencias y registrar eventos; integración live OpenCode en scratch separado sin Jira/Slack y con presupuesto explícito. Objetivo 80% menos tiempo improductivo sobre escenario de incidente reproducible; reportar también p50/p95 y calidad/regresiones antes de cambiar routing de modelos.

## Risks / Trade-offs

- [Merge de permisos por versión OpenCode] → Validar config y sesión principal/hija/reanudada contra versión fijada y observar permisos residuales en puente.
- [Caché produce falso pass] → Fingerprints conservadores, parser unknown bloquea, pruebas de invalidación y baseline incomparable.
- [Presupuestos demasiado cortos] → Perfiles por operación/repo, eventos de duración y overrides explícitos; no inferir bloqueo de silencio de test.
- [Estado JSON y SQLite divergen] → Checkpoint store autoritativo de resultados, ids/cursor reconciliables y fault injection entre escrituras.
- [Efectos externos sin idempotency API] → Reconciliación previa y límites documentados; no prometer exactly-once.
- [Adapter target sin nuevo contrato] → Legacy path conservador, sin modificar el destino ni inventar flags.
- [Paralelismo sobre recursos compartidos] → Scheduler y ownership explícitos antes de habilitar concurrencia.
- [Benchmark synthetic no refleja Azure] → Separar resultados controlados de staging real y no atribuir silencios históricos desconocidos.

## Migration Plan

1. Crear fixtures del incidente y baseline de métricas; verificar runtime y contratos fijados.
2. Entregar política trust-total y puente de permisos/eventos, probar principal/hijos/reanudación en scratch.
3. Agregar store y supervisor con migraciones aditivas; mantener curso de tareas activas y cleanup común.
4. Integrar checks/baseline y final gate detrás de configuración de rollout; activar por defecto tras tests de ambos runners y rutas de feedback.
5. Activar handoffs/contexto, preparación, manifests y finalización durable; actualizar setup/docs/skills coherentes.
6. Ejecutar suites focalizadas por incremento, suite completa final local y benchmark; staging de una tarea con permisos y recursos aislados.
7. Rollout únicamente de Coderbot después de checkpoint de tarea activa. Registrar image/version y resultados antes/después; no modificar aplicaciones destino.

Rollback conserva state.json y backup de store antes de migración; detener intento con cleanup y restaurar imagen/config compatibles con cursor de fase. No borrar trabajo/artifacts ni volver a un runtime con solicitudes operativas pendientes; trust-total permanece aunque se reviertan optimizaciones. Datos aditivos no requieren downgrade destructivo y los checks sin identidad antigua se vuelven a validar conservadoramente.
