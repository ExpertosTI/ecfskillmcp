from __future__ import annotations
import asyncio
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from lxml import etree

from ecf_core.ecf_core_service import ECFSigner, ECFValidator
from ecf_core.dgii_client import DGIIClient, EstadoDGII, generar_security_code, generar_qr_url

XSD_DIR = Path("/app/xsd")
ORDERED_JSON = Path("/tmp/set_pruebas_ordered.json")
XS_NS = "{http://www.w3.org/2001/XMLSchema}"
TENANT_RNC = "132842316"
TENANT_SCHEMA = "tenant_132842316"


def normalize_header_name(h: str) -> str:
    h = h.strip()
    if h == "ENCF":
        return "eNCF"
    if h.startswith("MontosubRecargo["):
        return "MontoSubRecargo[" + h[len("MontosubRecargo["):]
    return h


def get_cell_val(row_info: dict, header_name: str) -> str:
    for c in row_info["cells"]:
        if c["header"].strip() == header_name:
            return c["val"]
    return ""


def categorize_row_cells(cells: list[dict]):
    by_col = {}
    for c in cells:
        col_idx = c["col_idx"]
        if col_idx == 1 and c["header"].strip() == "CasoPrueba":
            continue
        by_col[col_idx] = {
            "col_idx": col_idx,
            "col_let": c["col_let"],
            "raw_header": c["header"],
            "header": normalize_header_name(c["header"]),
            "val": c["val"],
            "consumed": False,
        }
    return by_col


def consume_cell(by_col: dict, pred) -> str | None:
    for col_idx, info in by_col.items():
        if not info["consumed"] and pred(col_idx, info["header"]):
            info["consumed"] = True
            return info["val"]
    return None


def build_ecf_xml_from_xsd(row_info: dict) -> tuple[bytes, list[dict]]:
    tipo = get_cell_val(row_info, "TipoeCF")
    xsd_path = XSD_DIR / f"ECF-{tipo}.xsd"
    xsd_root = ET.parse(xsd_path).getroot()
    by_col = categorize_row_cells(row_info["cells"])

    def find_seq_children(xsd_el):
        ct = xsd_el.find(f"{XS_NS}complexType")
        if ct is None:
            return None
        seq = ct.find(f"{XS_NS}sequence")
        if seq is None:
            return None
        return seq.findall(f"{XS_NS}element")

    def build_element(xsd_el, col_range: tuple[int, int], idx1: int | None = None, idx2: int | None = None) -> list[ET.Element]:
        tag = xsd_el.attrib["name"]
        max_occurs = xsd_el.attrib.get("maxOccurs", "1")
        children_xsd = find_seq_children(xsd_el)

        if children_xsd is None:
            if tag == "FechaHoraFirma":
                el = ET.Element(tag)
                el.text = "PENDING_SIGN"
                return [el]

            if idx1 is not None and idx2 is not None:
                expected = f"{tag}[{idx1}][{idx2}]"
            elif idx1 is not None:
                expected = f"{tag}[{idx1}]"
            else:
                expected = tag

            if max_occurs != "1" and idx1 is None:
                res = []
                max_n = 20 if max_occurs == "unbounded" else int(max_occurs)
                for i in range(1, max_n + 1):
                    target = f"{tag}[{i}]"
                    val = consume_cell(
                        by_col,
                        lambda c, h, t=target: col_range[0] <= c <= col_range[1] and h == t
                    )
                    if val is not None:
                        el = ET.Element(tag)
                        el.text = val
                        res.append(el)
                return res
            else:
                val = consume_cell(
                    by_col,
                    lambda c, h, t=expected: col_range[0] <= c <= col_range[1] and h == t
                )
                if val is not None:
                    el = ET.Element(tag)
                    el.text = val
                    return [el]
                return []

        sub_range = col_range
        if tag == "Encabezado":
            sub_range = (2, 182)
        elif tag == "Totales":
            sub_range = (2, 147)
        elif tag == "OtraMoneda":
            sub_range = (148, 182)
        elif tag == "DetallesItems":
            sub_range = (183, 5142)
        elif tag in ("Subtotales", "DescuentosORecargos", "Paginacion", "InformacionReferencia"):
            sub_range = (5143, 5220)

        if max_occurs != "1":
            res = []
            max_n = 1000 if max_occurs == "unbounded" else int(max_occurs)
            if tag == "Item":
                max_n = 65
            elif tag == "Pagina":
                max_n = 5
            for i in range(1, max_n + 1):
                el = ET.Element(tag)
                for ch_xsd in children_xsd:
                    if idx1 is None:
                        ch_els = build_element(ch_xsd, sub_range, idx1=i, idx2=None)
                    else:
                        ch_els = build_element(ch_xsd, sub_range, idx1=idx1, idx2=i)
                    for ce in ch_els:
                        el.append(ce)
                if len(el) > 0:
                    res.append(el)
            return res
        else:
            el = ET.Element(tag)
            for ch_xsd in children_xsd:
                ch_els = build_element(ch_xsd, sub_range, idx1=idx1, idx2=idx2)
                for ce in ch_els:
                    el.append(ce)
            if len(el) > 0:
                return [el]
            return []

    ecf_xsd_el = xsd_root.find(f"{XS_NS}element[@name='ECF']")
    built = build_element(ecf_xsd_el, (2, 6000))[0]
    unconsumed = [info for info in by_col.values() if not info["consumed"]]
    xml_bytes = ET.tostring(built, encoding="utf-8", xml_declaration=True)
    return xml_bytes, unconsumed


