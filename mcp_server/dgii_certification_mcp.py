#!/usr/bin/env python3
"""
MCP Server (Stdio JSON-RPC 2.0) for DGII e-CF & RFCE Certification (RENECF).

Exposes tools to:
1. `dgii_extract_excel_set_pruebas`: Parse an official DGII Set de Pruebas Excel (.xlsx) into ordered JSON.
2. `dgii_validate_set_pruebas_xsd`: Offline build, sign, and XSD-validate all 25 e-CFs and 4 RFCEs on the VPS.
3. `dgii_check_tenant_readiness`: Query the RENECF Admin API for a tenant's 7/7 certification readiness & approved counts.
4. `dgii_run_set_pruebas`: Execute a single comprobante (`only_encf`) or the full 4-phase certification run on the VPS.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
EXTRACT_SCRIPT = KIT_ROOT / ".agents" / "skills" / "dgii-ecf-certification" / "scripts" / "extract_excel_to_json.py"
VPS_HOST = os.environ.get("RENECF_VPS_HOST", "root@93.127.217.52")


TOOLS = [
    {
        "name": "dgii_extract_excel_set_pruebas",
        "description": (
            "Extracts a DGII CerteCF Set de Pruebas Excel file (.xlsx) into an ordered JSON file "
            "preserving all 5,220 column indices (ECF + RFCE sheets) and removing '#e' empty sentinels."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "xlsx_path": {
                    "type": "string",
                    "description": "Absolute path to the DGII Excel file (e.g., /Users/brainiacx/Downloads/132842316-16102026193033.xlsx)",
                },
                "output_json_path": {
                    "type": "string",
                    "description": "Absolute path where the extracted ordered JSON should be saved (default: /tmp/set_pruebas_ordered.json)",
                },
            },
            "required": ["xlsx_path"],
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
            "required": ["ordered_json_path"],
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
]


def _run_tool(name: str, args: dict) -> str:
    if name == "dgii_extract_excel_set_pruebas":
        xlsx_path = Path(args["xlsx_path"])
        out_path = Path(args.get("output_json_path") or "/tmp/set_pruebas_ordered.json")
        res = subprocess.run(
            [sys.executable, str(EXTRACT_SCRIPT), str(xlsx_path), str(out_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    if name == "dgii_check_tenant_readiness":
        rnc = args.get("rnc") or "132842316"
        cmd = (
            f"API_CID=$(docker ps --filter name=ecf_api -q | head -n 1); "
            f"docker exec -e PYTHONPATH=/app $API_CID python3 -c \""
            f"import asyncio, os, asyncpg; from ecf_core.utils import sanitize_dsn; "
            f"async def m(): "
            f"  c = await asyncpg.connect(sanitize_dsn(os.environ['DATABASE_URL'])); "
            f"  er = await c.fetch('SELECT ncf, tipo_ecf, estado, security_code, track_id FROM tenant_{rnc}.ecf WHERE estado=\\'aprobado\\' ORDER BY tipo_ecf, ncf'); "
            f"  rr = await c.fetch('SELECT ncf, estado, monto_total FROM tenant_{rnc}.rfce ORDER BY ncf'); "
            f"  print('Approved ECFs:', len(er)); "
            f"  for x in er: print(' ', dict(x)); "
            f"  print('RFCEs:', len(rr)); "
            f"  for x in rr: print(' ', dict(x)); "
            f"  await c.close(); "
            f"asyncio.run(m())\""
        )
        res = subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=60)
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    if name == "dgii_validate_set_pruebas_xsd":
        json_path = Path(args["ordered_json_path"])
        runner_path = KIT_ROOT / ".agents" / "skills" / "dgii-ecf-certification" / "scripts" / "run_dgii_set_pruebas_vps.py"
        subprocess.run(["scp", str(json_path), f"{VPS_HOST}:/tmp/set_pruebas_ordered.json"], check=True, timeout=60)
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
                    "serverInfo": {"name": "dgii-ecf-certification-mcp", "version": "1.0.0"},
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
