from __future__ import annotations
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from lxml import etree
import openpyxl
import httpx
import asyncpg

from ecf_core.utils import sanitize_dsn
from ecf_core.cert_vault import CertVault
from ecf_core.ecf_core_service import ECFSigner
from ecf_core.dgii_client import DGIIClient

TENANT_RNC = "132842316"
EXCEL_PATH = Path("/tmp/132842316-19102026085633.xlsx")
XSD_PATH = Path("/app/xsd/ACECF.xsd")
OUTPUT_DIR = Path("/tmp/xml_aprobacion_comercial")


async def get_tenant_p12() -> tuple[bytes, bytes]:
    db_url = sanitize_dsn(os.environ["DATABASE_URL"])
    conn = await asyncpg.connect(db_url)
    try:
        vault = CertVault()
        t_row = await conn.fetchrow(
            "SELECT id, cert_password FROM public.tenants WHERE rnc = $1",
            TENANT_RNC,
        )
        c_row = await conn.fetchrow(
            """
            SELECT cert_data, iv, tag
            FROM public.tenant_certs
            WHERE tenant_id = $1 AND activo = TRUE
            ORDER BY created_at DESC
            LIMIT 1
            """,
            t_row["id"],
        )
        p12_data = vault.descifrar(bytes(c_row["cert_data"]), bytes(c_row["iv"]), bytes(c_row["tag"]))
        p12_password = vault.descifrar_campo(t_row["cert_password"]).encode("utf-8")
        return p12_data, p12_password
    finally:
        await conn.close()


def build_acecf_xml(row: dict) -> bytes:
    root = etree.Element("ACECF")
    det = etree.SubElement(root, "DetalleAprobacionComercial")

    v_str = str(row["Version"]).strip()
    if v_str.endswith(".0"):
        v_str = "1.0"
    etree.SubElement(det, "Version").text = v_str

    rnc_em = str(int(row["RNCEmisor"]) if isinstance(row["RNCEmisor"], (int, float)) else row["RNCEmisor"]).strip()
    etree.SubElement(det, "RNCEmisor").text = rnc_em

    encf = str(row["eNCF"]).strip()
    etree.SubElement(det, "eNCF").text = encf

    f_em = str(row["FechaEmision"]).strip()
    etree.SubElement(det, "FechaEmision").text = f_em

    monto = f"{Decimal(str(row['MontoTotal'])):.2f}"
    etree.SubElement(det, "MontoTotal").text = monto

    rnc_comp = str(int(row["RNCComprador"]) if isinstance(row["RNCComprador"], (int, float)) else row["RNCComprador"]).strip()
    etree.SubElement(det, "RNCComprador").text = rnc_comp

    st = str(int(row["Estado"]) if isinstance(row["Estado"], (int, float)) else row["Estado"]).strip()
    etree.SubElement(det, "Estado").text = st

    rechazo = row.get("DetalleMotivoRechazo")
    if rechazo and str(rechazo).strip() not in ("None", ""):
        etree.SubElement(det, "DetalleMotivoRechazo").text = str(rechazo).strip()

    fh_ac = str(row["FechaHoraAprobacionComercial"]).strip()
    etree.SubElement(det, "FechaHoraAprobacionComercial").text = fh_ac

    return etree.tostring(root, encoding="utf-8", xml_declaration=True)


async def main():
    validate_only = "--validate-only" in sys.argv

    if not EXCEL_PATH.exists():
        raise FileNotFoundError(f"Excel file not found at {EXCEL_PATH}")

    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb["ACEECF_Generadas"]
    headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

    xsd_schema = etree.XMLSchema(etree.parse(str(XSD_PATH)))
    p12_data, p12_password = await get_tenant_p12()
    signer = ECFSigner()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    items = []
    print("=== STEP 1: Building, Signing & Validating ACECF XMLs ===")
    for r in range(2, ws.max_row + 1):
        row_dict = {headers[c - 1]: ws.cell(r, c).value for c in range(1, ws.max_column + 1)}
        encf = str(row_dict["eNCF"]).strip()
        rnc_comp = str(int(row_dict["RNCComprador"]) if isinstance(row_dict["RNCComprador"], (int, float)) else row_dict["RNCComprador"]).strip()

        raw_xml = build_acecf_xml(row_dict)
        raw_doc = etree.fromstring(raw_xml)
        if not xsd_schema.validate(raw_doc):
            raise RuntimeError(f"Raw ACECF XML failed XSD for {encf}: {xsd_schema.error_log}")

        signed_xml = signer.firmar(raw_xml, p12_data, p12_password, exclusive=False)
        signed_doc = etree.fromstring(signed_xml)
        if not xsd_schema.validate(signed_doc):
            raise RuntimeError(f"Signed ACECF XML failed XSD for {encf}: {xsd_schema.error_log}")

        filename = f"{rnc_comp}{encf}.xml"
        file_path = OUTPUT_DIR / filename
        file_path.write_bytes(signed_xml)
        print(f"  [XSD OK] #{r - 1:02d} {filename} (len={len(signed_xml)} bytes)")
        items.append({
            "encf": encf,
            "filename": filename,
            "xml_bytes": signed_xml,
            "row": row_dict,
        })

    print(f"\n[SUCCESS] All {len(items)} ACECF XMLs generated, signed, and validated against ACECF.xsd!")

    if validate_only:
        print("[INFO] --validate-only specified. Exiting without transmitting to DGII.")
        return

    print("\n=== STEP 2: Authenticating and Submitting to DGII CerteCF ===")
    async with DGIIClient(ambiente="CerteCF") as dgii:
        dgii.set_certificate(p12_data, p12_password)
        await dgii._authenticate()
        token = dgii._access_token
        print(f"  [AUTH OK] Bearer token obtained: {token[:20]}...")

        url_ac = "https://ecf.dgii.gov.do/CerteCF/AprobacionComercial/api/AprobacionComercial"
        headers_http = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            for idx, item in enumerate(items, 1):
                fn = item["filename"]
                encf = item["encf"]
                print(f"\n>>> [{idx:02d}/{len(items):02d}] Sending {fn}...")

                files = {
                    "xml": (fn, item["xml_bytes"], "application/xml"),
                }

                resp = await client.post(url_ac, headers=headers_http, files=files)
                print(f"    HTTP Status: {resp.status_code}")
                try:
                    resp_json = resp.json()
                    print(f"    Response JSON: {resp_json}")
                except Exception:
                    print(f"    Response Text: {resp.text}")

                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"Failed to submit ACECF {fn}: HTTP {resp.status_code} - {resp.text}")

                # If token expires or is near expiry, refresh
                await asyncio.sleep(1)

    print("\n========================================================")
    print("ALL 11 APROBACIONES COMERCIALES TRANSMITTED SUCCESSFULLY!")
    print("========================================================")


if __name__ == "__main__":
    asyncio.run(main())
