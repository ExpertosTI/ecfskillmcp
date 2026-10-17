---
name: dgii-ecf-certification
description: >-
  Complete runbook, validator, and automation suite for Dominican Republic DGII
  e-CF & RFCE certification (CerteCF / Set de Pruebas / Postulación / Pase a Producción).
  Use this skill whenever onboarding a new company (RNC) to DGII Facturación Electrónica,
  signing Postulación XMLs, parsing the official DGII Set de Pruebas Excel, validating
  against DGII XSDs (ECF-31..47 & RFCE-32), or submitting certification comprobantes.
---

# DGII e-CF & RFCE Certification Skill (`CerteCF` -> `Producción`)

This skill provides the battle-tested methodology, rules, scripts, and MCP tools to pass the Dominican Republic DGII Electronic Invoicing (`e-CF`) certification (`CerteCF`) with **zero rejections**.

---

## 1. Golden Rules of DGII `CerteCF` (Non-Negotiable)

> [!CAUTION]
> **One Rejection Resets the Entire Set!**
> In DGII `CerteCF` (*Pruebas de Datos de e-CF*), if **any** comprobante is rejected (`Estado: Rechazado`), DGII automatically resets the entire test set (`Las pruebas de datos de eCF han sido reiniciadas debido a que se han rechazado comprobantes`). Never send unvalidated XMLs or allow background workers to emit stale jobs during certification.

### The 7 Critical DGII Technical Requirements

1. **Freeze Background Workers Before Running Set de Pruebas:**
   - Pause `ecf_worker` and `ecf_scheduler` (`docker pause`) and clear any `pendiente`/`encolado` rows in `{schema}.ecf` and `{schema}.rfce` before starting.
2. **Exact 26-Character Multipart Filename (`{RNCEmisor}{eNCF}.xml`):**
   - Every e-CF and RFCE uploaded via `multipart/form-data` (`field="xml"`) **MUST** be named `{RNCEmisor}{eNCF}.xml` (e.g., `132842316E310000000001.xml` = 9-digit RNC + 13-char e-NCF + `.xml`).
   - Sending `E310000000001.xml` (without the RNC prefix) causes DGII rejection: `"La longitud del nombre del archivo no es válida."`.
3. **Inclusive Canonicalization (`REC-xml-c14n-20010315` / `exclusive=False`):**
   - DGII's .NET `SignedXml` verifier requires `http://www.w3.org/TR/2001/REC-xml-c14n-20010315` (`exclusive=False` in `ECFSigner.firmar()`).
   - `FechaHoraFirma` inside the XML must be set to `"PENDING_SIGN"` before signing so `ECFSigner.firmar()` populates it with the exact Dominican Republic (`AST` / `UTC-4`) timestamp (`DD-MM-YYYY HH:MM:SS`) matching the signature time.
4. **100% XSD-Driven Element Ordering & Excel Cell Consumption:**
   - The DGII Excel test file (`{RNC}-{timestamp}.xlsx`) has two sheets: `ECF` (25 rows) and `RFCE` (4 rows), with up to 5,220 columns.
   - Multiple XML sections reuse identical column header names in the Excel (e.g., `TelefonoEmisor[1]` vs buyer phone, `MontoSubTotal` vs `SubTotalImpuestoAdicional`, `TipoImpuesto[1]..[4]` across header and items).
   - Always map cells by **column index intervals (`col_idx`)** and build XML trees by traversing the official `ECF-{tipo}.xsd` and `RFCE-32.xsd` `<xs:sequence>` definitions so 100% of non-empty Excel cells are consumed (`unconsumed == 0`) and `lxml.etree.XMLSchema` validation passes locally before sending a single byte to DGII.
5. **Header Normalization Quirks in DGII Excel:**
   - Column `ENCF` in Excel maps to XML tag `<eNCF>`.
   - Column `MontosubRecargo[i]` (lowercase `s`) in Excel maps to XML tag `<MontoSubRecargo>`.
   - Sentinel string `#e` in the Excel means **empty/omitted** — never include `#e` in the XML.
