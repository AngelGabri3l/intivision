#!/usr/bin/env python3
"""
Resumen consolidado de seguridad para GitHub Actions.

Lee los SARIF de todas las herramientas (Semgrep, Bandit, CodeQL, Trivy) y el JSON del chequeo de
políticas, y escribe:
  - una tabla en el resumen de la ejecución ($GITHUB_STEP_SUMMARY), y
  - el mismo detalle en texto en el log del job.

Uso: python sarif_summary.py <directorio-con-reportes> [--max-rows 40]
Variables opcionales: NEEDS_JSON (toJSON(needs)) para mostrar el estado de cada job.
Solo usa la biblioteca estándar.
"""
import argparse
import glob
import json
import os
import sys

ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
ICON = {"CRITICAL": "🟥", "HIGH": "🟧", "MEDIUM": "🟨", "LOW": "🟦", "INFO": "⬜"}
LEVEL_MAP = {"error": "HIGH", "warning": "MEDIUM", "note": "LOW", "none": "INFO"}
JOB_NAMES = {
    "secrets": "Secretos (gitleaks)", "policy": "Políticas de configuración", "semgrep": "Semgrep",
    "bandit": "Bandit", "codeql": "CodeQL", "iac": "Dockerfiles (Hadolint + Trivy config)",
    "images": "Imágenes (Trivy)",
}
RESULT_ICON = {"success": "✅", "failure": "❌", "cancelled": "⚪", "skipped": "⏭️"}


def severity(result, rules):
    rule = rules.get(result.get("ruleId"), {})
    props = rule.get("properties") or {}
    score = props.get("security-severity")
    if score is not None:
        try:
            s = float(score)
            return "CRITICAL" if s >= 9 else "HIGH" if s >= 7 else "MEDIUM" if s >= 4 else "LOW" if s > 0 else "INFO"
        except ValueError:
            pass
    level = result.get("level") or (rule.get("defaultConfiguration") or {}).get("level") or "warning"
    return LEVEL_MAP.get(level, "MEDIUM")


def location(result):
    for loc in result.get("locations") or []:
        phys = loc.get("physicalLocation") or {}
        uri = (phys.get("artifactLocation") or {}).get("uri", "")
        line = (phys.get("region") or {}).get("startLine")
        if uri:
            return f"{uri}:{line}" if line else uri
    return "-"