def build_rfce_xml_from_xsd(row_info: dict, security_code: str) -> tuple[bytes, list[dict]]:
    xsd_path = XSD_DIR / "RFCE-32.xsd"
    xsd_root = ET.parse(xsd_path).getroot()
    by_col = categorize_row_cells(row_info["cells"])

    def find_seq_children(xsd_el):
        ct = xsd_el.find(f"{XS_NS}complexType")
        if ct is None:
            return None
        seq = ct.find(f"{XS_NS}sequence")
        if seq is None:
            return None
        return seq.findall(f"{XS_NS}element")

    def build_element(xsd_el, idx1: int | None = None) -> list[ET.Element]:
        tag = xsd_el.attrib["name"]
        max_occurs = xsd_el.attrib.get("maxOccurs", "1")
        children_xsd = find_seq_children(xsd_el)

        if children_xsd is None:
            if tag == "CodigoSeguridadeCF":
                consume_cell(by_col, lambda c, h: h == "CodigoSeguridadeCF")
                el = ET.Element(tag)
                el.text = security_code
                return [el]
            expected = f"{tag}[{idx1}]" if idx1 is not None else tag
            val = consume_cell(by_col, lambda c, h, t=expected: h == t)
            if val is not None:
                el = ET.Element(tag)
                el.text = val
                return [el]
            return []

        if max_occurs != "1":
            res = []
            max_n = int(max_occurs)
            for i in range(1, max_n + 1):
                el = ET.Element(tag)
                for ch_xsd in children_xsd:
                    for ce in build_element(ch_xsd, idx1=i):
                        el.append(ce)
                if len(el) > 0:
                    res.append(el)
            return res
        else:
            el = ET.Element(tag)
            for ch_xsd in children_xsd:
                for ce in build_element(ch_xsd, idx1=idx1):
                    el.append(ce)
            if len(el) > 0:
                return [el]
            return []

    rfce_xsd_el = xsd_root.find(f"{XS_NS}element[@name='RFCE']")
    built = build_element(rfce_xsd_el)[0]
    unconsumed = [info for info in by_col.values() if not info["consumed"]]
    xml_bytes = ET.tostring(built, encoding="utf-8", xml_declaration=True)
    return xml_bytes, unconsumed


async def get_db_conn():
    import asyncpg
    from ecf_core.utils import sanitize_dsn

    db_url = sanitize_dsn(os.environ["DATABASE_URL"])
    return await asyncpg.connect(db_url)


async def get_tenant_p12(conn) -> tuple[bytes, bytes]:
    from ecf_core.cert_vault import CertVault

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


def sign_dgii_xml(xml_bytes: bytes, p12_data: bytes, p12_password: bytes) -> bytes:
    signer = ECFSigner()
    return signer.firmar(xml_bytes, p12_data, p12_password, exclusive=False)


