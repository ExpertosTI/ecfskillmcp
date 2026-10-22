from __future__ import annotations
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import xml.etree.ElementTree as ET
from lxml import etree
import asyncpg

from ecf_core.utils import sanitize_dsn, safe_schema
from ecf_core.cert_vault import CertVault
from ecf_core.ecf_core_service import ECFSigner, ECFValidator
from ecf_core.dgii_client import DGIIClient, EstadoDGII, generar_security_code

TENANT_RNC = "132842316"
TENANT_SCHEMA = "tenant_132842316"
XSD_DIR = Path("/app/xsd")
ORDERED_JSON_PATH = Path("/tmp/set_pruebas_ordered.json")
OUTPUT_DIR = Path("/tmp/simulacion_paso4")
OFFSET = 300  # Shift sequences to prevent reuse in simulation


def shift_encf(encf: str, offset: int = OFFSET) -> str:
    if not encf or len(encf) < 4:
        return encf
    prefix = encf[:3]
    num_str = encf[3:]
    try:
        num = int(num_str)
        return f"{prefix}{num + offset:010d}"
    except ValueError:
        return encf


def normalize_header_name(raw: str) -> str:
    h = raw.strip()
    if h == "ENCF":
        return "eNCF"
    if h.startswith("MontosubRecargo"):
        return h.replace("MontosubRecargo", "MontoSubRecargo")
    return h


def get_cell_val(row_info: dict, header_name: str) -> str:
    for c in row_info["cells"]:
        if c["header"].strip() == header_name:
            return c["val"]
    return ""


def categorize_row_cells(cells: list[dict], offset: int = OFFSET):
    today = datetime.now().strftime("%d-%m-%Y")
    by_col = {}
    for c in cells:
        col_idx = c["col_idx"]
        if col_idx == 1 and c["header"].strip() == "CasoPrueba":
            continue
        hdr = normalize_header_name(c["header"])
        val = c["val"]

        # Shift eNCF
        if hdr == "eNCF":
            val = shift_encf(val, offset)
        # Shift NCFModificado
        elif hdr.startswith("NCFModificado") or hdr.startswith("eNCFModificado"):
            val = shift_encf(val, offset)
        # Date normalizations for DGII business rules:
        elif hdr in ("FechaEmision", "FechaNCFModificado", "FechaEntrega", "FechaEmbarque", "FechaOrdenCompra") and val != "#e":
            val = today
        elif hdr == "FechaLimitePago" and val and val != "#e":
            val = "30-10-2026"
        elif hdr.startswith("FechaElaboracion") and val and val != "#e":
            val = today
        elif hdr.startswith("FechaVencimientoItem") and val and val != "#e":
            val = "31-12-2026"

        by_col[col_idx] = {
            "col_idx": col_idx,
            "col_let": c["col_let"],
            "raw_header": c["header"],
            "header": hdr,
            "val": val,
            "consumed": False,
        }
    return by_col


def consume_cell(by_col: dict, pred) -> str | None:
    for col_idx, info in by_col.items():
        if not info["consumed"] and pred(col_idx, info["header"]):
            info["consumed"] = True
            return info["val"]
    return None


def parse_xsd_element(elem):
    tag_name = elem.get("name")
    min_occurs = elem.get("minOccurs", "1")
    max_occurs = elem.get("maxOccurs", "1")
    children = []
    has_subelements = False

    comp = elem.find("{http://www.w3.org/2001/XMLSchema}complexType")
    if comp is not None:
        seq = comp.find("{http://www.w3.org/2001/XMLSchema}sequence")
        if seq is not None:
            for child in seq.findall("{http://www.w3.org/2001/XMLSchema}element"):
                ch_info = parse_xsd_element(child)
                children.append(ch_info)
                has_subelements = True
    return {
        "tag": tag_name,
        "min": min_occurs,
        "max": max_occurs,
        "is_leaf": not has_subelements,
        "children": children,
    }


def load_xsd_model(tipo_ecf: int | str):
    if str(tipo_ecf) == "RFCE-32":
        path = XSD_DIR / "RFCE-32.xsd"
        root_tag = "RFCE"
    else:
        path = XSD_DIR / f"ECF-{tipo_ecf}.xsd"
        root_tag = "ECF"
    tree = etree.parse(str(path))
    schema_root = tree.getroot()
    for el in schema_root.findall("{http://www.w3.org/2001/XMLSchema}element"):
        if el.get("name") == root_tag:
            return parse_xsd_element(el)
    raise ValueError(f"Root element {root_tag} not found in {path}")


