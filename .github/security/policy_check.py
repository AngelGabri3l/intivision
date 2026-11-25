#!/usr/bin/env python3
"""
Chequeo de políticas de seguridad derivado de la auditoría (security-audit, run-1).

Revisa la configuración versionada (compose, Caddy, MQTT, Dockerfiles, dependencias, ignores) y
falla si encuentra una violación de nivel "error". Las de nivel "warning" se muestran pero no bloquean.

Uso:  python .github/security/policy_check.py [--root .] [--strict] [--scope FILE] [--json-out FILE]
      --strict   convierte los warnings en errores.
      --scope    archivo de alcance (por defecto .github/security/alcance.yml): los hallazgos de
                 componentes fuera de alcance se muestran pero no bloquean.
      --json-out guarda los hallazgos en JSON (lo usa el job de resumen).

Cada regla cita la pista de la auditoría que la originó (ver docs/pipeline-seguridad.md).
"""
import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:  # el workflow instala PyYAML; sin él no se pueden leer compose ni config
    print("::error::PyYAML no está instalado (pip install pyyaml)")
    sys.exit(2)

PUBLIC_MQTT_BROKERS = {
    "broker.hivemq.com", "test.mosquitto.org", "broker.emqx.io",
    "mqtt.eclipseprojects.io", "public.mqtthq.com", "broker.mqttdashboard.com",
}
LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")

findings = []


def add(level, rule, path, line, message, component="general"):
    findings.append({"level": level, "rule": rule, "path": path, "line": line or 1,
                     "message": message, "component": component})


def read(root, rel):
    p = os.path.join(root, rel)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def line_of(text, needle):
    for i, ln in enumerate(text.splitlines(), 1):
        if needle in ln:
            return i
    return 1


# ---------------------------------------------------------------- reglas

def check_compose(root):
    """POL-001: ningún servicio interno publica puertos en todas las interfaces (auditoría: pista 6)."""
    rel = "docker-compose.yml"
    text = read(root, rel)
    if text is None:
        return
    data = yaml.safe_load(text) or {}
    exposed_ok = {"caddy"}  # el proxy TLS es la única entrada prevista
    for name, svc in (data.get("services") or {}).items():
        for port in svc.get("ports") or []:
            spec = str(port if not isinstance(port, dict) else f"{port.get('host_ip', '')}:{port.get('published')}:{port.get('target')}")
            parts = spec.split(":")
            bound_loopback = len(parts) >= 3 and parts[0].strip("[]") in [h.strip("[]") for h in LOOPBACK]
            if name in exposed_ok or bound_loopback:
                continue
            level = "error" if name == "backend" else "warning"
            add(level, "POL-001", rel, line_of(text, spec),
                f"El servicio '{name}' publica el puerto {spec} en todas las interfaces. "
                f"Usa '127.0.0.1:{parts[-2] if len(parts) > 1 else parts[0]}:{parts[-1]}' o quita 'ports' "
                f"y deja que solo Caddy reciba tráfico externo.",
                component="mqtt" if name == "mqtt" else "general")


def check_mosquitto(root):
    """POL-002: el broker propio no admite clientes anónimos (auditoría: mejoras recomendadas / pista 1)."""
    rel = "mosquitto.conf"
    text = read(root, rel)
    if text is None:
        return
    if re.search(r"^\s*allow_anonymous\s+true\b", text, re.M):
        add("warning", "POL-002", rel, line_of(text, "allow_anonymous"),
            "mosquitto permite clientes anónimos. Usa 'allow_anonymous false' con password_file y acl_file.",
            component="mqtt")


