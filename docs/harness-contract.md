# Harness contract v1

El archivo opcional `e2e/codebot-harness.json` declara `version: 1`, `checks`
(mismo contrato que `.codebot/checks.json`) y `preparation` con `inputs`,
`commands`, `probes`, `timeout`. Cada check declara suites/proveedores en argv,
cwd, recursos exclusivos, reporter y entradas relevantes. Nunca agregar flags
que el run.sh existente no soporte. Sin contrato se usa `./run.sh`.

Los outputs se declaran mediante `CODEBOT_ARTIFACT_MANIFEST`, un JSON por ejecución:
`version`, `run_id`, `snapshot`, `status`, `artifacts` (kind, path, test_id, hash).
Preparación separada verifica dependencias y browsers por fingerprint; los probes
deben fallar si una instalación está incompleta.

Los harnesses compartidos mantienen workers=1. Para incrementarlos primero
aislar propietarios de sesión, bases/workspaces, puertos, containers e imágenes.
Compartir nombres de proyecto Compose o carpetas de output no constituye aislamiento.

## Configuración específica del destino

Coderbot no conoce aplicaciones, proveedores ni inventarios de specs particulares.
Los grupos y recursos compartidos se declaran en `e2e/codebot-harness.json`:

```json
{
  "version": 1,
  "checks": [
    {"id": "provider-a", "argv": ["./run.sh", "--provider", "provider-a", "a.spec.ts", "b.spec.ts"], "cwd": "e2e", "resources": ["shared-harness"]},
    {"id": "provider-b", "argv": ["./run.sh", "--provider", "provider-b", "c.spec.ts"], "cwd": "e2e", "resources": ["shared-harness"]}
  ]
}
```

Los flags del ejemplo solo se usan si el harness destino los soporta. El recurso
compartido mantiene ejecución serial sin inferir puertos, nombres de archivos o
proveedores. Checks en otras rutas, incluidas aplicaciones anidadas, se declaran
en `.codebot/checks.json`; no se detectan directorios propios de una aplicación.

La grabación exporta `PW_VIDEO=on` para harnesses que lo consuman y conserva el
entorno configurado. La detección legacy se limita a `e2e/test-results/`; artifacts
externos se entregan con `CODEBOT_ARTIFACT_MANIFEST` y procedencia verificable.

Para habilitar workers de Playwright, cada worker debe recibir un workspace y
OUTPUT_PATH propios y metadata de owner escrita antes de hacer visible la sesión.
Para ejecutar proveedores simultáneamente deben recibir puertos y nombres de
imagen/containers propios. Hasta completar esos cambios, mantener `workers: 1`.
