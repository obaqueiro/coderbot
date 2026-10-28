## 1. Autonomía total y regresión del incidente

- [x] 1.1 Crear fixtures sanitizadas de permiso hijo pendiente, E2E terminado y reanudación del incidente; documentar baseline reproducible sin usar datos de credenciales.
- [x] 1.2 Validar schema y APIs de OpenCode fijado y definir contrato del puente de eventos; agregar pruebas de compatibilidad del runtime gestionado.
- [x] 1.3 Aplicar permiso global allow y normalizar overrides operativos de todos los perfiles y configuraciones gestionadas; probar restricciones heredadas ask/deny.
- [x] 1.4 Implementar política efectiva para sesiones principales, descendientes y reanudadas y resolución automática de permisos residuales de toda la familia.
- [x] 1.5 Verificar Claude sin permisos operativos y mantener aprobaciones funcionales vía FSM; probar que no se solicita intervención SSH/config al usuario del bot.
- [x] 1.6 Ejecutar integración headless acotada en scratch para read/write/bash externos con principal, general, explore, reanudación y reinicio.

## 2. Registro durable de ejecución

- [x] 2.1 Implementar execution_store SQLite WAL con migraciones aditivas y entidades de tarea, intento, operación, check, hallazgo, checkpoint, artifact y entrega.
- [x] 2.2 Implementar identidad estable de tarea/intento y reconciliación con state.json; probar recuperación de estado anterior sin datos nuevos.
- [x] 2.3 Implementar snapshots de contenido tracked/untracked relevante y exclusiones explícitas; probar ediciones con git status idéntico y cambios documentales.
- [x] 2.4 Implementar fingerprints de argv/cwd, entorno, lockfiles y herramientas con manejo de secretos sin exportar valores.
- [x] 2.5 Persistir resultado de turno y próxima acción antes de notificaciones; probar crash entre store, state.json y efecto externo.
- [x] 2.6 Persistir checks y evidencia RED/GREEN con procedencia; invalidar running/interrupted y recuperar solo resultados completos.

## 3. Puente de runtime y supervisión

- [x] 3.1 Integrar servidor OpenCode local por intento y puente Node fijado que emita tool start/end, session status, permisos y errores principales/hijos.
- [x] 3.2 Implementar operations con deadlines monotónicos por clase y presupuesto agregado compartido por retry/fallback/backoff.
- [x] 3.3 Clasificar espera de proveedor, herramienta, subagente y proceso muerto; detectar lectura bloqueada sin matar tests silenciosos dentro de presupuesto.
- [x] 3.4 Implementar cancelación TERM/gracia/KILL/reap y callbacks de cleanup sobre procesos y recursos propios; probar nietos y puertos residuales.
- [x] 3.5 Integrar runners Claude/OpenCode con supervisor y recuperación acotada desde checkpoint, conservando compatibilidad de resultados.
- [x] 3.6 Extender KICK a checks, E2E, grabación y conversión ejecutados por Python, conservando tarea, rama y aprobaciones.
- [x] 3.7 Unificar shutdown y timeout de todas las operaciones y evitar reintentos sobre cleanup incompleto o locks ocupados.
- [x] 3.8 Actualizar config/setup de presupuestos y healthcheck basado en supervisor; probar bloqueo, progreso legítimo y reinicio sin esperas acumuladas excesivas.

## 4. Runner determinista de checks

- [x] 4.1 Definir contrato versionado de check plan con argv, cwd, inputs, env, deadline, resources, reporter y preparación; agregar validación sin permisos humanos.
- [x] 4.2 Implementar detección/adaptadores para unittest, node test, lint/build/types y harness E2E legacy, con salida desconocida explícita.
- [x] 4.3 Ejecutar checks bajo supervisor con logs/artifacts por run y resultados estructurados; añadir parsers JUnit/JSON y fallback de texto testeado.
- [x] 4.4 Implementar reutilización por identidad y causa de invalidación; probar cambio de código, lockfile, runtime, entorno y check interrumpido.
- [x] 4.5 Implementar scheduler con recursos exclusivos y límites de concurrencia; probar conflictos de puertos, checkout, preparación e imágenes.
- [x] 4.6 Registrar checks focalizados solicitados por agente y ejecutados por controller sin convertir contratos textuales en evidencia ficticia.