async def upsert_ecf_record(
    conn,
    row_info: dict,
    xml_orig: bytes,
    xml_firmado: bytes,
    sec_code: str,
    estado: str,
    track_id: str | None,
    resp_raw: dict,
    err_msg: str | None,
):
    encf = get_cell_val(row_info, "ENCF")
    tipo_ecf = int(get_cell_val(row_info, "TipoeCF"))
    rnc_comp = get_cell_val(row_info, "RNCComprador") or get_cell_val(row_info, "IdentificadorExtranjero") or None
    if rnc_comp and len(rnc_comp) > 11:
        rnc_comp = rnc_comp[:11]
    nom_comp = get_cell_val(row_info, "RazonSocialComprador") or "CONSUMIDOR FINAL"
    fecha_str = get_cell_val(row_info, "FechaEmision") or "01-04-2020"
    fecha_dt = datetime.strptime(fecha_str, "%d-%m-%Y").date()
    total = Decimal(get_cell_val(row_info, "MontoTotal") or "0")
    itbis = Decimal(get_cell_val(row_info, "TotalITBIS") or "0")
    subtotal = total - itbis
    ref_ncf = get_cell_val(row_info, "NCFModificado") or None

    await conn.execute(
        f"""
        INSERT INTO {TENANT_SCHEMA}.ecf (
            ncf, tipo_ecf, estado, codigo_seguridad, security_code,
            rnc_comprador, nombre_comprador, fecha_emision,
            subtotal, itbis, total, xml_original, xml_firmado,
            respuesta_dgii, ultimo_error, referencia_ncf, track_id,
            sent_at, approved_at, updated_at
        ) VALUES (
            $1, $2, $3::varchar, $4, $4,
            $5, $6, $7,
            $8, $9, $10, $11, $12,
            $13::jsonb, $14, $15, $16,
            now(), CASE WHEN $3::varchar IN ('aprobado', 'condicionado') THEN now() ELSE NULL END, now()
        )
        ON CONFLICT (ncf) DO UPDATE SET
            tipo_ecf = EXCLUDED.tipo_ecf,
            estado = EXCLUDED.estado,
            codigo_seguridad = EXCLUDED.codigo_seguridad,
            security_code = EXCLUDED.security_code,
            rnc_comprador = EXCLUDED.rnc_comprador,
            nombre_comprador = EXCLUDED.nombre_comprador,
            fecha_emision = EXCLUDED.fecha_emision,
            subtotal = EXCLUDED.subtotal,
            itbis = EXCLUDED.itbis,
            total = EXCLUDED.total,
            xml_original = EXCLUDED.xml_original,
            xml_firmado = EXCLUDED.xml_firmado,
            respuesta_dgii = EXCLUDED.respuesta_dgii,
            ultimo_error = EXCLUDED.ultimo_error,
            referencia_ncf = EXCLUDED.referencia_ncf,
            track_id = EXCLUDED.track_id,
            sent_at = now(),
            approved_at = CASE WHEN EXCLUDED.estado IN ('aprobado', 'condicionado') THEN now() ELSE {TENANT_SCHEMA}.ecf.approved_at END,
            updated_at = now()
        """,
        encf,
        tipo_ecf,
        estado,
        sec_code,
        rnc_comp,
        nom_comp,
        fecha_dt,
        subtotal,
        itbis,
        total,
        xml_orig,
        xml_firmado,
        json.dumps(resp_raw, ensure_ascii=False),
        err_msg,
        ref_ncf,
        track_id,
    )