6. **Strict 4-Phase Submission Order (Dependencies & Hosts):**
   - **Phase A1 (Priority Base e-CFs):** Send the base invoices referenced by `E33` and `E34` **first** and wait until `Aceptado`:
     - `E320000000006` (referenced by `E330000000001`)
     - `E310000000001` (referenced by `E340000000001`)
     - `E310000000034` (referenced by `E340000000002`)
   - **Phase A2 (Remaining Base e-CFs):** Send the remaining 15 full e-CFs (`E31`, `E32 >= 250k`, `E41`, `E43`, `E44`, `E45`, `E46`, `E47`) to `https://ecf.dgii.gov.do/certecf/Recepcion/api/FacturasElectronicas`.
   - **Phase B (Modification e-CFs):** Send `E330000000001`, `E340000000001`, `E340000000002` **only after** Phase A1 is `Aceptado` (prevents `"El eNCF modificado no ha sido emitido"`).
   - **Phase C (4 RFCEs < RD$250,000):**
     - For `E320000000011`, `E320000000012`, `E320000000014`, `E320000000015` (`Tipo 32 < RD$250,000`), first sign their full `ECF` XML locally to extract the 6-character `CodigoSeguridadeCF` (first 6 chars of `SignatureValue`), then build and sign the `RFCE` XML (`RFCE-32.xsd`) with that `CodigoSeguridadeCF`, and submit ONLY the `RFCE` XML to `https://fc.dgii.gov.do/certecf/recepcionfc/api/recepcion/ecf`.
7. **Postulación XML Signing:**
   - Keep the exact filename downloaded from the DGII portal (e.g., `postulacion_58327.xml` inside `postulacion_58327.zip`), sign with `ECFSigner().firmar(..., exclusive=False)`.

---

## 2. Step-by-Step Certification Workflow

### Step 1: Extract Ordered JSON from DGII Excel
Use [extract_excel_to_json.py](./scripts/extract_excel_to_json.py) to parse the tenant's official DGII Excel file into `set_pruebas_ordered.json`:
```bash
python3 scripts/extract_excel_to_json.py /path/to/132842316-16102026193033.xlsx /tmp/set_pruebas_ordered.json
```

### Step 2: Pre-Flight Offline XSD Validation (`--validate-only`)
Run [run_dgii_set_pruebas_vps.py](./scripts/run_dgii_set_pruebas_vps.py) with `--validate-only` to build, sign, and validate all 25 `ECF` XMLs and 4 `RFCE` XMLs against the official DGII XSDs without sending anything to DGII:
```bash
python3 scripts/run_dgii_set_pruebas_vps.py --validate-only
```
Verify that all 25 ECFs and 4 RFCEs print `[XSD OK]` and `unconsumed=0`.

### Step 3: Pilot Single Comprobante (`--only E310000000001`)
Test a single priority base comprobante first:
```bash
python3 scripts/run_dgii_set_pruebas_vps.py --only E310000000001
```
Confirm `estado=Aceptado` before proceeding.

### Step 4: Full Sequential Certification Run
Execute the remaining comprobantes in strict 4-phase order:
```bash
python3 scripts/run_dgii_set_pruebas_vps.py --skip-e31-01
```

---

## 3. Included Scripts & MCP Tools

- **[`scripts/extract_excel_to_json.py`](./scripts/extract_excel_to_json.py)**: Parses any DGII `CerteCF` Excel workbook (`ECF` + `RFCE` sheets) into an ordered JSON preserving `col_idx` and stripping `#e` sentinels.
- **[`scripts/run_dgii_set_pruebas_vps.py`](./scripts/run_dgii_set_pruebas_vps.py)**: Full XSD-driven XML builder, XAdES/XML-DSig signer, XSD validator, DGII `CerteCF` transmitter, and PostgreSQL persistence script.
- **[`../../mcp_server/dgii_certification_mcp.py`](../../mcp_server/dgii_certification_mcp.py)**: MCP Server exposing tools to inspect tenant readiness, parse DGII Excel files, validate XMLs against DGII XSDs, and run certification steps directly from Antigravity.