## 5. Baseline y suite completa final

- [x] 5.1 Crear worktree baseline sobre base_sha inmutable y entorno comparable con recursos aislados y cleanup supervisado.
- [x] 5.2 Comparar fallos por identidad de test y firma de error conservadora; probar errores semánticamente distintos, infraestructura y baseline indeterminado.
- [x] 5.3 Persistir/reutilizar baseline por SHA, comando y entorno; ejecutar comparaciones necesarias sin rerun completo injustificado.
- [x] 5.4 Cambiar VERIFY a checks focalizados/OpenSpec y la fase final a suites completas después de revisión, incluyendo tareas sin harness.
- [x] 5.5 Unificar política VERIFY/E2E para preexisting/regression/infrastructure/unknown; probar que 18 fallos preexistentes no disparan reparación indiscriminada.
- [x] 5.6 Implementar reparación focalizada y re-review de regresiones finales, invalidando solo checks con entradas afectadas.
- [x] 5.7 Integrar gates de snapshot final en feedback, threads y conflictos con continuación durable hacia push/entrega.
- [x] 5.8 Asociar reportes de calidad/review y SHA entregado al contenido válido; probar cambios exclusivamente de archivo OpenSpec y cambios de aplicación posteriores.

## 6. Handoffs, prompts y modelo de trabajo

- [x] 6.1 Implementar handoff_context acotado con requisitos, decisiones, diff, findings, checks y acción pendiente para sesiones por fase.
- [x] 6.2 Iniciar revisión con contexto fresco y rotar/compactar implementación al presupuesto configurable; probar reanudación con mapping de sesiones.
- [x] 6.3 Actualizar prompts y bridge skill para checks focalizados, suite final del controller y review/re-review independiente sin duplicación.
- [x] 6.4 Reconciliar TDD y KICK para preservar trabajo con evidencia válida, tratando código desconocido mediante diagnóstico focalizado.
- [x] 6.5 Aplicar fan-out proporcional y ownership de archivos/recursos con límite configurable; probar integración de tareas concurrentes independientes.
- [x] 6.6 Añadir configuración diferenciada de modelo/esfuerzo por rol sin cambiar principal automáticamente; evaluar latencia y calidad con fixtures comparables.

## 7. Preparación reutilizable y contrato del harness

- [x] 7.1 Implementar preparation con fingerprint de dependencias y probes de disponibilidad; probar reutilización y reparación tras instalación parcial.
- [x] 7.2 Preinstalar dependencias fijadas de plugins/puente/browsers gestionados en imagen y evitar auto-installs repetidos del runtime.
- [x] 7.3 Separar registry público y scopes privados en preparación/setup sin exponer o sobrescribir credenciales; probar auth privada requerida y paquetes públicos.
- [x] 7.4 Definir contrato opcional de harness prepare/run/suites/providers/resources/artifacts y adapter legacy compatible.
- [x] 7.5 Validar fixtures genéricas de agrupación por proveedor mediante el contrato declarativo del destino, sin inventarios ni adapters específicos de una aplicación.
- [x] 7.6 Documentar y probar aislamiento de sesiones/puertos para workers concurrentes; mantener ejecución serial cuando no se garantice aislamiento.

## 8. Evidencia y entrega recuperable

