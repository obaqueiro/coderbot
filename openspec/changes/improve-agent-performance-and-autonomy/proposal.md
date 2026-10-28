## Why

Una ejecución prolongada acumuló más de 28 horas con bloqueos de subagentes esperando permisos externos, reintentos de fases y verificación repetida; los checks terminados ocuparon aproximadamente 50 minutos de tiempo calendario. Coderbot necesita autonomía operativa completa y una ejecución medible, recuperable y proporcional al cambio, manteniendo cobertura y revisión independientes.

## What Changes

- Configurar el entorno de confianza total: permitir todas las operaciones del agente principal y subagentes, eliminar restricciones heredadas y resolver automáticamente cualquier permiso residual en toda la familia de sesiones. Nunca depender de intervención por SSH o edición de configuración del usuario del bot.
- Introducir supervisión de operaciones, deadlines agregados y cancelación/limpieza común para agente, subagentes, tests, grabación y conversión; distinguir permisos, proveedor, herramientas, procesos y ausencia de progreso.
- Ejecutar checks focalizados durante implementación y correcciones, revisión interna independiente y una suite completa final mediante un runner determinista. Comparar automáticamente fallos con el baseline fijado al SHA base y aplicar la misma política a VERIFY y E2E.
- Persistir resultados vinculados al contenido relevante, comando y entorno; deduplicar checks válidos e invalidarlos cuando cambien sus entradas. Conservar resultados y siguientes acciones antes de efectos externos para recuperarse sin repetir trabajo completado.
- Acotar contexto con sesiones por fase, handoffs estructurados, compactación y reportes externos; limitar fan-out y aislar recursos compartidos. Ajustar skills para preservar trabajo válido y evidencia RED/GREEN en recuperaciones.
- Preparar dependencias y herramientas gestionadas por Coderbot por fingerprint y evitar instalaciones/builds repetidos; utilizar las capacidades existentes de los harnesses mediante adaptadores del controlador, sin modificar la aplicación destino.
- Recoger evidencia mediante manifests de ejecución, reutilizar artifacts frescos y separar grabación, conversión, subida y notificación con checkpoints idempotentes.
- Sustituir mensajes administrativos estáticos y comandos explícitos por rutas deterministas; usar llamadas ligeras, sin herramientas y con presupuesto corto únicamente cuando sea necesario.
- Corregir esperas de review, continuaciones de threads, snapshots HOLD/CONTINUE y trazabilidad del SHA final; instrumentar duración, reintentos, contexto, checks y entrega con métricas de rendimiento.

## Capabilities

### New Capabilities

- `autonomous-agent-runtime`: permisos completos, supervisión, presupuestos y cancelación para ejecuciones headless de confianza total.
- `execution-checkpoints`: registro durable de checks, snapshots, recuperación, handoffs de contexto y efectos externos idempotentes.
- `performance-observability`: métricas de ejecución y clasificación verificable de actividad, esperas y repeticiones.
- `efficient-agent-workflow`: trabajo focalizado, delegación acotada, preparación reutilizable y utilities ligeras.

### Modified Capabilities

- `quality-gates`: suite final única por snapshot, baseline consistente, revisión independiente y evidencia fresca con procedencia.
- `lifecycle-communication`: STATUS observable, mensajes deterministas y entrega recuperable sin repetir trabajo de programación.

## Impact

- Python: `src/main.py`, runners, `activity.py`, `turn_control.py`, `config.py`, `prompts.py`, `evidence.py` y nuevos módulos de supervisión, checks y checkpoints.
- Integraciones gestionadas: plugin OpenCode, skills, Dockerfile, entrypoint, healthcheck, setup y documentación de configuración/harnesses.
- Estado durable: migraciones aditivas y recuperación de tareas existentes; mantener compatibilidad con Claude/OpenCode, Slack/email y los tres backlogs.
- Tests: integración headless de permisos principal/hijos, fault injection, baseline e invalidación, limpieza, recuperación y artifacts; benchmark de ciclo con latencias controladas.
- Despliegue: implementación incremental local y rollout únicamente de Coderbot después de staging. El código, configuración y harness de cualquier aplicación destino quedan fuera del alcance; se omiten optimizaciones que requieran modificarlos.
