from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import pathlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from lxml import etree

from ecf_core.cert_vault import CertVault, CertVaultError, CertVaultRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])

_db_pool: Optional[asyncpg.Pool] = None


def set_db_pool(pool: asyncpg.Pool):
    global _db_pool
    _db_pool = pool


async def get_db_pool(request: Request) -> asyncpg.Pool:
    pool = getattr(request.app.state, "db_pool", None) or _db_pool
    if pool is not None:
        return pool
    from ecf_core.utils import sanitize_dsn
    db_url = sanitize_dsn(os.environ.get("DATABASE_URL", ""))
    if db_url:
        pool = await asyncpg.create_pool(dsn=db_url, min_size=2, max_size=10)
        set_db_pool(pool)
        return pool
    raise HTTPException(status_code=503, detail="Base de datos no disponible")


NAMESPACE_DS = "http://www.w3.org/2000/09/xmldsig#"
XADES_NS = "http://uri.etsi.org/01903/v1.3.2#"


def firmar_postulacion_xml(xml_bytes: bytes, p12_data: bytes, p12_password: bytes) -> bytes:
    private_key, certificate, _ = pkcs12.load_key_and_certificates(p12_data, p12_password)

    parser = etree.XMLParser(remove_blank_text=True)
    root = etree.fromstring(xml_bytes, parser)

    signing_time = datetime.now(timezone.utc)
    signing_time_iso = signing_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    cert_digest = base64.b64encode(certificate.fingerprint(hashes.SHA256())).decode()
    cert_serial = str(certificate.serial_number)
    cert_issuer = certificate.issuer.rfc4514_string()
    cert_b64 = base64.b64encode(
        certificate.public_bytes(serialization.Encoding.DER)
    ).decode()

    signature_id = f"Signature-{uuid.uuid4()}"
    signed_props_id = f"SignedProperties-{uuid.uuid4()}"

    sig_node = etree.SubElement(
        root, f"{{{NAMESPACE_DS}}}Signature", nsmap={"ds": NAMESPACE_DS}
    )
    sig_node.set("Id", signature_id)

    obj_node = etree.SubElement(sig_node, f"{{{NAMESPACE_DS}}}Object")
    qprops = etree.SubElement(
        obj_node, f"{{{XADES_NS}}}QualifyingProperties", nsmap={"xades": XADES_NS}
    )
    qprops.set("Target", f"#{signature_id}")

    signed_props = etree.SubElement(qprops, f"{{{XADES_NS}}}SignedProperties")
    signed_props.set("Id", signed_props_id)
    ssp = etree.SubElement(signed_props, f"{{{XADES_NS}}}SignedSignatureProperties")
    etree.SubElement(ssp, f"{{{XADES_NS}}}SigningTime").text = signing_time_iso

    scerts = etree.SubElement(ssp, f"{{{XADES_NS}}}SigningCertificate")
    scert = etree.SubElement(scerts, f"{{{XADES_NS}}}Cert")
    cv = etree.SubElement(scert, f"{{{XADES_NS}}}CertDigest")
    etree.SubElement(cv, f"{{{NAMESPACE_DS}}}DigestMethod").set(
        "Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256"
    )
    etree.SubElement(cv, f"{{{NAMESPACE_DS}}}DigestValue").text = cert_digest
    issuer_serial = etree.SubElement(scert, f"{{{XADES_NS}}}IssuerSerial")
    etree.SubElement(issuer_serial, f"{{{NAMESPACE_DS}}}X509IssuerName").text = cert_issuer
    etree.SubElement(issuer_serial, f"{{{NAMESPACE_DS}}}X509SerialNumber").text = cert_serial

    root_copy = etree.fromstring(etree.tostring(root))
    for sig in root_copy.findall(f"{{{NAMESPACE_DS}}}Signature"):
        root_copy.remove(sig)
    doc_c14n = etree.tostring(root_copy, method="c14n", exclusive=False)
    doc_digest = base64.b64encode(hashlib.sha256(doc_c14n).digest()).decode()

    signed_info = etree.Element(
        f"{{{NAMESPACE_DS}}}SignedInfo", nsmap={"ds": NAMESPACE_DS}
    )
    etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}CanonicalizationMethod").set(
        "Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
    )
    sm = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}SignatureMethod")
    sm.set("Algorithm", "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")

    ref_doc = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}Reference")
    ref_doc.set("URI", "")
    transforms = etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}Transforms")
    t1 = etree.SubElement(transforms, f"{{{NAMESPACE_DS}}}Transform")
    t1.set("Algorithm", "http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}DigestMethod").set(
        "Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256"
    )
    etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}DigestValue").text = doc_digest

    si_bytes = etree.tostring(signed_info, method="c14n", exclusive=False)
    raw_sig = private_key.sign(si_bytes, padding.PKCS1v15(), hashes.SHA256())
    sig_value_b64 = base64.b64encode(raw_sig).decode()

    sig_node.insert(0, signed_info)
    sv_el = etree.SubElement(sig_node, f"{{{NAMESPACE_DS}}}SignatureValue")
    sv_el.text = sig_value_b64

    ki = etree.SubElement(sig_node, f"{{{NAMESPACE_DS}}}KeyInfo")
    x509 = etree.SubElement(ki, f"{{{NAMESPACE_DS}}}X509Data")
    etree.SubElement(x509, f"{{{NAMESPACE_DS}}}X509Certificate").text = cert_b64

    return etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


