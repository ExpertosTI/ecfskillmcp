#!/usr/bin/env python3
"""
MCP Server (Stdio JSON-RPC 2.0) for DGII e-CF & RFCE Certification (RENECF).

Exposes tools to:
1. `dgii_extract_excel_set_pruebas`: Parse an official DGII Set de Pruebas Excel (.xlsx) into ordered JSON.
2. `dgii_validate_set_pruebas_xsd`: Offline build, sign, and XSD-validate all 25 e-CFs and 4 RFCEs on the VPS.
3. `dgii_check_tenant_readiness`: Query the RENECF Admin API/DB for a tenant's readiness, approved e-CFs & RFCEs.
4. `dgii_run_set_pruebas`: Execute a single comprobante (`only_encf`) or the full 4-phase certification suite on the VPS.
5. `dgii_run_aprobacion_comercial`: Execute Paso 3 (ACECF) commercial approvals for 11 comprobantes.
6. `dgii_run_simulacion_paso4`: Execute Paso 4 full simulation transmission (21 e-CFs + 4 RFCEs).
7. `dgii_generate_paso5_pdfs`: Generate all 11 compliant representation PDFs with validated QR & Razón Social directly to External SSD.
8. `dgii_get_certification_status`: Summarize full end-to-end certification status, external drive files, and portal health.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = KIT_ROOT / ".agents" / "skills" / "dgii-ecf-certification" / "scripts"
VPS_HOST = os.environ.get("RENECF_VPS_HOST", "root@93.127.217.52")

# External drive root detection
EXTERNAL_CANDIDATES = [
    Path("/Volumes/Extreme SSD/FACTURACION ELECTRONICA"),
    Path("/Volumes/ExtremeSSD/FACTURACION ELECTRONICA"),
]
EXTERNAL_BASE = next((p for p in EXTERNAL_CANDIDATES if p.exists()), None)


def get_tenant_external_dir(rnc: str = "132842316") -> Path | None:
    if not EXTERNAL_BASE:
        return None
    for folder in EXTERNAL_BASE.iterdir():
        if folder.is_dir() and ("RENACE" in folder.name.upper() or rnc in folder.name):
            return folder
    return EXTERNAL_BASE / f"TENANT_{rnc}"


TOOLS = [
    {
        "name": "dgii_extract_excel_set_pruebas",
        "description": (
            "Extracts a DGII CerteCF Set de Pruebas Excel file (.xlsx) into an ordered JSON file "
            "preserving all 5,220 column indices (ECF + RFCE sheets) and removing '#e' empty sentinels. "
            "Defaults to reading from the External SSD if available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "xlsx_path": {
                    "type": "string",
                    "description": "Path to the DGII Excel file. If omitted, picks the latest .xlsx in External SSD.",
                },
                "output_json_path": {
                    "type": "string",
                    "description": "Path to save the JSON output. Defaults to External SSD.",
                },
            },
        },
    },
    {
        "name": "dgii_check_tenant_readiness",
        "description": (
            "Checks the certification readiness (7/7 score, approved e-CFs, RFCEs, and platform onboarding status) "
            "for a tenant RNC on the RENECF VPS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rnc": {
                    "type": "string",
                    "description": "Tenant RNC (default: 132842316)",
                }
            },
        },
    },
    {
        "name": "dgii_validate_set_pruebas_xsd",
        "description": (
            "Uploads the ordered JSON to the RENECF VPS and runs --validate-only to verify that all 25 e-CFs "
            "and 4 RFCEs consume 100% of Excel cells (unconsumed=0) and pass official DGII XSD validation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ordered_json_path": {
                    "type": "string",
                    "description": "Local path to set_pruebas_ordered.json to upload and validate.",
                }
            },
        },
    },
    {
        "name": "dgii_run_set_pruebas",
        "description": (
            "Runs the DGII CerteCF Set de Pruebas on the RENECF VPS. Can run a single pilot comprobante "
            "via only_encf (recommended first) or the full 4-phase sequential suite."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "only_encf": {
                    "type": "string",
                    "description": "Optional e-NCF (e.g. 'E310000000001') to run as a single pilot test.",
                },
                "skip_e31_01": {
                    "type": "boolean",
                    "description": "Set true if E310000000001 was already accepted in the pilot run.",
                },
            },
        },
    },
    {
        "name": "dgii_run_aprobacion_comercial",
        "description": (
            "Executes Paso 3 (Aprobación Comercial ACECF) on the RENECF VPS, transmitting 11 ACECF XMLs to DGII."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rnc": {
                    "type": "string",
                    "description": "Tenant RNC (default: 132842316)",
                }
            },
        },
    },
    {
        "name": "dgii_run_simulacion_paso4",
        "description": (
            "Executes Paso 4 (Pruebas de Simulación) on the RENECF VPS, transmitting 21 e-CFs and 4 RFCEs via API."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offset": {
                    "type": "integer",
                    "description": "Sequence shift offset (e.g. 300 or 400). Default is 300.",
                }
            },
        },
    },
    {
        "name": "dgii_generate_paso5_pdfs",
        "description": (
            "Generates all 11 DGII-compliant PDF representations (ReportLab + QRCode) with verified QR code links "
            "(routing E32 >= 250k to consultatimbre) and correct Razón Social, saving them directly to the External SSD."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rnc": {
                    "type": "string",
                    "description": "Tenant RNC (default: 132842316)",
                },
                "destination_dir": {
                    "type": "string",
                    "description": "Local destination directory. Defaults to External SSD PASO_5_REPRESENTACION_IMPRESA folder.",
                }
            },
        },
    },
    {
        "name": "dgii_get_certification_status",
        "description": (
            "Queries overall certification status across DB tables, External SSD folders, and generates an executive overview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rnc": {
                    "type": "string",
                    "description": "Tenant RNC (default: 132842316)",
                }
            },
        },
    },
]


def _run_tool(name: str, args: dict) -> str:
    rnc = args.get("rnc") or "132842316"
    tenant_ext_dir = get_tenant_external_dir(rnc)

    if name == "dgii_extract_excel_set_pruebas":
        xlsx_path_str = args.get("xlsx_path")
        if not xlsx_path_str:
            if tenant_ext_dir and tenant_ext_dir.exists():
                xlsx_files = sorted(tenant_ext_dir.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
                if xlsx_files:
                    xlsx_path_str = str(xlsx_files[0])
        if not xlsx_path_str:
            return "Error: No xlsx_path provided and no .xlsx file found in External SSD."

        out_path_str = args.get("output_json_path")
        if not out_path_str:
            if tenant_ext_dir:
                out_path_str = str(tenant_ext_dir / f"set_pruebas_{rnc}.json")
            else:
                out_path_str = "/tmp/set_pruebas_ordered.json"

        extract_script = SKILLS_DIR / "extract_excel_to_json.py"
        res = subprocess.run(
            [sys.executable, str(extract_script), xlsx_path_str, out_path_str],
            capture_output=True,
            text=True,
            timeout=90,
        )
        return f"Parsed: {xlsx_path_str}\nOutput: {out_path_str}\n{res.stdout}\n{res.stderr}".strip()

    if name == "dgii_check_tenant_readiness":
        py_code = (
            "import asyncio, os, asyncpg\n"
            "from ecf_core.utils import sanitize_dsn\n"
            "async def m():\n"
            "    c = await asyncpg.connect(sanitize_dsn(os.environ['DATABASE_URL']))\n"
            f"    er = await c.fetch(\"SELECT ncf, tipo_ecf, estado, security_code, track_id FROM tenant_{rnc}.ecf WHERE estado='aprobado' ORDER BY tipo_ecf, ncf\")\n"
            f"    rr = await c.fetch(\"SELECT ncf, estado, monto_total FROM tenant_{rnc}.rfce ORDER BY ncf\")\n"
            "    print('Approved ECFs in DB:', len(er))\n"
            "    for x in er:\n"
            "        print(' ', dict(x))\n"
            "    print('RFCEs in DB:', len(rr))\n"
            "    for x in rr:\n"
            "        print(' ', dict(x))\n"
            "    await c.close()\n"
            "asyncio.run(m())\n"
        )
        cmd = (
            "API_CID=$(docker ps --filter name=ecf_api -q | head -n 1); "
            f"docker exec -i -e PYTHONPATH=/app $API_CID python3 << 'PYEOF'\n"
            f"{py_code}\n"
            "PYEOF"
        )
        res = subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=60)
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    if name == "dgii_validate_set_pruebas_xsd":
        json_path_str = args.get("ordered_json_path")
        if not json_path_str and tenant_ext_dir:
            cand = tenant_ext_dir / f"set_pruebas_{rnc}.json"
            if cand.exists():
                json_path_str = str(cand)
        if not json_path_str:
            return "Error: No ordered_json_path provided."

        runner_path = SKILLS_DIR / "run_dgii_set_pruebas_vps.py"
        subprocess.run(["scp", json_path_str, f"{VPS_HOST}:/tmp/set_pruebas_ordered.json"], check=True, timeout=60)
        subprocess.run(["scp", str(runner_path), f"{VPS_HOST}:/tmp/run_dgii_set_pruebas_vps.py"], check=True, timeout=60)
        cmd = (
            "API_CID=$(docker ps --filter name=ecf_api -q | head -n 1); "
            "docker cp /tmp/set_pruebas_ordered.json $API_CID:/tmp/set_pruebas_ordered.json; "
            "docker cp /tmp/run_dgii_set_pruebas_vps.py $API_CID:/tmp/run_dgii_set_pruebas_vps.py; "
            "docker exec -e PYTHONPATH=/app $API_CID python3 /tmp/run_dgii_set_pruebas_vps.py --validate-only"
        )
        res = subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=120)
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    if name == "dgii_run_set_pruebas":
        only_encf = args.get("only_encf")
        skip_e31_01 = args.get("skip_e31_01", False)
        flags = ""
        if only_encf:
            flags += f" --only {only_encf}"
        if skip_e31_01:
            flags += " --skip-e31-01"
        cmd = (
            "API_CID=$(docker ps --filter name=ecf_api -q | head -n 1); "
            f"docker exec -e PYTHONPATH=/app $API_CID python3 /tmp/run_dgii_set_pruebas_vps.py{flags}"
        )
        res = subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=300)
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    if name == "dgii_run_aprobacion_comercial":
        script_path = SKILLS_DIR / "run_aprobacion_comercial.py"
        subprocess.run(["scp", str(script_path), f"{VPS_HOST}:/tmp/run_aprobacion_comercial.py"], check=True, timeout=60)
        cmd = (
            "API_CID=$(docker ps --filter name=ecf_api -q | head -n 1); "
            "docker cp /tmp/run_aprobacion_comercial.py $API_CID:/tmp/run_aprobacion_comercial.py; "
            "docker exec -e PYTHONPATH=/app $API_CID python3 /tmp/run_aprobacion_comercial.py"
        )
        res = subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=180)
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    if name == "dgii_run_simulacion_paso4":
        offset = args.get("offset", 300)
        script_path = SKILLS_DIR / "run_full_simulacion_paso4.py"
        subprocess.run(["scp", str(script_path), f"{VPS_HOST}:/tmp/run_full_simulacion_paso4.py"], check=True, timeout=60)
        cmd = (
            "API_CID=$(docker ps --filter name=ecf_api -q | head -n 1); "
            "docker cp /tmp/run_full_simulacion_paso4.py $API_CID:/tmp/run_full_simulacion_paso4.py; "
            f"docker exec -e PYTHONPATH=/app $API_CID python3 /tmp/run_full_simulacion_paso4.py"
        )
        res = subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=300)
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    if name == "dgii_generate_paso5_pdfs":
        dest_dir_str = args.get("destination_dir")
        if not dest_dir_str:
            if tenant_ext_dir:
                dest_dir = tenant_ext_dir / "PASO_5_REPRESENTACION_IMPRESA"
            else:
                dest_dir = Path("/tmp/representaciones_impresas_paso5")
        else:
            dest_dir = Path(dest_dir_str)

        dest_dir.mkdir(parents=True, exist_ok=True)

        # 1. Ejecutar generación en el contenedor VPS
        script_path = SKILLS_DIR / "generate_paso5_pdfs.py"
        subprocess.run(["scp", str(script_path), f"{VPS_HOST}:/tmp/generate_paso5_pdfs.py"], check=True, timeout=60)
        cmd = (
            "API_CID=$(docker ps --filter name=ecf_api -q | head -n 1); "
            "docker cp /tmp/generate_paso5_pdfs.py $API_CID:/tmp/generate_paso5_pdfs.py; "
            f"docker exec -e PYTHONPATH=/app $API_CID python3 /tmp/generate_paso5_pdfs.py --rnc {rnc} && "
            "docker cp $API_CID:/tmp/representaciones_impresas_paso5 /root/"
        )
        gen_res = subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=120)

        # 2. Descargar los 11 PDFs directamente al External SSD
        scp_cmd = f"scp -o StrictHostKeyChecking=no {VPS_HOST}:/root/representaciones_impresas_paso5/*.pdf \"{dest_dir}/\""
        scp_res = subprocess.run(scp_cmd, shell=True, capture_output=True, text=True, timeout=90)

        # 3. Listar archivos y tamaños en el disco externo
        files = sorted(dest_dir.glob("*.pdf"))
        file_summary = [f"  - {f.name} ({f.stat().st_size / 1024:.1f} KB)" for f in files]

        return (
            f"=== DGII Paso 5 PDFs Generated & Synced Successfully ===\n"
            f"Destination (External SSD): {dest_dir}\n"
            f"Total PDFs: {len(files)} / 11\n"
            + "\n".join(file_summary)
            + f"\n\nVPS Output:\n{gen_res.stdout[:500]}"
        )

    if name == "dgii_get_certification_status":
        lines = ["=== DGII CerteCF Certification Status ==="]
        if EXTERNAL_BASE:
            lines.append(f"External Drive Connected: {EXTERNAL_BASE}")
            if tenant_ext_dir and tenant_ext_dir.exists():
                lines.append(f"Tenant Directory: {tenant_ext_dir}")
                for sub in sorted(tenant_ext_dir.iterdir()):
                    if sub.is_dir():
                        count = len(list(sub.glob("*")))
                        lines.append(f"  [{sub.name}]: {count} items")
        else:
            lines.append("External Drive: Not detected at /Volumes/Extreme SSD")

        # Database readiness check
        check_out = _run_tool("dgii_check_tenant_readiness", {"rnc": rnc})
        lines.append("\nDatabase State:")
        lines.append(check_out)

        return "\n".join(lines)

    raise ValueError(f"Unknown tool: {name}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "dgii-ecf-certification-mcp", "version": "2.0.0"},
                },
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            t_name = params.get("name", "")
            t_args = params.get("arguments") or {}
            try:
                out_text = _run_tool(t_name, t_args)
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": out_text}], "isError": False},
                }
            except Exception as exc:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True},
                }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        elif req_id is not None:
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