def check_actuator(root):
    """POL-003: la sirena no se controla desde un broker MQTT público sin credenciales (auditoría: pista 1)."""
    rel = "config.yaml"
    text = read(root, rel)
    if text is None:
        return
    cfg = yaml.safe_load(text) or {}
    act = cfg.get("actuator") or {}
    if str(act.get("type", "")).lower() != "mqtt":
        return
    host = str(act.get("host") or "").lower()
    if host in PUBLIC_MQTT_BROKERS:
        add("error", "POL-003", rel, line_of(text, "host:"),
            f"El actuador de la sirena usa el broker público '{host}'. Cualquiera que conozca el tópico "
            f"puede encender o apagar la sirena. Usa un broker propio con usuario/contraseña y TLS.",
            component="mqtt")
    if not act.get("username") and not act.get("password"):
        add("warning", "POL-003b", rel, line_of(text, "username:"),
            "El actuador MQTT no tiene credenciales en config.yaml; verifica que MQTT_USERNAME/MQTT_PASSWORD "
            "se definan en el despliegue.", component="mqtt")
    if int(act.get("port") or 1883) == 1883:
        add("warning", "POL-003c", rel, line_of(text, "port:"),
            "MQTT en el puerto 1883 viaja sin cifrar; usa 8883 con TLS.", component="mqtt")


def check_firmware(root):
    """POL-004: el firmware no lleva un broker público ni un tópico fijo (auditoría: pista 1)."""
    rel = "wokwi/sketch.ino"
    text = read(root, rel)
    if text is None:
        return
    for host in PUBLIC_MQTT_BROKERS:
        if host in text:
            add("warning", "POL-004", rel, line_of(text, host),
                f"El firmware se conecta al broker público '{host}' sin autenticación.", component="mqtt")
    if re.search(r"\.connect\(\s*[\w.()]+\s*\)", text):
        add("warning", "POL-004b", rel, line_of(text, ".connect("),
            "mqtt.connect() sin usuario ni contraseña: cualquier publicador del tópico controla la sirena.",
            component="mqtt")


def check_caddy(root):
    """POL-005/006: sin credenciales en el Caddyfile y con límite de cuerpo (auditoría: pistas 5 y 14)."""
    rel = "Caddyfile"
    text = read(root, rel)
    if text is None:
        return
    if re.search(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}", text) or "basic_auth" in text or "basicauth" in text:
        add("error", "POL-005", rel, line_of(text, "basic"),
            "El Caddyfile contiene basic_auth o un hash bcrypt. Las credenciales no se versionan; "
            "usa una variable de entorno ({$VAR}) o un archivo fuera del repositorio.")
    if "max_size" not in text:
        add("warning", "POL-006", rel, 1,
            "Caddy no limita el tamaño del cuerpo de las peticiones. Agrega 'request_body { max_size 210MB }' "
            "para que un cuerpo chunked no se reciba completo antes de llegar a la app.")


def check_dockerfiles(root):
    """POL-007: los contenedores no corren como root (auditoría: mejoras recomendadas)."""
    for rel in ("Dockerfile", "backend/Dockerfile", "edge/Dockerfile"):
        text = read(root, rel)
        if text is None:
            continue
        users = re.findall(r"^\s*USER\s+(\S+)", text, re.M | re.I)
        if not users or users[-1].lower() in ("root", "0", "0:0"):
            add("warning", "POL-007", rel, 1,
                "La imagen corre como root (no hay USER sin privilegios al final del Dockerfile).")


def check_requirements(root):
    """POL-008: dependencias fijadas a una versión exacta (auditoría: mejoras recomendadas)."""
    rel = "requirements.txt"
    text = read(root, rel)
    if text is None:
        return
    for i, ln in enumerate(text.splitlines(), 1):
        s = ln.split("#", 1)[0].strip()
        if not s or s.startswith("-"):
            continue
        if "==" not in s:
            add("warning", "POL-008", rel, i, f"'{s}' no está fijada con '=='; las builds no son reproducibles.")


def check_ignores(root):
    """POL-009: los ignores cubren secretos y datos locales (auditoría: mejoras recomendadas)."""
    gi = read(root, ".gitignore") or ""
    for pat in (".env", ".env.*", "*.pem", "*.key"):
        if pat not in gi.split():
            add("warning", "POL-009", ".gitignore", 1, f".gitignore no incluye '{pat}'.")
    di = read(root, ".dockerignore")
    if di is not None:
        for pat in ("*.dump", "backups/", "*.mp4", ".git/"):
            if pat not in di.split():
                add("warning", "POL-009b", ".dockerignore", 1,
                    f".dockerignore no incluye '{pat}': 'COPY . .' lo metería en la imagen.")