@router.post("/firmar-postulacion")
async def firmar_postulacion_endpoint(
    xml_file: UploadFile = File(...),
    p12_file: UploadFile = File(...),
    password: str = Form(...),
):
    """Firma digitalmente la Solicitud de Postulación XML emitida por la DGII."""
    try:
        xml_bytes = await xml_file.read()
        p12_bytes = await p12_file.read()
        if not xml_bytes or not p12_bytes:
            raise HTTPException(status_code=400, detail="Archivos XML y P12 requeridos")

        signed_bytes = firmar_postulacion_xml(xml_bytes, p12_bytes, password.encode("utf-8"))
        filename = xml_file.filename or "postulacion.xml"
        if not filename.endswith("_firmada.xml"):
            filename = filename.replace(".xml", "_firmada.xml")

        return Response(
            content=signed_bytes,
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error("Error firmando postulación: %s", e, exc_info=True)
        raise HTTPException(status_code=422, detail=f"Error firmando XML: {str(e)}")


@router.post("/register")
async def register_client_endpoint(
    request: Request,
    rnc: str = Form(...),
    razon_social: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    telefono: Optional[str] = Form(None),
    cert_file: UploadFile = File(...),
    cert_password: str = Form(...),
    excel_file: Optional[UploadFile] = File(None),
    db: asyncpg.Pool = Depends(get_db_pool),
):
    """Registra un nuevo cliente/tenant, cifra y guarda su certificado digital en Bóveda,
    y procesa tanto Set de Pruebas Excel como XML de Postulación.
    """
    rnc_clean = "".join(filter(str.isdigit, rnc))
    if not (len(rnc_clean) in (9, 11)):
        raise HTTPException(status_code=422, detail="RNC o Cédula inválido (debe tener 9 u 11 dígitos)")

    p12_data = await cert_file.read()
    if len(p12_data) > 60_000 or len(p12_data) < 100:
        raise HTTPException(status_code=422, detail="Archivo .p12 inválido o excede tamaño permitido (máx 60KB)")

    vault = CertVault()
    try:
        metadatos = vault.extraer_metadatos(p12_data, cert_password.encode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Contraseña incorrecta o certificado .p12 inválido: {exc}")

    rs = (razon_social or "").strip()
    if not rs:
        async with db.acquire() as conn:
            padron_row = await conn.fetchrow(
                "SELECT razon_social FROM public.dgii_rnc WHERE rnc = $1", rnc_clean
            )
            if padron_row and padron_row["razon_social"]:
                rs = padron_row["razon_social"]
            else:
                subj = metadatos.get("subject", {})
                rs = subj.get("CN") if isinstance(subj, dict) else str(subj)
                if not rs or rs == "None":
                    rs = f"Contribuyente {rnc_clean}"

    # Directorio persistente del tenant en servidor
    tenant_dir = pathlib.Path(f"/app/data/tenants/{rnc_clean}")
    tenant_dir.mkdir(parents=True, exist_ok=True)
    subfolders = [
        "CERTIFICADO",
        "POSTULACION",
        "SET_DE_PRUEBAS",
        "APROBACION_COMERCIAL",
        "PASO_4_SIMULACION",
        "PASO_5_REPRESENTACION_IMPRESA",
        "SUBIR_A_DGII_CONSUMO_MENOR_250K",
        "XML_FIRMADOS",
    ]
    for sub in subfolders:
        (tenant_dir / sub).mkdir(exist_ok=True)

    # Guardar copia del .p12
    cert_filename = cert_file.filename or f"{rnc_clean}_certificado.p12"
    (tenant_dir / "CERTIFICADO" / cert_filename).write_bytes(p12_data)

    async with db.acquire() as conn:
        tenant_row = await conn.fetchrow(
            "SELECT id, rnc, razon_social FROM public.tenants WHERE rnc = $1 AND deleted_at IS NULL",
            rnc_clean,
        )

        if tenant_row:
            tenant_id = str(tenant_row["id"])
            logger.info("Tenant ya existe para RNC %s (id=%s). Actualizando certificado.", rnc_clean, tenant_id)
            await conn.execute(
                """
                UPDATE public.tenants
                SET razon_social = COALESCE(NULLIF($1, ''), razon_social),
                    email = COALESCE(NULLIF($2, ''), email),
                    telefono = COALESCE(NULLIF($3, ''), telefono),
                    updated_at = NOW()
                WHERE id = $4
                """,
                rs,
                email or "",
                telefono or "",
                uuid.UUID(tenant_id),
            )
        else:
            tenant_id = str(uuid.uuid4())
            raw_api_key = f"sk_cert_{secrets.token_hex(24)}"
            api_key = raw_api_key[:64]
            webhook_secret = secrets.token_hex(32)
            encrypted_webhook_secret = vault.cifrar_campo(webhook_secret)
            schema_name = f"tenant_{rnc_clean}"

            await conn.execute(
                """
                INSERT INTO public.tenants (
                    id, rnc, razon_social, nombre_comercial, email, telefono,
                    plan, estado, schema_name, ambiente, api_key, odoo_webhook_secret,
                    max_ecf_mensual, is_platform_operator, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    'profesional', 'activo', $7, 'certificacion', $8, $9,
                    5000, FALSE, NOW(), NOW()
                )
                """,
                uuid.UUID(tenant_id),
                rnc_clean,
                rs,
                rs,
                email or f"admin@{rnc_clean}.com",
                telefono or "",
                schema_name,
                api_key,
                encrypted_webhook_secret,
            )
            try:
                await conn.execute("SELECT public.crear_schema_tenant($1)", schema_name)
            except Exception as schema_err:
                logger.warning("crear_schema_tenant error: %s", schema_err)

            tipos_secuencias = [31, 32, 33, 34, 41, 43, 44, 45, 46, 47]
            for tipo in tipos_secuencias:
                await conn.execute(
                    """
                    INSERT INTO public.ncf_sequences (
                        tenant_id, tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo
                    ) VALUES ($1, $2, $3, 1, 9999999999, TRUE)
                    ON CONFLICT (tenant_id, tipo_ecf) DO NOTHING
                    """,
                    uuid.UUID(tenant_id),
                    tipo,
                    f"E{tipo}",
                )

        encrypted_password = vault.cifrar_campo(cert_password)
        await conn.execute(
            "UPDATE public.tenants SET cert_password = $1, updated_at = NOW() WHERE id = $2",
            encrypted_password,
            uuid.UUID(tenant_id),
        )

        cert_repo = CertVaultRepository(db, vault)
        cert_id = await cert_repo.guardar(tenant_id, p12_data, cert_password.encode("utf-8"))

    # Metadata local del cliente
    info_json = {
        "rnc": rnc_clean,
        "razon_social": rs,
        "email": email or "",
        "telefono": telefono or "",
        "cert_serial": metadatos.get("serial"),
        "valid_from": str(metadatos.get("valid_from")),
        "valid_to": str(metadatos.get("valid_to")),
        "registrado_en": datetime.now(timezone.utc).isoformat(),
        "ambiente": "certificacion",
        "software": "RENECF 2.5",
    }
    (tenant_dir / "cliente_info.json").write_text(json.dumps(info_json, indent=2, ensure_ascii=False))

    # Manejo inteligente del archivo adjunto en Paso 3
    doc_info = {}
    signed_postulacion_b64 = None
    if excel_file and excel_file.filename:
        file_bytes = await excel_file.read()
        fname = excel_file.filename.lower()
        if fname.endswith(".xml"):
            # Es un XML de postulación: FIRMARLO DE INMEDIATO
            try:
                signed_xml = firmar_postulacion_xml(file_bytes, p12_data, cert_password.encode("utf-8"))
                orig_path = tenant_dir / "POSTULACION" / excel_file.filename
                firmada_name = excel_file.filename.replace(".xml", "_firmada.xml")
                firmada_path = tenant_dir / "POSTULACION" / firmada_name
                orig_path.write_bytes(file_bytes)
                firmada_path.write_bytes(signed_xml)
                signed_postulacion_b64 = base64.b64encode(signed_xml).decode("utf-8")
                doc_info = {
                    "tipo": "postulacion_xml",
                    "filename": firmada_name,
                    "mensaje": "¡XML de Postulación firmado digitalmente con éxito!",
                }
            except Exception as sign_err:
                logger.error("Fallo al autofirmar XML de postulación: %s", sign_err)
                doc_info = {"tipo": "postulacion_error", "error": str(sign_err)}
        else:
            # Es un Excel .xlsx del Set de Pruebas
            excel_path = tenant_dir / "SET_DE_PRUEBAS" / excel_file.filename
            excel_path.write_bytes(file_bytes)
            doc_info = {
                "tipo": "excel_set_pruebas",
                "filename": excel_file.filename,
                "mensaje": "Set de Pruebas Excel almacenado y listo para ejecución.",
            }

    return {
        "success": True,
        "tenant_id": tenant_id,
        "rnc": rnc_clean,
        "razon_social": rs,
        "cert": {
            "id": cert_id,
            "serial": metadatos.get("serial"),
            "valid_from": str(metadatos.get("valid_from")),
            "valid_to": str(metadatos.get("valid_to")),
            "subject": metadatos.get("subject"),
        },
        "documento": doc_info,
        "signed_postulacion_b64": signed_postulacion_b64,
        "signed_postulacion_name": doc_info.get("filename"),
        "mensaje": f"Contribuyente {rnc_clean} ({rs}) registrado y certificado con éxito en Renace e-CF.",
    }


@router.get("/status/{rnc}")
async def get_client_status(rnc: str, db: asyncpg.Pool = Depends(get_db_pool)):
    """Consulta el estado del tenant y su certificado."""
    rnc_clean = "".join(filter(str.isdigit, rnc))
    async with db.acquire() as conn:
        tenant = await conn.fetchrow(
            """
            SELECT t.id, t.rnc, t.razon_social, t.ambiente, t.estado,
                   c.cert_serial, c.valid_to, c.activo as cert_activo
            FROM public.tenants t
            LEFT JOIN public.tenant_certs c ON c.tenant_id = t.id AND c.activo = TRUE
            WHERE t.rnc = $1 AND t.deleted_at IS NULL
            ORDER BY c.created_at DESC LIMIT 1
            """,
            rnc_clean,
        )
    if not tenant:
        raise HTTPException(status_code=404, detail="Contribuyente no encontrado")

    return {
        "rnc": tenant["rnc"],
        "razon_social": tenant["razon_social"],
        "ambiente": tenant["ambiente"],
        "estado": tenant["estado"],
        "cert_registrado": bool(tenant["cert_serial"]),
        "cert_serial": tenant["cert_serial"],
        "cert_vence": str(tenant["valid_to"]) if tenant["valid_to"] else None,
    }
