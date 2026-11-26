# Pipeline de seguridad (GitHub Actions)

El workflow [`.github/workflows/security-ci.yml`](../.github/workflows/security-ci.yml) convierte los hallazgos de las auditorías de seguridad del proyecto en controles automáticos. Las auditorías se hicieron con la skill security-audit en perfil `quick`:

- **run-1:** commit `5999fbf`.
- **run-2:** commit `6e3cdf1`. Encontró 2 hallazgos confirmados y 14 pendientes de validar.

## Cuándo se ejecuta

| Evento | Qué revisa |
|---|---|
| Pull request a `main` | Todos los jobs. Gitleaks revisa solo los commits del PR |
| Push a `main` o tag `v*` | Todos los jobs |
| Lunes 05:23 UTC (`schedule`) | Todos los jobs. Gitleaks revisa **todo el historial** |
| Manual (`workflow_dispatch`) | Igual que el semanal |

Un PR que viene de un *fork* recibe un token de solo lectura, así que no puede publicar en Code Scanning. En ese caso el workflow se salta esa publicación y los resultados igual aparecen en el job **Resumen de vulnerabilidades**.

## Jobs

| Job | Herramienta | ¿Bloquea? | Qué cubre |
|---|---|---|---|
| `secrets` | Gitleaks + [`.gitleaks.toml`](../.gitleaks.toml) | Sí | Hash bcrypt de `basic_auth` en el historial (**confirmado en run-2**), secretos `whsec_…` (**confirmado en run-2**), tokens de Mercado Pago, Telegram y Anthropic, URLs de Postgres con contraseña |
| `policy` | [`policy_check.py`](../.github/security/policy_check.py) | Sí, en reglas de nivel *error* | Puertos expuestos, credenciales en el Caddyfile, archivos de secretos, contenedores como root, variables de seguridad que no llegan al contenedor, enlace de recuperación por `http`, acceso del agente a la mini PC |
| `semgrep` | Semgrep (`p/python`, `p/secrets` y [reglas propias](../.github/security/semgrep-intivision.yml)) | Sí, en severidad ERROR | Secreto con valor por defecto en el código (**confirmado en run-2**), enlaces armados con `request.base_url`, conexión a la base retenida en un streaming, `storage_key` del primero que lo reclama, tokens en logs |
| `bandit` | Bandit (excluye `tools/`, que son scripts de evaluación) | Sí, en severidad alta con confianza media o alta | Patrones inseguros genéricos de Python |
| `codeql` | CodeQL (`python`, `actions`), consultas `security-extended` | No; publica en Code Scanning | Flujo de datos: inyección y rutas |
| `iac` | Hadolint (backend, detector y `edge/Dockerfile`) + Trivy config | Hadolint en *error* | Dockerfiles |
| `images` | Build + Trivy de las imágenes backend, detector y **edge** (`MOTOR=0`) | Sí, con HIGH/CRITICAL que tengan parche | Dependencias realmente instaladas, también las del agente |
| `resumen` | [`sarif_summary.py`](../.github/security/sarif_summary.py) | No | Junta los reportes de todos los jobs y los imprime |

Además, [`dependabot.yml`](../.github/dependabot.yml) abre cada semana PRs de actualización para `pip` y `docker` en `/`, `/backend` y `/edge`, y para las GitHub Actions.

## Hallazgos de run-2 y el control que los vigila

| Hallazgo de run-2 | Estado | Control en el pipeline |
|---|---|---|
| Secreto del webhook simulado escrito en el código (`backend/pasarela.py:82`) | **Confirmado (baja)** | Semgrep `secret-env-fallback-literal` (**bloquea**) y gitleaks `webhook-signing-secret` |
| Hash bcrypt de `basic_auth` en el historial de git | **Confirmado (baja)** | Gitleaks `bcrypt-hash` en el escaneo semanal del historial (**bloquea**) |
| El compose no pasa `PASARELA`, `MP_*`, `PASARELA_WEBHOOK_SECRET`, `ANTHROPIC_API_KEY` | Pendiente de validar | `policy` POL-011 (aviso) |
| Enlace de recuperación por `http` cuando `PUBLIC_BASE_URL` está vacío | Pendiente de validar | `policy` POL-012 y Semgrep `link-from-request-base-url` (avisos) |
| `/ingest` JSON acepta nombres de imagen del cliente (`storage_key`) | Pendiente de validar | Semgrep `global-storage-key-first-claimant` (aviso) |
| La exportación CSV retiene una conexión del pool | Pendiente de validar | Semgrep `db-connection-held-in-generator` (aviso) |
| Agente de la mini PC con `network_mode: host`, `/dev` y root | Mejora recomendada | `policy` POL-007 y POL-013 (avisos; `privileged` bloquearía) |
| Dependencias e imágenes sin versión fija | Mejora recomendada | Dependabot y Trivy de imágenes |

