"""Coderbot's own fixed message text in English and Spanish, so boilerplate never costs a model call."""
# Coderbot's own fixed text, English source fragment -> Spanish. Fragments are the literal
# parts of the messages in main.py (f-string pieces included), so values in between
# (URLs, counts, agent output already in the task language) pass through untouched.
PHRASES = {
    # subjects
    "question during ": "pregunta durante ",
    "revised proposal": "propuesta revisada",
    "proposal for review": "propuesta para revisión",
    "draft PR": "PR en borrador",
    " stuck - needs your help": " bloqueado: necesito tu ayuda",
    "Verification blocked": "Verificación bloqueada",
    "repairing final checks": "reparando los checks finales",
    "final check repair findings": "hallazgos de la reparación de checks finales",
    "final check recovery decision": "decisión sobre la recuperación de checks finales",
    "OpenSpec archival stuck - needs your help": "Archivado de OpenSpec bloqueado: necesito tu ayuda",
    "merge blocked — the PR changed since review": "merge bloqueado: el PR cambió desde la revisión",
    "merge blocked — could not check the PR": "merge bloqueado: no pude consultar el PR",
    "merge blocked — could not verify review comments": "merge bloqueado: no pude verificar los comentarios de revisión",
    "merge blocked — unresolved review comments": "merge bloqueado: hay comentarios de revisión sin resolver",
    "merge conflict — resolving automatically": "conflicto de merge: resolviéndolo automáticamente",
    "merge failed — needs your help": "el merge falló: necesito tu ayuda",
    "merge blocked": "merge bloqueado",
    "credential link": "enlace para la credencial",
    "credential received": "credencial recibida",
    "credential needed": "credencial necesaria",
    "credential not received": "credencial no recibida",
    "credential page closed": "página de credencial cerrada",
    "credential page unavailable": "página de credencial no disponible",
    "credential paused": "credencial en pausa",
    "continuing without credential": "continúo sin la credencial",
    "secure link not used": "enlace seguro no utilizado",
    "stuck in ": "bloqueado en ",
    "aborted — reset to IDLE": "abortado: reinicio a IDLE",
    "on hold": "en espera",
    " held task — ": " tarea en espera: ",
    "approval deferred": "aprobación pospuesta",
    "clarification needed": "necesito una aclaración",
    "Verification requested": "Verificación solicitada",
    " general — will continue": " general: continuaré",
    " general — nothing on hold here": " general: no hay nada en espera aquí",
    " general — nothing to hold": " general: no hay nada que poner en espera",
    " general — nothing to complete": " general: no hay nada que completar",
    "pending conversation changes": "cambios de conversación pendientes",
    "blocked: dirty working tree": "bloqueado: hay cambios sin confirmar",
    "e2e suite stuck — needs your help": "la suite e2e está bloqueada: necesito tu ayuda",
    "evidence reconciliation pending": "conciliación de evidencias pendiente",
    "evidence delivery pending": "entrega de evidencias pendiente",
    "PR ready for review": "PR listo para revisión",
    "PR updated": "PR actualizado",
    "decision selection": "selección de decisión",
    "planning context incorporated": "contexto de planificación incorporado",
    "PR instruction needs confirmation": "la instrucción sobre el PR necesita confirmación",
    "PR decision needs confirmation": "la decisión sobre el PR necesita confirmación",
    "proposal delivery pending": "entrega de propuesta pendiente",
    "unresolved review threads need your help": "hilos de revisión sin resolver: necesito tu ayuda",
    "feedback investigated": "feedback revisado",
    "feedback received": "mensaje recibido",
    "service unavailable": "servicio no disponible",
    # milestone banners and progress
    "Awaiting approval": "Esperando aprobación", "PR merged": "PR fusionado",
    "I'm exploring the codebase.": "Estoy explorando el código del repositorio.",
    "Exploration is complete; I'm preparing the OpenSpec proposal.": "La exploración terminó; estoy preparando la propuesta OpenSpec.",
    "Proposal approved; implementation is starting.": "Propuesta aprobada; comienza la implementación.",
    "Implementation is complete; I'm running checks and review.": "La implementación terminó; estoy ejecutando checks y revisión.",
    "Final checks passed; I'm archiving the change.": "Los checks finales pasaron; estoy archivando el cambio.",
    "Verification passed; running internal review": "La verificación pasó; ejecutando revisión interna",
    # bodies
    "Note: I detected work beyond the ": "Nota: detecté trabajo más allá de la fase ",
    " phase and cleaned it up (": " y lo limpié (",
    "). Implementation starts only after your approval.": "). La implementación empieza solo después de tu aprobación.",
    "The complete OpenSpec proposal is attached; implementation remains paused.\nTask: ":
        "Adjunto la propuesta OpenSpec completa; la implementación sigue en pausa.\nTarea: ",
    " gate has failed ": " ha fallado ",
    " times. Latest reason: ": " veces. Último motivo: ",
    "Reply with guidance to continue — e.g. tell the agent to fix a failure, or that a failure is pre-existing on the base branch (it will confirm that there and report it as pre-existing).":
        "Responde con indicaciones para continuar; por ejemplo, pide que el agente corrija un fallo, o indica que un fallo ya existía en la rama base (lo confirmará allí y lo reportará como preexistente).",
    "Should I return to implementation to complete or repair the pending work, or investigate whether this is pre-existing on the base branch? Reply with guidance; full results and supporting files are attached.":
        "¿Vuelvo a la implementación para completar o reparar el trabajo pendiente, o investigo si esto ya existía en la rama base? Responde con indicaciones; adjunto los resultados completos y los archivos de apoyo.",
    " gate is blocked: ": " está bloqueado: ",
    "Reported results:\n": "Resultados reportados:\n",
    "\n\nThe existing implementation is preserved. ": "\n\nLa implementación existente se conserva. ",
    "Verification is blocked by ": "La verificación está bloqueada por ",
    "What changed: Current implementation and verification are documented in the PR.\n":
        "Qué cambió: la implementación y la verificación actuales están documentadas en el PR.\n",
    "Review cover: ": "Resumen de revisión: ",
    "\n\nOpenSpec archive: `": "\n\nArchivo de OpenSpec: `",
    "Reply with change requests, or tell me to merge.": "Responde con los cambios que quieras, o dime que haga merge.",
    "\n\nI'll pick up the next pending item from the backlog.": "\n\nTomaré el siguiente elemento pendiente del backlog.",
    "Task on hold: ": "Tarea en espera: ",
    " (all work committed there)\nIt was in state ": " (todo el trabajo está confirmado allí)\nEstaba en el estado ",
    ".\n\nI'm moving on to the next backlog item. Reply 'continue' on THIS thread (optionally followed by instructions) and I'll pick this task back up first thing after my current work.":
        ".\n\nPaso al siguiente elemento del backlog. Responde 'continue' en ESTE hilo (opcionalmente con instrucciones) y retomaré esta tarea en cuanto termine mi trabajo actual.",
    "Technical results are in the attached report.": "Los resultados técnicos están en el informe adjunto.",
    "\n\nSupporting details: ": "\n\nDetalles de apoyo: ",
    "I have asked ": "He hecho ",
    " questions in a row during ": " preguntas seguidas durante ",
    " without completing the phase. The pending question was:\n\n": " sin completar la fase. La pregunta pendiente era:\n\n",
    "I opened a draft PR so you can follow the implementation:\n": "Abrí un PR en borrador para que puedas seguir la implementación:\n",
    "\n\nI push after each implementation step and mark it ready once it is verified and reviewed.":
        "\n\nSubo los cambios después de cada paso de implementación y lo marcaré como listo cuando esté verificado y revisado.",
    "\n\nFull details are in the diagnostic file: ": "\n\nLos detalles completos están en el archivo de diagnóstico: ",
    "\n1. Continue diagnosis and implement a different fix (Recommended)\n2. Provide additional evidence or the missing external prerequisite; include details after ':'\n3. Keep waiting and preserve work; do not waive checks\nReply with the option number; include any new evidence with option 2.":
        "\n1. Continuar el diagnóstico e implementar otra corrección (Recomendado)\n2. Aportar evidencia adicional o el requisito externo que falta; incluye los detalles después de ':'\n3. Seguir esperando y conservar el trabajo; no omitir checks\nResponde con el número de opción; incluye la evidencia nueva con la opción 2.",
    "\n\nBounded automatic repair attempts did not produce a controller-verified pass. Last diagnosis:\n":
        "\n\nLos intentos automáticos de reparación no lograron que los checks pasaran. Último diagnóstico:\n",
    "No successful repair diagnosis was recorded.": "No se registró ningún diagnóstico de reparación exitoso.",
    "\nI am diagnosing the logs and implementing a fix. Work is preserved; no user action is needed. Unaffected valid checks will be reused.":
        "\nEstoy diagnosticando los logs e implementando una corrección. El trabajo se conserva; no necesito nada de ti. Reutilizaré los checks válidos que no se vean afectados.",
    "Repair turn finished; controller verification is next.": "Terminó el turno de reparación; sigue la verificación del controlador.",
    "\n\nArchival failed ": "\n\nEl archivado falló ",
    "\n\nReply with guidance to retry.": "\n\nResponde con indicaciones para reintentar.",
    "Evidence unavailable (missing or over attachment limit): ": "Evidencia no disponible (falta o supera el límite de adjuntos): ",
    " queued for delivery.\n": " en cola para entrega.\n",
    "Note: I could not check the PR's review threads just now; I will re-check before any merge.\n\n":
        "Nota: no pude consultar los hilos de revisión del PR ahora; los volveré a revisar antes de cualquier merge.\n\n",
    "I did not merge: the PR branch moved after I presented it (": "No hice merge: la rama del PR cambió después de que te la presenté (",
    "\n\nPlease review the new commits and reply 'merge' again to merge them.": "\n\nRevisa los commits nuevos y responde 'merge' otra vez para fusionarlos.",
    "your decision on the pull request: ": "tu decisión sobre el pull request: ",
    "\nReply 'merge' to merge it, with the changes you want made to the PR, 'complete' to mark the task done without merging, or 'abort' to drop it.":
        "\nResponde 'merge' para fusionarlo, con los cambios que quieras en el PR, 'complete' para dar la tarea por terminada sin merge, o 'abort' para descartarla.",
    "(question not recorded; see my last message below)": "(pregunta no registrada; ve mi último mensaje abajo)",
    "help with the step I'm stuck on (": "ayuda con el paso en el que estoy bloqueado (",
    "). Reply 'retry' to try that step again, 'abort' to reset to a clean slate, 'complete' to mark the task done as-is, or reply with instructions and I'll apply them and continue.":
        "). Responde 'retry' para reintentar ese paso, 'abort' para empezar de cero, 'complete' para dar la tarea por terminada tal como está, o responde con instrucciones y las aplicaré para continuar.",
    "\n\nThe item was marked done in the backlog:\n\n": "\n\nEl elemento se marcó como terminado en el backlog:\n\n",
    "\n\nBut I could not find this item in the backlog to mark it done:\n\n": "\n\nPero no encontré este elemento en el backlog para marcarlo como terminado:\n\n",
    "\n\nPlease mark it done (or delete it) promptly — until then it may be picked again.":
        "\n\nMárcalo como terminado (o bórralo) pronto; hasta entonces podría volver a tomarse.",
    "\n\nWas working on: ": "\n\nEstaba trabajando en: ",
    "\nPrevious state: ": "\nEstado anterior: ",
    "\n\nRemote branches and PRs were left untouched. I'll pick up the next pending item from the backlog.":
        "\n\nNo toqué las ramas remotas ni los PR. Tomaré el siguiente elemento pendiente del backlog.",
    "\nWorkspace files and child branches remain local; no blanket commit or reset was performed.\nIt was in state ":
        "\nLos archivos del workspace y las ramas de los repos hijos siguen locales; no hice ningún commit ni reset general.\nEstaba en el estado ",
    ". Reply 'continue' on this thread to resume.": ". Responde 'continue' en este hilo para retomarla.",
    "\n\nRepo reset:\n- ": "\n\nReinicio del repositorio:\n- ",
    "No coding-agent turn is running; the task is unchanged.": "No hay un turno del agente en ejecución; la tarea sigue igual.",
    "Restarting this agent turn; keeping the task and saved work.": "Reiniciando este turno del agente; conservaré la tarea y el trabajo ya guardado.",
    "\nOn hold: ": "\nEn espera: ",
    " (continue requested)": " (se pidió continuar)",
    "\n\nVerification found approved work still incomplete. I'm returning to implementation to finish it; no new approval is needed.\n\n":
        "\n\nLa verificación encontró trabajo aprobado aún incompleto. Vuelvo a la implementación para terminarlo; no hace falta una nueva aprobación.\n\n",
    "\n\nNote: if you reply 'retry', please re-send the change request you want applied — the original one was consumed.":
        "\n\nNota: si respondes 'retry', vuelve a enviar el cambio que quieres aplicar; el original ya se consumió.",
    "Work is preserved. Implementation remains paused until your explicit approval.":
        "El trabajo se conserva. La implementación sigue en pausa hasta tu aprobación explícita.",
    "I could not tell whether your reply approves the proposal, requests changes, marks the task complete, or aborts:\n\n":
        "No pude determinar si tu respuesta aprueba la propuesta, pide cambios, da la tarea por terminada o la aborta:\n\n",
    "\n\nPlease reply again with an explicit approval, the changes you want, 'complete', or 'abort'.":
        "\n\nResponde de nuevo con una aprobación explícita, los cambios que quieres, 'complete' o 'abort'.",
    "\n\nVerifier findings (missing evidence or results):\n": "\n\nHallazgos del verificador (evidencia o resultados faltantes):\n",
    "Unresolved verification tasks:\n": "Tareas de verificación sin resolver:\n",
    "Note: no approved e2e evidence is available for delivery. Capture, validation or conversion may be pending; see evidence diagnostics and codebot logs.\n":
        "Nota: no hay evidencia e2e aprobada para entregar. La captura, validación o conversión puede estar pendiente; revisa los diagnósticos de evidencias y los logs de codebot.\n",
    "Reviewer provenance: ": "Origen de la revisión: ",
    "Note: the PR changed while I resolved merge conflicts with ": "Nota: el PR cambió mientras resolvía los conflictos de merge con ",
    " earlier repl(y/ies) on this thread were set aside — please re-send your instruction against the updated PR.\n\n":
        " respuesta(s) anteriores de este hilo quedaron descartadas; vuelve a enviar tu instrucción sobre el PR actualizado.\n\n",
    "The PR still conflicts with ": "El PR sigue en conflicto con ",
    " resolution attempt(s).\nPR: ": " intento(s) de resolución.\nPR: ",
    "\n\nPlease resolve the conflicts on the branch yourself (merge ": "\n\nResuelve tú los conflictos en la rama (haz merge de ",
    " in and push), then reply 'retry'.": " y sube los cambios), y luego responde 'retry'.",
    "The pull request was closed on GitHub without being merged.\nPR: ": "El pull request se cerró en GitHub sin merge.\nPR: ",
    "\n\nIf the work landed some other way, reply 'complete'; to drop the task reply 'abort'; to keep working on it, reopen the PR and reply 'retry'.":
        "\n\nSi el trabajo se integró de otra forma, responde 'complete'; para descartar la tarea responde 'abort'; para seguir trabajando en ella, reabre el PR y responde 'retry'.",
    "I could not tell whether your reply asks for changes, an abort, a merge, or to mark the task complete:\n\n":
        "No pude determinar si tu respuesta pide cambios, abortar, hacer merge o dar la tarea por terminada:\n\n",
    "\n\nPlease reply again with the change requests, 'abort', 'complete', or an explicit 'merge'.":
        "\n\nResponde de nuevo con los cambios, 'abort', 'complete' o un 'merge' explícito.",
    "The revised proposal requires approval before merge.": "La propuesta revisada necesita aprobación antes del merge.",
    "The PR content changed since your review. Review the updated PR before merging.":
        "El contenido del PR cambió desde tu revisión. Revisa el PR actualizado antes del merge.",
    "I did not merge: I couldn't query the PR's state just now.\nPR: ": "No hice merge: no pude consultar el estado del PR ahora.\nPR: ",
    "\n\nReply 'merge' to retry.": "\n\nResponde 'merge' para reintentar.",
    "\nThe PR still has unresolved review threads I could not clear after ":
        "\nEl PR todavía tiene hilos de revisión sin resolver que no pude cerrar después de ",
    " round(s): resolve them on GitHub, or reply 'merge anyway' to merge regardless.":
        " ronda(s): resuélvelos en GitHub, o responde 'merge anyway' para fusionar de todos modos.",
    "\nThe problem was:\n\n": "\nEl problema fue:\n\n",
    "Work is preserved. I will keep waiting; no action was authorized.": "El trabajo se conserva. Seguiré esperando; no se autorizó ninguna acción.",
    "Repository recovery made no verified progress. I need a concrete resolution for the preserved repository condition.":
        "La recuperación del repositorio no logró un progreso verificado. Necesito una resolución concreta para el estado conservado del repositorio.",
    "\n\nNotes from the repo reset (I'll re-check before the next task):\n- ":
        "\n\nNotas del reinicio del repositorio (lo volveré a revisar antes de la siguiente tarea):\n- ",
    "Loop detected: ": "Bucle detectado: ",
    " times without any change to the repository. Stopping instead of repeating it again.":
        " veces sin ningún cambio en el repositorio. Me detengo en lugar de repetirlo otra vez.",
    "I've failed this step ": "Este paso falló ",
    " times in a row. Last error:\n\n": " veces seguidas. Último error:\n\n",
    "I could not tell whether you want me to retry, abort, or follow instructions:\n\n":
        "No pude determinar si quieres que reintente, aborte o siga instrucciones:\n\n",
    "\n\nPlease reply 'retry', 'abort', 'complete', or with explicit instructions.":
        "\n\nResponde 'retry', 'abort', 'complete' o con instrucciones explícitas.",
    "Resuming this task with your note:\n\n": "Retomo esta tarea con tu nota:\n\n",
    "Resuming this task; I'm still waiting on your reply to my last message":
        "Retomo esta tarea; sigo esperando tu respuesta a mi último mensaje",
    "Resuming this task from state ": "Retomo esta tarea desde el estado ",
    "I will verify the existing work without repeating planning or implementation. Verification, internal review and final checks still apply.":
        "Verificaré el trabajo existente sin repetir la planificación ni la implementación. La verificación, la revisión interna y los checks finales siguen aplicando.",
    "Got it — this task is next: I'll resume it as soon as my current work is done (right away if I'm idle).":
        "Entendido: esta tarea va después; la retomaré en cuanto termine mi trabajo actual (de inmediato si estoy libre).",
    "Internal error: I was waiting on a question for unknown phase ": "Error interno: esperaba una respuesta para la fase desconocida ",
    " and cannot resume it. 'retry' resumes the task at ": " y no puedo retomarla. 'retry' retoma la tarea en ",
    " with its work and branch intact; 'abort' resets me.": " con su trabajo y su rama intactos; 'abort' me reinicia.",
    "There are pending requests or messages to evaluate before publishing or merging. Work is preserved; no publication or merge was authorized by the lateral chat.":
        "Hay solicitudes o mensajes pendientes de evaluar antes de publicar o hacer merge. El trabajo se conserva; el chat lateral no autorizó ninguna publicación ni merge.",
    "The repo working tree has uncommitted changes; codebot will not start a new task. Clean it up and reply to this email to retry.":
        "El repositorio tiene cambios sin confirmar; codebot no empezará una tarea nueva. Límpialo y responde a este mensaje para reintentar.",
    "The complete OpenSpec review package is unavailable: ": "El paquete completo de revisión de OpenSpec no está disponible: ",
    ". I cannot ask you to approve a partial proposal.": ". No puedo pedirte que apruebes una propuesta incompleta.",
    "Cannot confirm implementation completion: ": "No puedo confirmar que la implementación esté completa: ",
    "\n\nThe e2e suite has failed ": "\n\nLa suite e2e ha fallado ",
    " times in a row and I could not fix it myself (this is often caused by something outside the code, e.g. a stuck process/port left over from a prior run). Diagnostic output is attached.\n\nPlease investigate, then reply with guidance (or tell me what to try) to continue.":
        " veces seguidas y no pude arreglarla (suele deberse a algo fuera del código, como un proceso o puerto colgado de una ejecución anterior). Adjunto el diagnóstico.\n\nInvestiga y responde con indicaciones (o dime qué probar) para continuar.",
    "Existing video delivery could not be verified just now. No new recording or upload was started; retry reconciliation.":
        "No pude verificar ahora la entrega del video existente. No inicié ninguna grabación ni subida nueva; reintenta la conciliación.",
    "Drive delivery pending at ": "Entrega en Drive pendiente en ",
    "Heads up: the PR has ": "Aviso: el PR tiene ",
    " unresolved review comment(s); I'll address them while waiting, and I will not merge until they are resolved (or you say 'merge anyway'):\n\nDetails are on the PR.\n\n":
        " comentario(s) de revisión sin resolver; los atenderé mientras espero y no haré merge hasta que se resuelvan (o digas 'merge anyway'):\n\nLos detalles están en el PR.\n\n",
    " automated (OCR) review comment(s) are still unresolved; I'll address them automatically — fixing or resolving with an explanation — while waiting for your reply.\n\n":
        " comentario(s) de revisión automática (OCR) siguen sin resolver; los atenderé automáticamente, corrigiéndolos o resolviéndolos con una explicación, mientras espero tu respuesta.\n\n",
    "I couldn't merge the PR; GitHub reported:\n\n": "No pude hacer merge del PR; GitHub respondió:\n\n",
    "\n\nThis usually means ": "\n\nEsto suele significar que ",
    " moved ahead or there are conflicts. If it's a merge conflict I'll detect it and start resolving automatically on my next check; otherwise please resolve it on the branch and reply 'merge' to retry.":
        " avanzó o hay conflictos. Si es un conflicto de merge lo detectaré y empezaré a resolverlo automáticamente en mi siguiente revisión; si no, resuélvelo en la rama y responde 'merge' para reintentar.",
    "CONTINUE received, but no task is on hold on this thread.": "Recibí CONTINUE, pero no hay ninguna tarea en espera en este hilo.",
    "HOLD received, but no task is in progress.": "Recibí HOLD, pero no hay ninguna tarea en curso.",
    "DONE received, but no task is in progress.": "Recibí DONE, pero no hay ninguna tarea en curso.",
    "The PR changed. Please send your instruction again; I did not merge.": "El PR cambió. Envía tu instrucción de nuevo; no hice merge.",
    "The displayed PR decision changed. Please review it and send your decision again; I did not merge.":
        "La decisión mostrada sobre el PR cambió. Revísala y envía tu decisión de nuevo; no hice merge.",
    "The complete proposal package is still pending delivery. Please approve once you have received the full package.":
        "El paquete completo de la propuesta sigue pendiente de entrega. Apruébalo cuando lo hayas recibido completo.",
    "` has changed and this PR now conflicts with it, so I can't merge it yet.\nPR: ":
        "` cambió y este PR ahora tiene conflictos con ella, así que todavía no puedo hacer merge.\nPR: ",
    "\n\nI'm resolving the conflicts now. If any conflict has genuinely different reasonable resolutions I'll send you the alternatives; otherwise the automated review re-runs on the resolved branch and I'll message you when it's clean — reply 'merge' then.":
        "\n\nEstoy resolviendo los conflictos. Si algún conflicto admite soluciones razonables distintas te enviaré las alternativas; si no, la revisión automática se repetirá sobre la rama resuelta y te avisaré cuando esté limpia; entonces responde 'merge'.",
    "I did not merge: I couldn't query the PR's review threads to check for unresolved comments.\nPR: ":
        "No hice merge: no pude consultar los hilos de revisión del PR para comprobar si hay comentarios sin resolver.\nPR: ",
    "\n\nReply 'merge' to have me retry the check, or 'merge anyway' to merge without it.":
        "\n\nResponde 'merge' para que reintente la comprobación, o 'merge anyway' para fusionar sin ella.",
    "I did not merge: the PR has ": "No hice merge: el PR tiene ",
    " unresolved review thread(s).\nPR: ": " hilo(s) de revisión sin resolver.\nPR: ",
    "\n\nThe unresolved findings are listed on the PR.\n\nOptions: reply with change requests and I'll address them; resolve the threads on GitHub and reply 'merge' again; or reply 'merge anyway' to merge despite them.":
        "\n\nLos hallazgos sin resolver están en el PR.\n\nOpciones: responde con los cambios y los atenderé; resuelve los hilos en GitHub y responde 'merge' otra vez; o responde 'merge anyway' para fusionar a pesar de ellos.",
    "PR updated: ": "PR actualizado: ",
    "\n\nChanged (implementation report): ": "\n\nCambios (informe de implementación): ",
    "(see earlier message)": "(ver el mensaje anterior)",
    "\nNo product replanning or merge is authorized.": "\nNo se autorizó ninguna replanificación del producto ni merge.",
    "Preserved merge needs a concrete conflict resolution; diagnostics are attached.":
        "El merge conservado necesita una resolución concreta de conflictos; adjunto el diagnóstico.",
    "Archive repair did not resolve its condition; diagnostic guidance is needed.":
        "La reparación del archivado no resolvió el problema; necesito indicaciones con base en el diagnóstico.",
    "Post-repair checks remain unresolved; diagnostic guidance is needed.":
        "Los checks posteriores a la reparación siguen sin resolverse; necesito indicaciones con base en el diagnóstico.",
    "The requested supporting files are attached.": "Adjunto los archivos de apoyo solicitados.",
    "There are still ": "Todavía hay ",
    " unresolved review conversation(s) on the PR after ": " conversación(es) de revisión sin resolver en el PR después de ",
    " round(s) of fixes.\nPR: ": " ronda(s) de correcciones.\nPR: ",
    "\nReply with guidance on the remaining findings; use 'merge anyway' only to explicitly merge despite them.":
        "\nResponde con indicaciones sobre los hallazgos restantes; usa 'merge anyway' solo para fusionar explícitamente a pesar de ellos.",
    "\nConfirm the destination folder and its existing access settings. Codebot will not create or modify permissions; do not send credentials here.":
        "\nConfirma la carpeta de destino y sus permisos actuales. Codebot no crea ni modifica permisos; no envíes credenciales aquí.",
    "This step cannot continue without your help: ": "Este paso no puede continuar sin tu ayuda: ",
    "The service is unavailable. Work is preserved; automatic attempts are paused. Reply 'retry' to resume.":
        "El servicio no está disponible. Conservé el trabajo y pausé los intentos automáticos. Responde 'retry' para retomar.",
    "Your decision: approve the attached proposal to start implementation, or reply with requested changes.":
        "Tu decisión: aprueba la propuesta adjunta para empezar la implementación, o responde con los cambios que quieras.",
    "Your input: answer the question below. I'll resume the current work once you reply.":
        "Tu respuesta: contesta la pregunta de abajo. Retomaré el trabajo en cuanto respondas.",
    "Your decision: review the PR, then reply with changes or an explicit 'merge'.":
        "Tu decisión: revisa el PR y luego responde con cambios o un 'merge' explícito.",
    "Reply Yes/No or 1/2; you may also describe requested changes.": "Responde Sí/No o 1/2; también puedes describir los cambios que quieres.",
    "Reply with the option number; include details if requested. You may also write your own answer.":
        "Responde con el número de opción; incluye detalles si se piden. También puedes escribir tu propia respuesta.",
    "Reply in full text with the requested information.": "Responde en texto libre con la información solicitada.",
    "Decision needed: ": "Decisión necesaria: ",
    # decision cards (handoffs.decision)
    "No — preserve the work and keep waiting": "No: conservar el trabajo y seguir esperando",
    "Do you authorize continuing the pending implementation of the already approved spec?":
        "¿Autorizas continuar la implementación pendiente de la especificación ya aprobada?",
    "Do you authorize implementing the attached proposal?": "¿Autorizas implementar la propuesta adjunta?",
    "How should we resolve the pending approval?": "¿Cómo resolvemos la aprobación pendiente?",
    "What should I do with this PR?": "¿Qué hago con este PR?",
    "Yes — finish only the approved pending tasks, then verify (Recommended)":
        "Sí: terminar solo las tareas aprobadas pendientes y luego verificar (Recomendado)",
    "How should we reconcile the changed task scope?": "¿Cómo conciliamos el alcance modificado de la tarea?",
    "How should we resolve the missing evidence or execution prerequisite?":
        "¿Cómo resolvemos la evidencia o el requisito de ejecución que falta?",
    "Yes — approve this proposal and start implementation": "Sí: aprobar esta propuesta y empezar la implementación",
    "Is the working tree now clean so I can retry starting the task?":
        "¿Ya está limpio el repositorio para que reintente empezar la tarea?",
    "Provide the specific approval or decision requested below (Recommended)":
        "Dar la aprobación o decisión concreta que se pide abajo (Recomendado)",
    "Clarify what approval is missing": "Aclarar qué aprobación falta",
    "Keep waiting without changing the work": "Seguir esperando sin cambiar el trabajo",
    "Request changes; describe them": "Pedir cambios; descríbelos",
    "Authorize 'merge' after reviewing this PR; existing gates still apply":
        "Autorizar 'merge' después de revisar este PR; los controles existentes siguen aplicando",
    "Keep waiting without merging (Recommended)": "Seguir esperando sin hacer merge (Recomendado)",
    "Accept the current requirements and tasks as the approved scope":
        "Aceptar los requisitos y tareas actuales como el alcance aprobado",
    "Reconcile the checklist with the approved scope; specify corrections (Recommended)":
        "Conciliar la lista de tareas con el alcance aprobado; indica las correcciones (Recomendado)",
    "Request a scope revision; describe it for review": "Pedir una revisión del alcance; descríbela para revisarla",
    "Keep waiting without changing scope": "Seguir esperando sin cambiar el alcance",
    "Provide the missing evidence or recovery guidance below (Recommended)":
        "Aportar abajo la evidencia que falta o indicaciones de recuperación (Recomendado)",
    "Clarify exactly which result or prerequisite is missing": "Aclarar exactamente qué resultado o requisito falta",
    "Keep waiting; do not waive checks": "Seguir esperando; no omitir checks",
    "Explicitly authorize 'merge anyway'; bypass the review-thread gate":
        "Autorizar explícitamente 'merge anyway'; omitir el control de hilos de revisión",
    "Yes — recheck Git; start only if the working tree is clean":
        "Sí: revisar Git de nuevo; empezar solo si el repositorio está limpio",
    "How should I recover this stopped step?": "¿Cómo recupero este paso detenido?",
    "How should I continue the blocked step?": "¿Cómo continúo con el paso bloqueado?",
    "Retry the same step with existing gates; reuse valid evidence, do not mark it complete":
        "Reintentar el mismo paso con los controles existentes; reutilizar la evidencia válida, sin darlo por completado",
    "Provide specific recovery instructions; include what to change":
        "Dar instrucciones concretas de recuperación; incluye qué cambiar",
    "Keep waiting and preserve the work": "Seguir esperando y conservar el trabajo",
    "Continue with specific repair or investigation guidance (Recommended)":
        "Continuar con indicaciones concretas de reparación o investigación (Recomendado)",
    "Explain the blocker and available remedies": "Explicar el bloqueo y las soluciones disponibles",
}
# Whole messages or banner labels ("Label · detail"): too short to replace inside text.
LABELS = {
    "resumed": "reanudada", "Exploration": "Exploración", "Proposal": "Propuesta",
    "Implementation": "Implementación", "Verification": "Verificación", "Archiving": "Archivado",
    "Approval": "Aprobación",
}
_ORDERED = sorted(PHRASES.items(), key=lambda item: -len(item[0]))

