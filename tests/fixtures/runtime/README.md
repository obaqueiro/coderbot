# Regresión de espera de permisos

`permission-stall.json` reconstruye una secuencia mínima de espera de permisos:
E2E terminado, permiso externo de un hijo ignorado por el CLI y 6590 segundos
de espera antes del timeout. Identificadores y rutas son artificiales; no contiene
prompts, secretos ni razonamiento. Los tiempos son relativos y no requieren esperar.

Baseline reproducible: reproducir los eventos con el filtro del CLI 1.18.18
`permission.sessionID === root_session`. La solicitud hija queda sin respuesta.
El bridge corregido debe responder antes del siguiente evento, también al reanudar.
El benchmark de aceptación reutiliza esta secuencia con reloj controlado.