def check_tracked_secrets(root):
    """POL-010: no hay archivos de secretos versionados."""
    bad = (".env", ".pem", ".key", "credentials.json", "id_rsa")
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "venv", ".venv")]
        for f in files:
            if f == ".env" or f.startswith(".env.") and not f.endswith(".example") or f.endswith(bad[1:]):
                rel = os.path.relpath(os.path.join(base, f), root).replace("\\", "/")
                add("error", "POL-010", rel, 1, "Archivo de secretos dentro del repositorio.")


# ---------------------------------------------------------------- reglas nuevas (auditoría run-2)

SECURITY_ENV = re.compile(r"^(PASARELA\w*|MP_\w+|\w*(SECRET|TOKEN|API_KEY|PASSWORD)\w*|PUBLIC_BASE_URL|ALLOWED_ORIGINS)$")
NOT_SERVICE_ENV = {"EDGE_TOKEN"}       # lo usa backend/test_ingest.py (script manual), no el servicio


def _backend_compose_env(root):
    text = read(root, "docker-compose.yml")
    if text is None:
        return None, None, None
    data = yaml.safe_load(text) or {}
    svc = (data.get("services") or {}).get("backend") or {}
    env = svc.get("environment") or []
    names = set()
    if isinstance(env, dict):
        names = set(env)
    else:
        for item in env:
            names.add(str(item).split("=", 1)[0].strip())
    return text, names, svc


def check_backend_env(root):
    """POL-011: las variables de seguridad que lee el backend llegan al contenedor (auditoría run-2)."""
    text, names, svc = _backend_compose_env(root)
    if text is None:
        return
    if svc.get("env_file"):
        return                          # un env_file entrega todo el .env: no se puede verificar desde el repo
    read_vars = {}
    backend_dir = os.path.join(root, "backend")
    for f in sorted(os.listdir(backend_dir)) if os.path.isdir(backend_dir) else []:
        if not f.endswith(".py"):
            continue
        src = read(root, f"backend/{f}") or ""
        for m in re.finditer(r"os\.(?:environ\.get|getenv)\(\s*[\"'](\w+)[\"']", src):
            var = m.group(1)
            if SECURITY_ENV.match(var) and var not in NOT_SERVICE_ENV:
                read_vars.setdefault(var, f"backend/{f}")
    for var, where in sorted(read_vars.items()):
        if var not in names:
            add("warning", "POL-011", "docker-compose.yml", line_of(text, "environment:"),
                f"El backend lee {var} ({where}) pero docker-compose.yml no se la pasa y no hay env_file: "
                f"aunque esté en el .env del servidor, el contenedor no la recibe y se usa el valor por defecto.")


def check_public_base_url(root):
    """POL-012: los enlaces absolutos no dependen de la petición (auditoría run-2, enlace de recuperación)."""
    text, names, _ = _backend_compose_env(root)
    dockerfile = read(root, "backend/Dockerfile") or ""
    if text is None:
        return
    m = re.search(r"PUBLIC_BASE_URL=\$\{PUBLIC_BASE_URL:?-([^}]*)\}", text)
    empty_default = m is not None and m.group(1).strip() == ""
    proxy_headers = "--proxy-headers" in dockerfile or "forwarded-allow-ips" in dockerfile
    if (empty_default or "PUBLIC_BASE_URL" not in (names or set())) and not proxy_headers:
        add("warning", "POL-012", "docker-compose.yml", line_of(text, "PUBLIC_BASE_URL"),
            "PUBLIC_BASE_URL puede quedar vacío y uvicorn no confía en X-Forwarded-Proto: el enlace de "
            "recuperación de contraseña se arma con request.base_url y sale como http://. Usa "
            "${PUBLIC_BASE_URL:?...} (obligatorio) o agrega --proxy-headers --forwarded-allow-ips al uvicorn.")


