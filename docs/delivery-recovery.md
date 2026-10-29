# Recuperación de entrega

RECORD, CONVERT, UPLOAD, ACCESS, NOTIFY y NOTIFY_FILES (`src/evidence_delivery.py`)
tienen claves durables por contenido. Los pasos
completados se reutilizan después de restart. Drive reconcilia uploads por hash
en appProperties antes de crear un archivo; PR creation ya consulta el PR abierto
de la rama antes de crear otro.

Para un efecto con confirmación local perdida, delivery_checkpoint acepta un
reconciliador de canal. Un canal que no proporciona búsqueda/idempotency fiable
conserva una ventana de duplicado entre envío y checkpoint: no se garantiza
exactly-once. En particular, Gmail y envío de adjuntos Slack necesitan reconciliar
identidad de mensaje con permisos de historial; un fallo de entrega nunca vuelve
a ejecutar programación ni tests. Las referencias a artifacts permanecen locales
hasta completar su entrega y no deben purgarse mientras la tarea esté activa.