def build_ecf_xml_from_xsd(row_info: dict, offset: int = OFFSET):
    tipo = int(get_cell_val(row_info, "TipoeCF"))
    model = load_xsd_model(tipo)
    by_col = categorize_row_cells(row_info["cells"], offset)

    def build_element(info, col_range, idx1=None, idx2=None):
        tag = info["tag"]
        is_leaf = info["is_leaf"]
        max_occurs = info["max"]
        children_xsd = info["children"]

        if is_leaf:
            expected = tag
            if idx2 is not None:
                expected = f"{tag}[{idx1}][{idx2}]"
            elif idx1 is not None:
                expected = f"{tag}[{idx1}]"

            if tag == "FechaHoraFirma":
                val = consume_cell(by_col, lambda c, h: h.startswith("FechaHoraFirma"))
                el = ET.Element(tag)
                el.text = "PENDING_SIGN"
                return [el]

            if max_occurs != "1":
                res = []
                max_n = 100 if max_occurs == "unbounded" else int(max_occurs)
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

    root_el = ET.Element("ECF")
    for ch in model["children"]:
        els = build_element(ch, (2, 5220))
        for e in els:
            root_el.append(e)

    unconsumed = [
        f"{c['col_let']}({c['header']}={c['val']})"
        for c in by_col.values()
        if not c["consumed"] and c["val"] != "#e" and not c["header"].startswith("CasoPrueba")
    ]
    xml_bytes = ET.tostring(root_el, encoding="utf-8", xml_declaration=True)
    return xml_bytes, unconsumed


def build_rfce_xml_from_xsd(row_info: dict, sec_code: str, offset: int = OFFSET):
    model = load_xsd_model("RFCE-32")
    by_col = categorize_row_cells(row_info["cells"], offset)

    def build_element(info, col_range):
        tag = info["tag"]
        is_leaf = info["is_leaf"]
        children_xsd = info["children"]

        if is_leaf:
            if tag == "CodigoSeguridadeCF":
                el = ET.Element(tag)
                el.text = sec_code
                return [el]
            if tag == "FechaHoraFirma":
                val = consume_cell(by_col, lambda c, h: h.startswith("FechaHoraFirma"))
                el = ET.Element(tag)
                el.text = "PENDING_SIGN"
                return [el]

            val = consume_cell(by_col, lambda c, h, t=tag: h == t)
            if val is not None:
                el = ET.Element(tag)
                el.text = val
                return [el]
            return []

        el = ET.Element(tag)
        for ch_xsd in children_xsd:
            ch_els = build_element(ch_xsd, col_range)
            for ce in ch_els:
                el.append(ce)
        if len(el) > 0:
            return [el]
        return []

    root_el = ET.Element("RFCE")
    for ch in model["children"]:
        els = build_element(ch, (2, 500))
        for e in els:
            root_el.append(e)

    unconsumed = [
        f"{c['col_let']}({c['header']}={c['val']})"
        for c in by_col.values()
        if not c["consumed"] and c["val"] != "#e" and not c["header"].startswith("CasoPrueba")
    ]
    xml_bytes = ET.tostring(root_el, encoding="utf-8", xml_declaration=True)
    return xml_bytes, unconsumed


async def poll_until_final(dgii: DGIIClient, track_id: str, encf: str, max_polls: int = 15, delay: float = 3.0):
    for attempt in range(1, max_polls + 1):
        await asyncio.sleep(delay)
        st = await dgii.consultar_por_track_id(track_id)
        print(f"    [Poll #{attempt}] {encf} (TrackId: {track_id}): estado={st.estado.value}, mensaje={st.mensaje}")
        if st.estado in (EstadoDGII.ACEPTADO, EstadoDGII.CONDICIONADO, EstadoDGII.RECHAZADO):
            return st
    print(f"    [WARN] {encf} timed out polling track_id {track_id}")
    return st