def check_edge_compose(root):
    """POL-013: el agente de la mini PC no tiene más acceso al equipo del necesario (auditoría run-2)."""
    rel = "edge/docker-compose.yml"
    text = read(root, rel)
    if text is None:
        return
    data = yaml.safe_load(text) or {}
    for name, svc in (data.get("services") or {}).items():
        if svc.get("network_mode") == "host":
            add("warning", "POL-013", rel, line_of(text, "network_mode"),
                f"'{name}' usa network_mode: host: si el agente se compromete, ve toda la red de la mini PC.")
        for vol in svc.get("volumes") or []:
            if str(vol).split(":")[0].strip() == "/dev":
                add("warning", "POL-013b", rel, line_of(text, "/dev:/dev"),
                    f"'{name}' monta /dev completo del equipo; limita a los dispositivos de video (devices:).")
        if svc.get("privileged"):
            add("error", "POL-013c", rel, line_of(text, "privileged"),
                f"'{name}' corre en modo privileged: equivale a root en la mini PC.")


CHECKS = [check_compose, check_mosquitto, check_actuator, check_firmware, check_caddy,
          check_dockerfiles, check_requirements, check_ignores, check_tracked_secrets,
          check_backend_env, check_public_base_url, check_edge_compose]


def load_scope(path):
    """Devuelve {componente: entrada} de los componentes declarados fuera de alcance."""
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    scope = {}
    for entry in data.get("fuera_de_alcance") or []:
        comp = str(entry.get("componente", "")).strip()
        if comp and str(entry.get("motivo", "")).strip():   # sin motivo no se acepta la exclusión
            scope[comp] = entry
    return scope


def md(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--strict", action="store_true", help="los warnings también fallan")
    ap.add_argument("--scope", default=".github/security/alcance.yml")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    for check in CHECKS:
        check(args.root)

    scope = load_scope(os.path.join(args.root, args.scope) if not os.path.isabs(args.scope) else args.scope)
    for f in findings:
        if f["component"] in scope:
            f["original_level"] = f["level"]
            f["level"] = "fuera-de-alcance"
            f["scope_state"] = scope[f["component"]].get("estado", "fuera-de-alcance")
        elif args.strict:
            f["level"] = "error"

    in_scope = [f for f in findings if f["level"] != "fuera-de-alcance"]
    out_scope = [f for f in findings if f["level"] == "fuera-de-alcance"]
    errors = [f for f in in_scope if f["level"] == "error"]
    warnings = [f for f in in_scope if f["level"] == "warning"]

    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for f in findings:
        if f["level"] == "fuera-de-alcance":
            tag, label = "notice", f"[fuera de alcance: {f['component']} {f['scope_state']}] "
        else:
            tag, label = f["level"], ""
        if gha:
            print(f"::{tag} file={f['path']},line={f['line']},title={f['rule']}::{label}{f['message']}")
        else:
            print(f"[{tag.upper():7}] {f['rule']:9} {f['path']}:{f['line']}  {label}{f['message']}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"findings": findings, "scope": scope}, fh, ensure_ascii=False, indent=2)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Chequeo de políticas (auditoría run-1)\n\n")
            fh.write(f"**{len(errors)} bloqueantes · {len(warnings)} avisos · "
                     f"{len(out_scope)} fuera de alcance**\n\n")
            if in_scope:
                fh.write("| Nivel | Regla | Archivo | Mensaje |\n|---|---|---|---|\n")
                for f in sorted(in_scope, key=lambda x: (x["level"] != "error", x["rule"])):
                    fh.write(f"| {f['level']} | {f['rule']} | `{f['path']}:{f['line']}` | {md(f['message'])} |\n")
            for comp, entry in scope.items():
                items = [f for f in out_scope if f["component"] == comp]
                fh.write(f"\n### Fuera de alcance: `{comp}` ({entry.get('estado', '')})\n\n")
                fh.write(f"{md(entry.get('motivo', ''))}\n\n**Revisar cuando:** {md(entry.get('revisar_cuando', '-'))}\n\n")
                if items:
                    fh.write("<details><summary>" + f"{len(items)} hallazgo(s) que no bloquean</summary>\n\n")
                    fh.write("| Nivel original | Regla | Archivo | Mensaje |\n|---|---|---|---|\n")
                    for f in items:
                        fh.write(f"| {f['original_level']} | {f['rule']} | `{f['path']}:{f['line']}` | {md(f['message'])} |\n")
                    fh.write("\n</details>\n")
    print(f"\n{len(errors)} bloqueante(s), {len(warnings)} aviso(s), {len(out_scope)} fuera de alcance.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