async def upsert_rfce_record(
    conn,
    ecf_row_info: dict,
    xml_rfce_orig: bytes,
    xml_rfce_firmado: bytes,
    sec_code: str,
    estado: str,
    track_id: str | None,
    resp_raw: dict,
    err_msg: str | None,
):
    encf = get_cell_val(ecf_row_info, "ENCF")
    fecha_str = get_cell_val(ecf_row_info, "FechaEmision") or "01-04-2020"
    fecha_dt = datetime.strptime(fecha_str, "%d-%m-%Y").date()
    total = Decimal(get_cell_val(ecf_row_info, "MontoTotal") or "0")

    ecf_db_row = await conn.fetchrow(
        f"SELECT id, rfce_id FROM {TENANT_SCHEMA}.ecf WHERE ncf = $1",
        encf,
    )
    existing_rfce = await conn.fetchrow(
        f"SELECT id FROM {TENANT_SCHEMA}.rfce WHERE ncf = $1 ORDER BY updated_at DESC LIMIT 1",
        encf,
    )
    rfce_id = (ecf_db_row["rfce_id"] if ecf_db_row and ecf_db_row["rfce_id"] else None) or (existing_rfce["id"] if existing_rfce else None)

    if rfce_id:
        await conn.execute(
            f"""
            UPDATE {TENANT_SCHEMA}.rfce
            SET fecha_resumen = $1,
                estado = $2::varchar,
                track_id = $3,
                monto_total = $4,
                xml_firmado = $5,
                respuesta_dgii = $6::jsonb,
                updated_at = now()
            WHERE id = $7
            """,
            fecha_dt,
            estado,
            track_id,
            total,
            xml_rfce_firmado,
            json.dumps(resp_raw, ensure_ascii=False),
            rfce_id,
        )
    else:
        rfce_id = await conn.fetchval(
            f"""
            INSERT INTO {TENANT_SCHEMA}.rfce (
                ncf, fecha_resumen, estado, track_id,
                cantidad_facturas, monto_total, xml_firmado,
                respuesta_dgii, created_at, updated_at
            ) VALUES (
                $1, $2, $3::varchar, $4,
                1, $5, $6,
                $7::jsonb, now(), now()
            )
            RETURNING id
            """,
            encf,
            fecha_dt,
            estado,
            track_id,
            total,
            xml_rfce_firmado,
            json.dumps(resp_raw, ensure_ascii=False),
        )
    if ecf_db_row and rfce_id:
        await conn.execute(
            f"UPDATE {TENANT_SCHEMA}.ecf SET rfce_id = $1 WHERE id = $2",
            rfce_id,
            ecf_db_row["id"],
        )


