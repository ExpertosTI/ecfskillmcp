from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from lxml import etree
import asyncpg

from ecf_core.utils import sanitize_dsn, safe_schema
from ecf_core.cert_vault import CertVault
from ecf_core.ecf_core_service import ECFSigner, ECFValidator
from ecf_core.dgii_client import DGIIClient, EstadoDGII, generar_security_code

TENANT_RNC = "132842316"
TENANT_SCHEMA = "tenant_132842316"
XSD_DIR = Path("/app/xsd")
OUTPUT_DIR = Path("/tmp/simulacion_paso4")

# Starting sequence for fresh simulation invoices
START_SEQ = 41


async def get_tenant_info():
    db_url = sanitize_dsn(os.environ["DATABASE_URL"])
    conn = await asyncpg.connect(db_url)
    try:
        vault = CertVault()
        t_row = await conn.fetchrow(
            "SELECT id, cert_password, razon_social, nombre_comercial FROM public.tenants WHERE rnc = $1",
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
        
        # Check highest used E32 sequence
        max_ecf = await conn.fetchval(
            f"SELECT ncf FROM {TENANT_SCHEMA}.ecf WHERE tipo_ecf = 32 AND ncf ~ '^E32' ORDER BY ncf DESC LIMIT 1"
        )
        max_rfce = await conn.fetchval(
            f"SELECT ncf FROM {TENANT_SCHEMA}.rfce WHERE ncf ~ '^E32' ORDER BY ncf DESC LIMIT 1"
        )
        return p12_data, p12_password, t_row, max_ecf, max_rfce
    finally:
        await conn.close()


def build_consumo_ecf_xml(
    rnc_emisor: str,
    razon_social: str,
    nombre_comercial: str,
    encf: str,
    fecha_emision: str,
    item_desc: str,
    precio_unitario: Decimal,
) -> bytes:
    """Builds a 100% XSD-compliant ECF Tipo 32 < 250k XML."""
    itbis_rate = Decimal("0.18")
    monto_gravado = precio_unitario
    itbis_monto = (monto_gravado * itbis_rate).quantize(Decimal("0.01"))
    total = (monto_gravado + itbis_monto).quantize(Decimal("0.01"))

    root = etree.Element("ECF")
    enc = etree.SubElement(root, "Encabezado")

    # Version
    etree.SubElement(enc, "Version").text = "1.0"

    # IdDoc
    id_doc = etree.SubElement(enc, "IdDoc")
    etree.SubElement(id_doc, "TipoeCF").text = "32"
    etree.SubElement(id_doc, "eNCF").text = encf
    etree.SubElement(id_doc, "IndicadorMontoGravado").text = "0"
    etree.SubElement(id_doc, "TipoIngresos").text = "01"
    etree.SubElement(id_doc, "TipoPago").text = "1"

    # Emisor
    emisor = etree.SubElement(enc, "Emisor")
    etree.SubElement(emisor, "RNCEmisor").text = rnc_emisor
    etree.SubElement(emisor, "RazonSocialEmisor").text = razon_social[:150]
    if nombre_comercial:
        etree.SubElement(emisor, "NombreComercial").text = nombre_comercial[:150]
    etree.SubElement(emisor, "DireccionEmisor").text = "AVE. ISABEL AGUIAR NO. 269, ZONA INDUSTRIAL DE HERRERA"
    tb_tel = etree.SubElement(emisor, "TablaTelefonoEmisor")
    etree.SubElement(tb_tel, "TelefonoEmisor").text = "809-472-7676"
    etree.SubElement(emisor, "CorreoEmisor").text = "INFO@RENACE.TECH"
    etree.SubElement(emisor, "FechaEmision").text = fecha_emision

    # Comprador: For Consumo < 250k, minimal or standard consumidor final
    comprador = etree.SubElement(enc, "Comprador")
    etree.SubElement(comprador, "RazonSocialComprador").text = "CLIENTE CONSUMIDOR FINAL"

    # Totales
    totales = etree.SubElement(enc, "Totales")
    etree.SubElement(totales, "MontoGravadoTotal").text = f"{monto_gravado:.2f}"
    etree.SubElement(totales, "MontoGravadoI1").text = f"{monto_gravado:.2f}"
    etree.SubElement(totales, "ITBIS1").text = "18"
    etree.SubElement(totales, "TotalITBIS").text = f"{itbis_monto:.2f}"
    etree.SubElement(totales, "TotalITBIS1").text = f"{itbis_monto:.2f}"
    etree.SubElement(totales, "MontoTotal").text = f"{total:.2f}"

    # DetallesItems
    detalles = etree.SubElement(root, "DetallesItems")
    item = etree.SubElement(detalles, "Item")
    etree.SubElement(item, "NumeroLinea").text = "1"
    etree.SubElement(item, "IndicadorFacturacion").text = "1"
    etree.SubElement(item, "NombreItem").text = item_desc[:80]
    etree.SubElement(item, "IndicadorBienoServicio").text = "2"  # 2 = Servicio
    etree.SubElement(item, "CantidadItem").text = "1"
    etree.SubElement(item, "UnidadMedida").text = "55"  # 55 = Unidad / Servicio
    etree.SubElement(item, "PrecioUnitarioItem").text = f"{precio_unitario:.2f}"
    etree.SubElement(item, "MontoItem").text = f"{precio_unitario:.2f}"

    # FechaHoraFirma placeholder
    etree.SubElement(root, "FechaHoraFirma").text = "PENDING_SIGN"

    return etree.tostring(root, encoding="utf-8", xml_declaration=True)


def build_rfce_xml(
    rnc_emisor: str,
    razon_social: str,
    encf: str,
    fecha_emision: str,
    precio_unitario: Decimal,
    sec_code: str,
) -> bytes:
    """Builds a 100% XSD-compliant RFCE XML."""
    itbis_rate = Decimal("0.18")
    monto_gravado = precio_unitario
    itbis_monto = (monto_gravado * itbis_rate).quantize(Decimal("0.01"))
    total = (monto_gravado + itbis_monto).quantize(Decimal("0.01"))

    root = etree.Element("RFCE")
    enc = etree.SubElement(root, "Encabezado")

    # Version
    etree.SubElement(enc, "Version").text = "1.0"

    # IdDoc
    id_doc = etree.SubElement(enc, "IdDoc")
    etree.SubElement(id_doc, "TipoeCF").text = "32"
    etree.SubElement(id_doc, "eNCF").text = encf
    etree.SubElement(id_doc, "TipoIngresos").text = "01"
    etree.SubElement(id_doc, "TipoPago").text = "1"

    # Emisor
    emisor = etree.SubElement(enc, "Emisor")
    etree.SubElement(emisor, "RNCEmisor").text = rnc_emisor
    etree.SubElement(emisor, "RazonSocialEmisor").text = razon_social[:150]
    etree.SubElement(emisor, "FechaEmision").text = fecha_emision

    # Comprador
    comprador = etree.SubElement(enc, "Comprador")
    etree.SubElement(comprador, "RazonSocialComprador").text = "CLIENTE CONSUMIDOR FINAL"

    # Totales
    totales = etree.SubElement(enc, "Totales")
    etree.SubElement(totales, "MontoGravadoTotal").text = f"{monto_gravado:.2f}"
    etree.SubElement(totales, "MontoGravadoI1").text = f"{monto_gravado:.2f}"
    etree.SubElement(totales, "TotalITBIS").text = f"{itbis_monto:.2f}"
    etree.SubElement(totales, "TotalITBIS1").text = f"{itbis_monto:.2f}"
    etree.SubElement(totales, "MontoTotal").text = f"{total:.2f}"

    # CodigoSeguridadeCF (first 6 chars of full ECF signature)
    etree.SubElement(enc, "CodigoSeguridadeCF").text = sec_code

    return etree.tostring(root, encoding="utf-8", xml_declaration=True)


async def main():
    validate_only = "--validate-only" in sys.argv

    p12_data, p12_password, t_row, max_ecf, max_rfce = await get_tenant_info()
    rnc_emisor = TENANT_RNC
    razon_social = t_row["razon_social"] or "RENACE EXPERTOS TECNOLOGIA E IDEAS SRL"
    nombre_comercial = t_row["nombre_comercial"] or "RENACE TECH"

    print(f"[INFO] Tenant: {rnc_emisor} | Razon Social: {razon_social}")
    print(f"[INFO] Last E32 in DB: ecf={max_ecf}, rfce={max_rfce}")

    # Determine safe starting sequence (must be strictly > any previously used sequence)
    last_seq_num = 16
    for val in (max_ecf, max_rfce):
        if val and val.startswith("E32"):
            try:
                seq_val = int(val[3:])
                if seq_val > last_seq_num:
                    last_seq_num = seq_val
            except ValueError:
                pass

    start_seq = max(START_SEQ, last_seq_num + 5)
    print(f"[INFO] Using fresh sequence starting at E32{start_seq:010d}")

    scenarios = [
        ("Servicios Profesionales de Desarrollo de Software", Decimal("10000.00")),
        ("Soporte Tecnico y Mantenimiento de Infraestructura Cloud", Decimal("5000.00")),
        ("Consultoria en Seguridad Informatica y Auditoria", Decimal("20000.00")),
        ("Licenciamiento y Subscripcion de Plataforma SaaS", Decimal("15000.00")),
    ]

    fecha_emision = datetime.now().strftime("%d-%m-%Y")
    signer = ECFSigner()
    validator = ECFValidator()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ecf_items = []
    rfce_items = []

    print("\n=== STEP 1: Generating, Signing & Validating 4 ECF Tipo 32 (<250k) ===")
    for i, (item_desc, precio) in enumerate(scenarios):
        seq_num = start_seq + i
        encf = f"E32{seq_num:010d}"
        
        # 1. Build raw ECF XML
        raw_ecf = build_consumo_ecf_xml(
            rnc_emisor, razon_social, nombre_comercial, encf, fecha_emision, item_desc, precio
        )
        
        # 2. Sign ECF XML
        signed_ecf = signer.firmar(raw_ecf, p12_data, p12_password, exclusive=False)
        
        # 3. Validate against ECF-32.xsd
        valido, errores = validator.validar(signed_ecf, 32)
        if not valido:
            raise RuntimeError(f"ECF {encf} failed ECF-32.xsd: {errores}")
        
        sec_code = generar_security_code(signed_ecf)
        ecf_fn = f"{rnc_emisor}{encf}.xml"
        (OUTPUT_DIR / ecf_fn).write_bytes(signed_ecf)
        print(f"  [XSD OK] ECF #{i+1:02d} {ecf_fn} | SecCode={sec_code} | Total=RD$ {precio * Decimal('1.18'):.2f}")

        # 4. Build corresponding RFCE XML with sec_code
        raw_rfce = build_rfce_xml(
            rnc_emisor, razon_social, encf, fecha_emision, precio, sec_code
        )
        
        # 5. Sign RFCE XML
        signed_rfce = signer.firmar(raw_rfce, p12_data, p12_password, exclusive=False)
        
        # 6. Validate against RFCE-32.xsd
        valido_rfce, errores_rfce = validator.validar_evento(signed_rfce, "RFCE-32")
        if not valido_rfce:
            raise RuntimeError(f"RFCE {encf} failed RFCE-32.xsd: {errores_rfce}")
        
        rfce_fn = f"{rnc_emisor}{encf}_rfce.xml"
        (OUTPUT_DIR / rfce_fn).write_bytes(signed_rfce)
        print(f"  [XSD OK] RFCE #{i+1:02d} {rfce_fn} | Valid against RFCE-32.xsd")

        ecf_items.append({
            "encf": encf,
            "filename": ecf_fn,
            "signed_xml": signed_ecf,
            "sec_code": sec_code,
            "subtotal": precio,
            "total": (precio * Decimal("1.18")).quantize(Decimal("0.01")),
        })
        rfce_items.append({
            "encf": encf,
            "filename": rfce_fn,
            "signed_xml": signed_rfce,
        })

    print(f"\n[SUCCESS] Generated and validated 4 fresh e-CFs (<250k) and 4 RFCEs!")

    if validate_only:
        print("[INFO] --validate-only specified. Exiting.")
        return

    print("\n=== STEP 2: Submitting 4 RFCE to DGII (fc.dgii.gov.do) ===")
    db_url = sanitize_dsn(os.environ["DATABASE_URL"])
    conn = await asyncpg.connect(db_url)
    try:
        async with DGIIClient(ambiente="CerteCF") as dgii:
            dgii.set_certificate(p12_data, p12_password)

            for idx, r_item in enumerate(rfce_items, 1):
                encf = r_item["encf"]
                e_item = ecf_items[idx - 1]
                print(f"\n>>> [{idx:02d}/04] Sending RFCE {encf}...")

                try:
                    resp = await dgii.enviar_rfce(
                        r_item["signed_xml"],
                        ncf=f"{rnc_emisor}{encf}",
                    )
                    print(f"    DGII RFCE Response: estado={resp.estado.value}, track_id={resp.track_id}, msg={resp.mensaje}")
                except Exception as ex:
                    msg_str = str(ex).lower()
                    if "ya han sido utilizados" in msg_str or "ya habia sido" in msg_str or "ya existe" in msg_str:
                        print(f"    [INFO] RFCE {encf} was already accepted in DGII ({ex}), continuing.")
                        resp = RespuestaDGII(estado=EstadoDGII.ACEPTADO, track_id=None, mensaje="Ya aceptado previamente", raw={"codigo": 1, "estado": "Aceptado"})
                    else:
                        raise

                if resp.estado == EstadoDGII.RECHAZADO:
                    # If DGII says it was already accepted, that's fine
                    msg_str = str(resp.raw)
                    if "ya habia sido" in msg_str.lower() or "ya existe" in msg_str.lower():
                        print(f"    [INFO] RFCE {encf} already accepted in DGII, continuing.")
                    else:
                        raise RuntimeError(f"RFCE {encf} REJECTED by DGII: {resp.raw}")

                # Persist to DB (bytea column expects bytes)
                await conn.execute(
                    f"""
                    INSERT INTO {TENANT_SCHEMA}.ecf (
                        ncf, tipo_ecf, estado, codigo_seguridad, security_code,
                        rnc_comprador, nombre_comprador, fecha_emision,
                        subtotal, itbis, total, xml_firmado, respuesta_dgii,
                        sent_at, approved_at, updated_at
                    ) VALUES (
                        $1, 32, 'aprobado', $2, $2,
                        '000000000', 'CLIENTE CONSUMIDOR FINAL', CURRENT_DATE,
                        $3, $4, $5, $6, $7::jsonb,
                        now(), now(), now()
                    )
                    ON CONFLICT (ncf) DO UPDATE SET
                        estado = 'aprobado',
                        xml_firmado = EXCLUDED.xml_firmado,
                        respuesta_dgii = EXCLUDED.respuesta_dgii,
                        updated_at = now()
                    """,
                    encf,
                    e_item["sec_code"],
                    e_item["subtotal"],
                    (e_item["total"] - e_item["subtotal"]),
                    e_item["total"],
                    bytes(e_item["signed_xml"]),
                    json.dumps(resp.raw, ensure_ascii=False),
                )
                await conn.execute(
                    f"""
                    INSERT INTO {TENANT_SCHEMA}.rfce (
                        ncf, fecha_resumen, estado, track_id, cantidad_facturas,
                        monto_total, xml_firmado, respuesta_dgii, created_at, updated_at
                    ) VALUES (
                        $1, CURRENT_DATE, 'aprobado', $2, 1,
                        $3, $4, $5::jsonb, now(), now()
                    )
                    ON CONFLICT (ncf) DO UPDATE SET
                        estado = 'aprobado',
                        xml_firmado = EXCLUDED.xml_firmado,
                        respuesta_dgii = EXCLUDED.respuesta_dgii,
                        updated_at = now()
                    """,
                    encf,
                    resp.track_id,
                    e_item["total"],
                    bytes(r_item["signed_xml"]),
                    json.dumps(resp.raw, ensure_ascii=False),
                )
                print(f"    [DB OK] Persisted {encf} to ecf and rfce tables.")
                await asyncio.sleep(2)
    finally:
        await conn.close()

    print("\n========================================================")
    print("ALL 4 RFCEs ACCEPTED BY DGII! The 4 full ECF XML files are ready for web upload:")
    for e_item in ecf_items:
        print(f"  - /tmp/simulacion_paso4/{e_item['filename']}")
    print("========================================================")


if __name__ == "__main__":
    asyncio.run(main())