def one_line(text, limit=170):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def load_sarif(path):
    """Devuelve [(herramienta, categoría, [hallazgos])] de un archivo SARIF."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"::warning::No se pudo leer {path}: {exc}")
        return []
    out = []
    category = os.path.splitext(os.path.basename(path))[0]
    for run in data.get("runs") or []:
        driver = (run.get("tool") or {}).get("driver") or {}
        tool = driver.get("name") or "desconocida"
        rules = {r.get("id"): r for r in driver.get("rules") or []}
        for ext in (run.get("tool") or {}).get("extensions") or []:      # CodeQL guarda reglas aquí
            rules.update({r.get("id"): r for r in ext.get("rules") or []})
        items, seen = [], set()
        for res in run.get("results") or []:
            sev = severity(res, rules)
            msg = one_line((res.get("message") or {}).get("text", ""))
            key = (res.get("ruleId"), location(res), msg)
            if key in seen:            # Trivy repite el mismo CVE por cada ruta del paquete
                continue
            seen.add(key)
            out_of_scope = "Fuera de alcance" in msg
            items.append({"sev": sev, "rule": res.get("ruleId") or "-", "loc": location(res),
                          "msg": msg, "out_of_scope": out_of_scope})
        out.append((tool, category, items))
    return out


def md(text):
    return str(text).replace("|", "\\|")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("reports")
    ap.add_argument("--max-rows", type=int, default=40)
    args = ap.parse_args()

    lines = ["# 🔐 Resumen de seguridad", ""]
    console = []

    # Estado de los jobs
    needs = {}
    try:
        needs = json.loads(os.environ.get("NEEDS_JSON") or "{}")
    except ValueError:
        pass
    if needs:
        lines += ["## Estado de los jobs", "", "| Job | Resultado |", "|---|---|"]
        for job, info in needs.items():
            res = info.get("result", "?")
            lines.append(f"| {JOB_NAMES.get(job, job)} | {RESULT_ICON.get(res, '❔')} {res} |")
            console.append(f"JOB {job:10} {res}")
        lines.append("")

    # Políticas
    policy_files = glob.glob(os.path.join(args.reports, "**", "policy.json"), recursive=True)
    if policy_files:
        with open(policy_files[0], encoding="utf-8") as fh:
            pol = json.load(fh)
        f = pol.get("findings", [])
        err = [x for x in f if x["level"] == "error"]
        warn = [x for x in f if x["level"] == "warning"]
        oos = [x for x in f if x["level"] == "fuera-de-alcance"]
        lines += ["## Políticas de configuración", "",
                  f"**{len(err)} bloqueantes · {len(warn)} avisos · {len(oos)} fuera de alcance**", ""]
        if err:
            lines += ["| Regla | Archivo | Mensaje |", "|---|---|---|"]
            lines += [f"| ❌ {x['rule']} | `{x['path']}:{x['line']}` | {md(x['message'])} |" for x in err]
            lines.append("")
        for comp, entry in (pol.get("scope") or {}).items():
            n = len([x for x in oos if x["component"] == comp])
            lines.append(f"> **Fuera de alcance — `{comp}` ({entry.get('estado', '')})**: "
                         f"{md(' '.join(str(entry.get('motivo', '')).split()))} ({n} hallazgos no bloquean)")
        lines.append("")
        console.append(f"POLÍTICAS: {len(err)} bloqueantes, {len(warn)} avisos, {len(oos)} fuera de alcance")
        for x in err:
            console.append(f"  [ERROR] {x['rule']} {x['path']}:{x['line']} {x['message']}")

    # SARIF
    sarifs = sorted(glob.glob(os.path.join(args.reports, "**", "*.sarif"), recursive=True))
    tools = []
    for path in sarifs:
        tools.extend(load_sarif(path))

    if tools:
        lines += ["## Hallazgos por herramienta", "",
                  "| Herramienta | Reporte | " + " | ".join(f"{ICON[s]} {s}" for s in ORDER) + " | Total |",
                  "|---|---|" + "---|" * (len(ORDER) + 1)]
        for tool, cat, items in tools:
            counts = {s: sum(1 for i in items if i["sev"] == s) for s in ORDER}
            lines.append(f"| {tool} | `{cat}` | " + " | ".join(str(counts[s]) for s in ORDER) + f" | **{len(items)}** |")
        lines.append("")

        for tool, cat, items in tools:
            if not items:
                continue
            items.sort(key=lambda i: (ORDER.index(i["sev"]), i["rule"], i["loc"]))
            shown = items[: args.max_rows]
            lines += [f"<details><summary><b>{tool}</b> · <code>{cat}</code> · {len(items)} hallazgo(s)</summary>", "",
                      "| Severidad | Regla | Ubicación | Detalle |", "|---|---|---|---|"]
            for i in shown:
                note = " _(fuera de alcance)_" if i["out_of_scope"] else ""
                lines.append(f"| {ICON[i['sev']]} {i['sev']} | `{md(i['rule'])}` | `{md(i['loc'])}` | {md(i['msg'])}{note} |")
            if len(items) > len(shown):
                lines.append(f"\n…y {len(items) - len(shown)} más. Lista completa en **Security → Code scanning**.")
            lines += ["", "</details>", ""]

            console.append(f"\n=== {tool} ({cat}): {len(items)} hallazgo(s) ===")
            for i in shown:
                console.append(f"  [{i['sev']:8}] {i['rule']}  {i['loc']}  {i['msg']}")
            if len(items) > len(shown):
                console.append(f"  ... y {len(items) - len(shown)} más")
    else:
        lines.append("_No se encontraron reportes SARIF._")

    lines += ["", "---",
              "Los resultados completos también están en **Security → Code scanning** "
              "(filtra por la rama o el PR). Mapa de controles: `docs/pipeline-seguridad.md`."]

    print("\n".join(console))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > 900_000:        # el resumen admite hasta 1 MiB por paso
        text = text.encode("utf-8")[:900_000].decode("utf-8", "ignore") + "\n\n_(resumen recortado)_\n"
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print("\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