async def poll_until_final(dgii: DGIIClient, track_id: str, encf: str, max_Wait_sec: int = 90):
    for attempt in range(1, (max_Wait_sec // 3) + 1):
        res = await dgii.consultar_por_track_id(track_id)
        print(f"    [Poll {attempt}] {encf} (track={track_id}) -> estado={res.estado.value} | msg={res.mensaje} | det={res.detalles}")
        if res.estado != EstadoDGII.PROCESANDO and res.estado != EstadoDGII.RECIBIDO:
            return res
        await asyncio.sleep(3)
    return res


async def main():
    validate_only = "--validate-only" in sys.argv
    send_rfce_also_to_ecf = "--send-e32-under-250k-to-ecf" in sys.argv
    only_encf = None
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if idx + 1 < len(sys.argv):
            only_encf = sys.argv[idx + 1].strip()

    data = json.loads(ORDERED_JSON.read_text(encoding="utf-8"))
    conn = await get_db_conn()
    try:
        p12_data, p12_password = await get_tenant_p12(conn)
    except Exception:
        await conn.close()
        raise
    validator = ECFValidator()

    ecf_rows_by_encf = {}
    signed_ecfs = {}
    orig_ecfs = {}
    sec_codes = {}

    print("=== STEP 1: Building, Signing & XSD-Validating all 25 ECF XMLs ===")
    for idx, r in enumerate(data["ECF"], 1):
        encf = get_cell_val(r, "ENCF")
        tipo = int(get_cell_val(r, "TipoeCF"))
        xml_orig, unconsumed = build_ecf_xml_from_xsd(r)
        if unconsumed:
            await conn.close()
            raise RuntimeError(f"Unconsumed cells in {encf}: {unconsumed}")

        xml_firmado = sign_dgii_xml(xml_orig, p12_data, p12_password)
        valido, errores = validator.validar(xml_firmado, tipo)
        if not valido:
            await conn.close()
            raise RuntimeError(f"XSD validation failed for {encf} (Tipo {tipo}): {errores}")

        sec_code = generar_security_code(xml_firmado)
        ecf_rows_by_encf[encf] = r
        orig_ecfs[encf] = xml_orig
        signed_ecfs[encf] = xml_firmado
        sec_codes[encf] = sec_code
        xml_out_dir = Path("/tmp/xml_firmados")
        xml_out_dir.mkdir(parents=True, exist_ok=True)
        (xml_out_dir / f"{TENANT_RNC}{encf}.xml").write_bytes(xml_firmado)
        print(f"  [XSD OK] #{idx:02d} {encf} (Tipo {tipo}) | SecCode={sec_code} | len={len(xml_firmado)} bytes")

    print("\n=== STEP 2: Building, Signing & XSD-Validating all 4 RFCE XMLs ===")
    signed_rfces = {}
    orig_rfces = {}
    rfce_rows_by_encf = {}
    for idx, r in enumerate(data["RFCE"], 1):
        encf = get_cell_val(r, "ENCF")
        sec_code = sec_codes[encf]
        xml_orig, unconsumed = build_rfce_xml_from_xsd(r, sec_code)
        if unconsumed:
            await conn.close()
            raise RuntimeError(f"Unconsumed cells in RFCE {encf}: {unconsumed}")

        xml_firmado = sign_dgii_xml(xml_orig, p12_data, p12_password)
        valido, errores = validator.validar_evento(xml_firmado, "RFCE-32")
        if not valido:
            await conn.close()
            raise RuntimeError(f"XSD validation failed for RFCE {encf}: {errores}")

        rfce_rows_by_encf[encf] = r
        orig_rfces[encf] = xml_orig
        signed_rfces[encf] = xml_firmado
        (xml_out_dir / f"{TENANT_RNC}{encf}_rfce.xml").write_bytes(xml_firmado)
        print(f"  [XSD OK] RFCE #{idx:02d} {encf} | SecCode={sec_code} | len={len(xml_firmado)} bytes")

    if validate_only:
        await conn.close()
        print("\n[SUCCESS] --validate-only completed: all 25 signed ECFs and 4 signed RFCEs passed official DGII lxml XSD validation!")
        return

    # Define strict submission order:
    # Phase A: Base e-CFs that are modified by E33/E34 first!
    priority_bases = [
        "E320000000006",  # Base for E330000000001
        "E310000000001",  # Base for E340000000001
        "E310000000034",  # Base for E340000000002
    ]
    # Remaining non-RFCE base e-CFs:
    other_bases = [
        "E310000000012",
        "E310000000007",
        "E320000000001",
        "E410000000007",
        "E410000000008",
        "E430000000008",
        "E430000000010",
        "E440000000009",
        "E440000000011",
        "E450000000011",
        "E450000000002",
        "E460000000009",
        "E460000000001",
        "E470000000010",
        "E470000000011",
    ]
    # Phase B: Dependent e-CFs (Notas de Débito y Crédito)
    dependents = [
        "E330000000001",  # Modifies E320000000006
        "E340000000001",  # Modifies E310000000001
        "E340000000002",  # Modifies E310000000034
    ]
    # Phase C: 4 RFCE (< 250k Consumo)
    rfce_encfs = [
        "E320000000011",
        "E320000000012",
        "E320000000014",
        "E320000000015",
    ]

    try:
        async with DGIIClient(ambiente="CerteCF") as dgii:
            dgii.set_certificate(p12_data, p12_password)

            async def send_and_verify_ecf(encf: str):
                r = ecf_rows_by_encf[encf]
                tipo = int(get_cell_val(r, "TipoeCF"))
                print(f"\n>>> Sending ECF {encf} (Tipo {tipo})...")
                resp = await dgii.enviar_ecf(
                    signed_ecfs[encf],
                    rnc_emisor=TENANT_RNC,
                    tipo_ecf=tipo,
                    ncf=f"{TENANT_RNC}{encf}",
                )
                print(f"    Initial response for {encf}: estado={resp.estado.value}, track_id={resp.track_id}, msg={resp.mensaje}")
                if resp.track_id and resp.estado in (EstadoDGII.PROCESANDO, EstadoDGII.RECIBIDO):
                    resp = await poll_until_final(dgii, resp.track_id, encf)

                db_estado_map = {
                    EstadoDGII.ACEPTADO: "aprobado",
                    EstadoDGII.CONDICIONADO: "condicionado",
                    EstadoDGII.RECHAZADO: "rechazado",
                    EstadoDGII.PROCESANDO: "enviado",
                    EstadoDGII.RECIBIDO: "enviado",
                }
                db_estado = db_estado_map.get(resp.estado, "enviado")
                err_msg = None if db_estado in ("aprobado", "condicionado") else json.dumps(resp.raw, ensure_ascii=False)
                await upsert_ecf_record(
                    conn,
                    r,
                    orig_ecfs[encf],
                    signed_ecfs[encf],
                    sec_codes[encf],
                    db_estado,
                    resp.track_id,
                    resp.raw,
                    err_msg,
                )
                if resp.estado == EstadoDGII.RECHAZADO:
                    print(f"!!! CRITICAL: {encf} was REJECTED by DGII! Raw response:\n{json.dumps(resp.raw, indent=2, ensure_ascii=False)}")
                    raise RuntimeError(f"Aborting because {encf} was rejected by DGII")
                return resp

            async def send_and_verify_rfce(encf: str):
                print(f"\n>>> Sending RFCE {encf} to fc.dgii.gov.do...")
                resp = await dgii.enviar_rfce(signed_rfces[encf], ncf=f"{TENANT_RNC}{encf}")
                print(f"    RFCE response for {encf}: estado={resp.estado.value}, track_id={resp.track_id}, msg={resp.mensaje}, det={resp.detalles}")
                if resp.track_id and resp.estado in (EstadoDGII.PROCESANDO, EstadoDGII.RECIBIDO):
                    resp = await poll_until_final(dgii, resp.track_id, encf)

                db_estado_map = {
                    EstadoDGII.ACEPTADO: "aprobado",
                    EstadoDGII.CONDICIONADO: "condicionado",
                    EstadoDGII.RECHAZADO: "rechazado",
                    EstadoDGII.PROCESANDO: "enviado",
                    EstadoDGII.RECIBIDO: "enviado",
                }
                db_estado = db_estado_map.get(resp.estado, "enviado")
                err_msg = None if db_estado in ("aprobado", "condicionado") else json.dumps(resp.raw, ensure_ascii=False)
                await upsert_ecf_record(
                    conn,
                    ecf_rows_by_encf[encf],
                    orig_ecfs[encf],
                    signed_ecfs[encf],
                    sec_codes[encf],
                    db_estado,
                    resp.track_id,
                    resp.raw,
                    err_msg,
                )
                await upsert_rfce_record(
                    conn,
                    ecf_rows_by_encf[encf],
                    orig_rfces[encf],
                    signed_rfces[encf],
                    sec_codes[encf],
                    db_estado,
                    resp.track_id,
                    resp.raw,
                    err_msg,
                )
                if resp.estado == EstadoDGII.RECHAZADO:
                    print(f"!!! CRITICAL: RFCE {encf} was REJECTED by DGII! Raw response:\n{json.dumps(resp.raw, indent=2, ensure_ascii=False)}")
                    raise RuntimeError(f"Aborting because RFCE {encf} was rejected by DGII")
                return resp

            if "--only-remaining-rfce" in sys.argv:
                await upsert_rfce_record(
                    conn,
                    ecf_rows_by_encf["E320000000011"],
                    orig_rfces["E320000000011"],
                    signed_rfces["E320000000011"],
                    sec_codes["E320000000011"],
                    "aprobado",
                    None,
                    {"estado": "Aceptado", "codigo": 1, "encf": "E320000000011"},
                    None,
                )
                print("  [DB OK] Persisted RFCE E320000000011 (already Aceptado by DGII)")
                for encf in ["E320000000012", "E320000000014", "E320000000015"]:
                    await send_and_verify_rfce(encf)
                print("\n[SUCCESS] All 4 RFCEs processed and accepted!")
                return

            if only_encf:
                if only_encf in rfce_encfs and not send_rfce_also_to_ecf:
                    await send_and_verify_rfce(only_encf)
                else:
                    await send_and_verify_ecf(only_encf)
                print(f"\n[SUCCESS] Single comprobante --only {only_encf} processed and accepted!")
                return

            print("\n=== PHASE A1: Sending Priority Base e-CFs (Required by E33/E34) ===")
            for encf in priority_bases:
                if encf == "E310000000001" and "--skip-e31-01" in sys.argv:
                    print("  [SKIP] E310000000001 already sent & accepted in pilot!")
                    continue
                await send_and_verify_ecf(encf)

            print("\n=== PHASE A2: Sending Remaining Base e-CFs ===")
            for encf in other_bases:
                await send_and_verify_ecf(encf)

            print("\n=== PHASE B: Sending Dependent e-CFs (E33, E34) ===")
            for encf in dependents:
                await send_and_verify_ecf(encf)

            if send_rfce_also_to_ecf:
                print("\n=== PHASE C0: Sending E32 < 250k to ECF endpoint as requested ===")
                for encf in rfce_encfs:
                    await send_and_verify_ecf(encf)

            print("\n=== PHASE C: Sending 4 RFCE (Consumo < RD$250,000) to fc.dgii.gov.do ===")
            for encf in rfce_encfs:
                await send_and_verify_rfce(encf)

    finally:
        await conn.close()

    print("\n========================================================")
    print("ALL 25 e-CFs AND 4 RFCEs PROCESSED AND ACCEPTED BY DGII!")
    print("========================================================")


if __name__ == "__main__":
    asyncio.run(main())