OPERATIONAL = {
    "content_filtered": (
        "The provider's content filter blocked this attempt. I stopped automatic retries and preserved the work. Please reformulate your text and send it again; I will not retry the blocked message.",
        "El filtro de contenido del proveedor bloqueó este intento. Detuve los reintentos automáticos y conservé el trabajo. Cambia el texto y envíalo de nuevo; no volveré a procesar el mensaje bloqueado."),
    "received": ("Received your message; I will review it before the next step.",
                 "Recibí tu mensaje; lo revisaré antes de continuar con el siguiente paso."),
    "working": ("Working; no action needed.", "Estoy trabajando; no necesito nada de ti."),
    "unavailable": ("The service is unavailable. Work is preserved; automatic attempts are paused. Reply 'retry' to resume.",
                    "El servicio no está disponible. Conservé el trabajo y pausé los intentos automáticos. Responde 'retry' para retomar."),
    "credential_ask": (
        "To test against {service} I need the credential {env} ({purpose}). How do you want to proceed?\n"
        "1. Send it through a secure one-time link (I open a temporary page and send you its address) (Recommended)\n"
        "2. Continue without it: checks that need it are reported as skipped, not passed\n"
        "3. Pause here until you decide\n"
        "Never paste the value itself in this thread. Reply with 1, 2 or 3.",
        "Para probar contra {service} necesito la credencial {env} ({purpose}). ¿Cómo quieres seguir?\n"
        "1. Enviarla por un enlace seguro de un solo uso (abro una página temporal y te mando su dirección) (Recomendado)\n"
        "2. Continuar sin ella: los checks que la necesiten se reportan como omitidos, no como aprobados\n"
        "3. Pausar aquí hasta que decidas\n"
        "Nunca pegues el valor en este hilo. Responde 1, 2 o 3."),
    "credential_link": (
        "Open {url} , paste only the value of {env}, keep 'Burn after reading' checked and press Create. "
        "Then reply here with the link it gives you (it includes the key after '#'). "
        "I read it once, the paste is destroyed, and I close the page. The page closes by itself in {minutes} minutes.",
        "Abre {url} , pega solo el valor de {env}, deja marcado 'Burn after reading' y pulsa Create. "
        "Luego responde aquí con el enlace que te da (incluye la clave después de '#'). "
        "Lo leo una sola vez, el paste se destruye y cierro la página. La página se cierra sola en {minutes} minutos."),
    "credential_received": (
        "Received {env}. It is stored privately for {hours} hours and never written to the repository, logs or this thread. Continuing.",
        "Recibí {env}. Queda guardada en privado por {hours} horas y nunca se escribe en el repositorio, los logs ni este hilo. Continúo."),
    "credential_skipped": (
        "Continuing without {env}. Checks that need it will be reported as skipped, not as passed.",
        "Continúo sin {env}. Los checks que la necesiten se reportarán como omitidos, no como aprobados."),
    "credential_paused": (
        "Paused. Reply 1 when you want the secure link, or 2 to continue without {env}.",
        "En pausa. Responde 1 cuando quieras el enlace seguro, o 2 para continuar sin {env}."),
    "credential_failed": (
        "I could not use that link: {problem} Reply 1 for a new secure page, 2 to continue without {env}, or 3 to pause.",
        "No pude usar ese enlace: {problem} Responde 1 para una página segura nueva, 2 para continuar sin {env}, o 3 para pausar."),
    "credential_unavailable": (
        "I could not open the secure page: {reason} You can send a link from an allowed PrivateBin instance{allowed}, "
        "reply 2 to continue without {env}, or 3 to pause.",
        "No pude abrir la página segura: {reason} Puedes mandar un enlace de una instancia PrivateBin permitida{allowed}, "
        "responder 2 para continuar sin {env}, o 3 para pausar."),
    "credential_page_closed": (
        "The secure page for {env} closed without receiving it. Reply 1 for a new one, 2 to continue without it, or 3 to pause.",
        "La página segura para {env} se cerró sin recibirla. Responde 1 para una nueva, 2 para continuar sin ella, o 3 para pausar."),
    "credential_unexpected": (
        "I received a secure link but I was not waiting for a credential, so I did not open it. Nothing was stored.",
        "Recibí un enlace seguro pero no estaba esperando una credencial, así que no lo abrí. No guardé nada."),
}


def operational(kind, language="English"):
    return OPERATIONAL[kind][1 if language.casefold() == "spanish" else 0]


def spanish(message):
    label, sep, rest = message.partition(" · ")
    if label in LABELS:
        return LABELS[label] + sep + spanish(rest) if sep else LABELS[label]
    for english, text in _ORDERED:
        message = message.replace(english, text)
    return message


def translate(message, language):
    """The message in the task language, or None when only a model can translate it."""
    if language.casefold() == "english":
        return message
    if language.casefold() == "spanish":
        return spanish(message)
    return None
