# Conversación paralela

La conversación paralela está siempre activa, sin configuración adicional.
Funciona en Slack y email, dentro
del hilo autorizado de una tarea; no crea tareas nuevas ni atiende hilos ajenos.

## Comportamiento

- En el hilo autorizado de una tarea, `/btw <texto>` fuerza conversación lateral;
  su contenido no aprueba, reintenta ni cancela el flujo.
- Las preguntas explicativas conservan la decisión pendiente.
- Una sesión text-only independiente responde mientras continúa el agente de trabajo.
- Contexto y solicitudes de cambio se registran en SQLite. Recibido no significa
  aprobado, incorporado ni aplicado.
- El contexto se entrega al siguiente turno; alcance nuevo requiere revisión y
  aprobación explícita. Una sustitución conserva el historial de la solicitud anterior.

Sin `/btw`, el bot clasifica el mensaje completo antes de actuar. Comandos, números
de opción y palabras clave (`retry`, `abort`, `complete`, `merge`) se resuelven sin
modelo; el resto cuesta una llamada de utilidad, y la respuesta a una espera pasa por
el clasificador único de `src/replies.py`. Las selecciones
numéricas requieren opciones válidas y detalles completos. Mensajes ambiguos o
condicionados (“antes de reintentar agrega X”) no ejecutan la acción por sí solos.
Las respuestas se revalidan contra la decisión vigente, no contra una opción antigua.

Las consultas utilizan estado confirmado y el paquete congelado de propuesta,
no suponen que archivos parcialmente editados o pruebas en curso estén completos.
El worker no bloquea STATUS/KICK ni reemplaza la sesión de trabajo. KICK reinicia
trabajo, no la conversación lateral.

## Estados de solicitudes

- `ready`: lista para el siguiente punto seguro.
- `clarifying`: requiere información; responder lateralmente con su ID.
- `processing`: transferida a investigación/corrección/replanificación.
- `waiting_approval`: alcance nuevo que requiere aprobar la propuesta revisada.
- `incorporated`: contexto incorporado o alcance aprobado; no significa código terminado.
- `applied`: corrección aplicada y encaminada por verificación/entrega.
- `replaced`: reemplazada explícitamente; conserva el historial y no vuelve a aplicarse.

Antes de publicar o fusionar se revisan mensajes sin clasificar y solicitudes
materiales pendientes. Una explicación ya clasificada no bloquea entrega.
Las solicitudes posteriores a una entrega confirmada no deshacen esa entrega.

## Recuperación y rollback

Un reinicio no borra conversaciones, solicitudes ni recibos. Nunca convertir
automáticamente mensajes `/btw` pendientes en respuestas operativas. Los resultados
generados se guardan antes de su entrega; un fallo de transporte reintenta la entrega,
no la generación. Interrupciones se reintentan con presupuesto acotado y sesión
persistida cuando el harness la expone. Un bloqueo de contenido no reenvía el mismo
texto: informa el fallo y pide un mensaje nuevo/reformulado. Tres fallos de generación
detienen el reintento automático.

La sesión lateral no tiene herramientas: los adaptadores deshabilitan herramientas,
plugins de trabajo y MCP; OpenCode usa permisos deny y un directorio separado.
La restricción no depende únicamente del prompt de “no editar”.