- [x] 8.1 Implementar manifest de artifacts con run, snapshot, spec/test, resultado, rutas y hashes para Playwright y Newman.
- [x] 8.2 Reutilizar evidencia válida del gate final y actualizar specs sobre diff final; limitar grabación adicional a un intento focalizado por snapshot/parámetros.
- [x] 8.3 Corregir detección legacy de archivos sobrescritos/nuevos y directorios externos; eliminar fallback a clips viejos o fallidos como evidencia aprobada.
- [x] 8.4 Normalizar bytes/str de TimeoutExpired y hacer ffmpeg timeout best-effort con cleanup; probar fallback a clips válidos o evidencia ausente.
- [x] 8.5 Separar RECORD/CONVERT/UPLOAD/NOTIFY en checkpoints durables e identidades estables; probar retry sin repetir harness o programación.
- [x] 8.6 Reconciliar PR, Drive y entrega de canal tras crash donde las APIs lo permitan; documentar ventanas no reconciliables y evitar duplicados conocidos.
- [x] 8.7 Mantener handoff honesto de evidencia parcial/ausente y checks preexisting/unknown con las aprobaciones pendientes intactas.

## 9. Utilities, comunicación y esperas

- [x] 9.1 Crear plantillas estáticas por idioma para fases, STATUS, check-ins y recuperación, eliminando llamadas LLM administrativas redundantes.
- [x] 9.2 Implementar utility profile sin coding tools/plugins, con timeout/modelo/esfuerzo propios y traducción conjunta reutilizada por conversación/trail.
- [x] 9.3 Resolver comandos explícitos y candidato único determinísticamente; mantener clasificación de ambigüedades y semántica de aprobación.
- [x] 9.4 Aplicar deadline WAIT_REVIEW antes de todas sus salidas y respetar ausencia de workflow tras conflictos.
- [x] 9.5 Conservar output/RESOLVE en continuación de threads después de preguntas y probar push/resolución sin nueva ronda innecesaria.
- [x] 9.6 Completar snapshot HOLD/CONTINUE y persistir restauración antes de retirada; probar capacidades divergentes y crash intermedio.
- [x] 9.7 Evitar procesamiento global de archivo OpenSpec histórico cuando baste el alcance relevante y probar que no se omite validación del cambio activo.

## 10. Observabilidad y aceptación

- [x] 10.1 Instrumentar timeline durable por fase/intento/operación, retry reasons, tokens/contexto, checks, cleanup, espera humana y entrega con datos sanitizados.
- [x] 10.2 Extender STATUS a operaciones del controller y descendientes con duración/deadline/espera observables sin LLM ni mutación del turno.
- [x] 10.3 Implementar exportador de métricas con tiempos exclusivos y solapados, unknown explícito y retención acotada sin perder checkpoints activos.
- [x] 10.4 Crear benchmark de ciclo con fake provider y latencias controladas para permisos hijos, bloqueo, baseline, crash y fallo de entrega.
- [x] 10.5 Comparar baseline/nueva versión y publicar resultados reproducibles: cero esperas de permiso, bloqueo local detectado en menos de un minuto, checks deduplicados y ejecución normal sin KICK; medir objetivo de 80% menos tiempo improductivo sin afirmarlo si no se cumple.
- [x] 10.6 Ejecutar regresiones focalizadas por incremento y suite completa final de Coderbot; validar ambos runners, canales y backlogs con fault injection.

## 11. Documentación y rollout coordinado

- [x] 11.1 Actualizar README, env example, setup y docs de runtime/harness/checks/checkpoints con defaults, migración y diagnósticos observables.
- [x] 11.2 Preparar staging aislado y verificar principal/subagentes/reanudación, suite final y artifacts con presupuesto acotado sin afectar tarea remota activa.
- Tarea 11.3 retirada: no modificar aplicaciones destino ni considerar implementado un cambio al harness externo.
- [x] 11.4 Ejecutar rollout de imagen/config únicamente de Coderbot tras checkpoint de la tarea activa y backup durable; registrar versión y comparación antes/después.
- [x] 11.5 Verificar recuperación/rollback conservando trabajo, trust-total y datos aditivos; documentar resultado y tareas de rollout realmente pendientes.