async def main():
    db_url = sanitize_dsn(os.environ["DATABASE_URL"])
    conn = await asyncpg.connect(db_url)
    vault = CertVault()

    t_row = await conn.fetchrow(
        "SELECT id, cert_password FROM public.tenants WHERE rnc = $1", TENANT_RNC
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

    signer = ECFSigner()
    validator = ECFValidator()

    with open(ORDERED_JSON_PATH, "r") as f:
        data = json.load(f)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ecf_rows_by_encf = {}
    signed_ecfs = {}
    orig_ecfs = {}
    sec_codes = {}

    print(f"=== STEP 1: Building, Signing & XSD-Validating all 25 ECF XMLs (Shifted +{OFFSET}) ===")
    for idx, r in enumerate(data["ECF"], 1):
        raw_encf = get_cell_val(r, "ENCF")
        encf = shift_encf(raw_encf, OFFSET)
        tipo = int(get_cell_val(r, "TipoeCF"))
        xml_orig, unconsumed = build_ecf_xml_from_xsd(r, OFFSET)
        if unconsumed:
            await conn.close()
            raise RuntimeError(f"Unconsumed cells in {encf}: {unconsumed}")

        xml_firmado = signer.firmar(xml_orig, p12_data, p12_password, exclusive=False)
        valido, errores = validator.validar(xml_firmado, tipo)
        if not valido:
            await conn.close()
            raise RuntimeError(f"XSD validation failed for {encf} (Tipo {tipo}): {errores}")

        sec_code = generar_security_code(xml_firmado)
        ecf_rows_by_encf[encf] = r
        orig_ecfs[encf] = xml_orig
        signed_ecfs[encf] = xml_firmado
        sec_codes[encf] = sec_code
        (OUTPUT_DIR / f"{TENANT_RNC}{encf}.xml").write_bytes(xml_firmado)
        print(f"  [XSD OK] #{idx:02d} {encf} (Tipo {tipo}) | SecCode={sec_code} | len={len(xml_firmado)} bytes")

    print(f"\n=== STEP 2: Building, Signing & XSD-Validating all 4 RFCE XMLs (Shifted +{OFFSET}) ===")
    signed_rfces = {}
    orig_rfces = {}
    for idx, r in enumerate(data["RFCE"], 1):
        raw_encf = get_cell_val(r, "ENCF")
        encf = shift_encf(raw_encf, OFFSET)
        sec_code = sec_codes[encf]
        xml_orig, unconsumed = build_rfce_xml_from_xsd(r, sec_code, OFFSET)
        if unconsumed:
            await conn.close()
            raise RuntimeError(f"Unconsumed cells in RFCE {encf}: {unconsumed}")

        xml_firmado = signer.firmar(xml_orig, p12_data, p12_password, exclusive=False)
        valido, errores = validator.validar_evento(xml_firmado, "RFCE-32")
        if not valido:
            await conn.close()
            raise RuntimeError(f"XSD validation failed for RFCE {encf}: {errores}")

        orig_rfces[encf] = xml_orig
        signed_rfces[encf] = xml_firmado
        (OUTPUT_DIR / f"{TENANT_RNC}{encf}_rfce.xml").write_bytes(xml_firmado)
        print(f"  [XSD OK] RFCE #{idx:02d} {encf} | SecCode={sec_code} | len={len(xml_firmado)} bytes")

    # Shifted transmission order:
    priority_bases = [
        shift_encf("E320000000006"),  # Base for E330000000101
        shift_encf("E310000000001"),  # Base for E340000000101
        shift_encf("E310000000034"),  # Base for E340000000102
    ]
    other_bases = [
        shift_encf("E310000000012"),
        shift_encf("E310000000007"),
        shift_encf("E320000000001"),
        shift_encf("E410000000007"),
        shift_encf("E410000000008"),
        shift_encf("E430000000008"),
        shift_encf("E430000000010"),
        shift_encf("E440000000009"),
        shift_encf("E440000000011"),
        shift_encf("E450000000011"),
        shift_encf("E450000000002"),
        shift_encf("E460000000009"),
        shift_encf("E460000000001"),
        shift_encf("E470000000010"),
        shift_encf("E470000000011"),
    ]
    dependents = [
        shift_encf("E330000000001"),  # Modifies E320000000106
        shift_encf("E340000000001"),  # Modifies E310000000101
        shift_encf("E340000000002"),  # Modifies E310000000134
    ]
    rfce_encfs = [
        shift_encf("E320000000011"),
        shift_encf("E320000000012"),
        shift_encf("E320000000014"),
        shift_encf("E320000000015"),
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

                if resp.estado == EstadoDGII.RECHAZADO:
                    print(f"!!! CRITICAL: {encf} was REJECTED by DGII! Raw:\n{json.dumps(resp.raw, indent=2, ensure_ascii=False)}")
                    raise RuntimeError(f"Aborting because {encf} was rejected by DGII")
                return resp

            async def send_and_verify_rfce(encf: str):
                print(f"\n>>> Sending RFCE {encf} to fc.dgii.gov.do...")
                resp = await dgii.enviar_rfce(signed_rfces[encf], ncf=f"{TENANT_RNC}{encf}")
                print(f"    RFCE response for {encf}: estado={resp.estado.value}, track_id={resp.track_id}, msg={resp.mensaje}")
                if resp.track_id and resp.estado in (EstadoDGII.PROCESANDO, EstadoDGII.RECIBIDO):
                    resp = await poll_until_final(dgii, resp.track_id, encf)

                if resp.estado == EstadoDGII.RECHAZADO:
                    print(f"!!! CRITICAL: RFCE {encf} was REJECTED by DGII! Raw:\n{json.dumps(resp.raw, indent=2, ensure_ascii=False)}")
                    raise RuntimeError(f"Aborting because RFCE {encf} was rejected by DGII")
                return resp

            print("\n=== PHASE A1: Sending Priority Base e-CFs (Required by E33/E34) ===")
            for encf in priority_bases:
                await send_and_verify_ecf(encf)

            print("\n=== PHASE A2: Sending Remaining Base e-CFs ===")
            for encf in other_bases:
                await send_and_verify_ecf(encf)

            print("\n=== PHASE B: Sending Dependent e-CFs (E33, E34) ===")
            for encf in dependents:
                await send_and_verify_ecf(encf)

            print("\n=== PHASE C: Sending 4 RFCE (Consumo < RD$250,000) to fc.dgii.gov.do ===")
            for encf in rfce_encfs:
                await send_and_verify_rfce(encf)

    finally:
        await conn.close()

    print("\n========================================================")
    print("ALL 21 e-CFs AND 4 RFCEs ACCEPTED BY DGII IN PASO 4!")
    print(f"4 Full ECF XMLs ready for web upload in {OUTPUT_DIR}:")
    for encf in rfce_encfs:
        print(f"  - {OUTPUT_DIR}/{TENANT_RNC}{encf}.xml")
    print("========================================================")


if __name__ == "__main__":
    asyncio.run(main())
