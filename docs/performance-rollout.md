# Rollout de Coderbot

## Alcance

Se despliega exclusivamente Coderbot, con autorización explícita. Se utilizan las
capacidades existentes del destino; sus scripts, configuración, tests y código
quedan fuera del rollout.

## Preparación y verificación

1. Validar la imagen y el código en staging aislado del checkout destino.
2. Registrar versión anterior/nueva y ubicación del backup en metadata privada de
   operación, no en documentación compartida.
3. Conservar datos, mounts resueltos, seeds del agente/npm y socket Docker.
4. Capturar checkpoint de la tarea activa antes de detener el controlador.
5. Verificar salud, continuidad de rama/sesión y reutilización de checks terminados;
   los checks interrumpidos deben seguir identificados como tales.

El checkout original y sus cambios previos se conservan sin sobrescribirlos.

## Recuperación

`scripts/rollout_performance.py` captura metadata privada del contenedor, hace
backup SQLite consistente tras detener el intento y reemplaza solo el mount /app.
La imagen anterior queda etiquetada para rollback. La configuración de rollback
debe conservar los mounts del contenedor inspeccionado; no expandir `~` bajo sudo,
que resolvería los seeds al home de root en vez del usuario del servicio.

No restaurar un backup de estado viejo sobre una tarea que ya avanzó. Primero
detener y limpiar el intento, conservar el cursor/store actual y elegir una imagen
compatible con esos datos aditivos. Trust-total se mantiene durante rollback;
una imagen antigua sin ese comportamiento solo se utiliza con el plugin gestionado
de confianza total correspondiente.

## Verificación de rollback aislado

Probar la imagen anterior en un contenedor aislado con el código/plugin gestionado
compatible. Validar integración headless de confianza total, store, reanudación,
cancelación, artifacts y entrega sin revertir el servicio productivo.

La verificación de mounts comprueba que release cambia solo /app y rollback
restaura las fuentes originales, manteniendo seeds, datos y checkout destino.
Los resultados de checks del destino se reportan por separado del éxito del rollout:
un arranque saludable no implica que la tarea activa esté terminada ni que todos
sus tests aprueben.