Hay pistas que el análisis estático no puede detectar y que necesitan tests de integración (ver `NEEDS-VALIDATION.md` de la auditoría). Son:

- el vínculo de Telegram de recuperación que no se revoca;
- los WebSockets en vivo que no se revalidan;
- el token de la mini PC revocado que sigue en el WebSocket;
- la pasarela simulada sin restricción en modo Mercado Pago;
- la cola única de Telegram;
- el oráculo de tiempo en el login bloqueado.

## Reglas del chequeo de políticas

| Regla | Nivel | Qué exige |
|---|---|---|
| POL-001 | error (backend), warning (otros) | Ningún servicio publica puertos en todas las interfaces; solo Caddy recibe tráfico externo. Hoy el backend usa `127.0.0.1:8000` y cumple |
| POL-002 | warning (fuera de alcance) | `mosquitto.conf` no tiene `allow_anonymous true` |
| POL-003 | error (fuera de alcance) | El actuador de la sirena no usa un broker MQTT público |
| POL-004 | warning (fuera de alcance) | El firmware no apunta a un broker público ni se conecta sin credenciales |
| POL-005 | error | El Caddyfile no contiene `basic_auth` ni hashes bcrypt |
| POL-006 | warning | Caddy limita el tamaño del cuerpo (`request_body max_size`) |
| POL-007 | warning | Los Dockerfiles (también `edge/Dockerfile`) terminan con un `USER` sin privilegios |
| POL-008 | warning | `requirements.txt` fija versiones con `==` |
| POL-009 | warning | `.gitignore` y `.dockerignore` excluyen secretos, dumps y videos |
| POL-010 | error | No hay archivos `.env`, `.pem` ni `.key` versionados |
| **POL-011** | warning | Toda variable de seguridad que lee el backend (`*SECRET*`, `*TOKEN*`, `*API_KEY*`, `PASARELA*`, `MP_*`, `PUBLIC_BASE_URL`, `ALLOWED_ORIGINS`) se pasa en `docker-compose.yml` o mediante `env_file` |
| **POL-012** | warning | `PUBLIC_BASE_URL` es obligatorio, o uvicorn corre con `--proxy-headers` |
| **POL-013** | warning; `privileged` es error | El agente de la mini PC no usa red del host ni monta `/dev` completo |

Para probarlo localmente:

```bash
pip install pyyaml==6.0.3
python .github/security/policy_check.py --root .
```

## Alcance

[`.github/security/alcance.yml`](../.github/security/alcance.yml) declara el **módulo MQTT de la sirena** como no desarrollado. Sus hallazgos se muestran con la etiqueta *fuera de alcance* y no bloquean. Cuando se desarrolle, hay que borrar esa entrada.

## Estado esperado del pipeline hoy

- **`semgrep` falla a propósito** por el hallazgo confirmado en `backend/pasarela.py:82`. Se corrige quitando el valor por defecto: si falta `PASARELA_WEBHOOK_SECRET`, la ruta debe responder 503. También hay que pasar la variable en el compose.
- **El escaneo semanal de `secrets` falla** por dos motivos:
  - el hash de `basic_auth` del historial (commits `b99bce4`…`41b30dc`);
  - el literal `whsec_…` del mismo archivo.

  El primero se resuelve cambiando esa contraseña y limpiando el historial, o aceptando el riesgo en `.gitleaksignore`. El segundo se resuelve al corregir el código.
- **`policy` pasa:** el puerto 8000 ya no está expuesto. Los hallazgos de run-2 que dependen del despliegue aparecen como avisos.

## Dónde ver los resultados

1. **Actions → la ejecución → Resumen de vulnerabilidades:** muestra el estado de cada job, las políticas y una tabla por herramienta. Los reportes quedan como artefactos `report-*` durante 14 días.
2. **Security → Code scanning:** Semgrep, Bandit, CodeQL y Trivy. Se publican en PRs de ramas del mismo repo y en `main`.
3. **Security → Dependabot y Secret scanning:** se activan en *Settings → Code security* del repositorio.
