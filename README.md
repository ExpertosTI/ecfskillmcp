# DGII e-CF & RFCE Certification Kit (`RENECF`)

Repositorio oficial de **Skills**, **Scripts de Automatización** y **Servidor MCP** para la certificación de Facturación Electrónica (`CerteCF` / `Set de Pruebas` / `Postulación` / `Pase a Producción`) ante la Dirección General de Impuestos Internos (**DGII**, República Dominicana).

---

## Estructura del Repositorio

```text
dgii-ecf-certification-kit/
├── .agents/
│   └── skills/
│       └── dgii-ecf-certification/
│           ├── SKILL.md                             # Runbook y reglas técnicas DGII CerteCF
│           └── scripts/
│               ├── extract_excel_to_json.py         # Extractor ordenado de Excel DGII (5,220 cols)
│               └── run_dgii_set_pruebas_vps.py      # Constructor XSD, firmador, validador y emisor
├── mcp_server/
│   └── dgii_certification_mcp.py                    # Servidor MCP (JSON-RPC 2.0 Stdio)
├── mcp_config.json                                  # Configuración MCP para Antigravity
└── README.md
```

---

## Las 7 Reglas de Oro para Pasar `CerteCF` sin Rechazos

1. **Pausar Workers en Segundo Plano antes del Set de Pruebas:**
   - Un solo rechazo en `CerteCF` reinicia todo el Set de Pruebas (`Las pruebas de datos de eCF han sido reiniciadas debido a que se han rechazado comprobantes`).
2. **Nombre de Archivo Multipart de 26 Caracteres (`{RNCEmisor}{eNCF}.xml`):**
   - Ejemplo: `132842316E310000000001.xml`. Enviar `E310000000001.xml` produce el rechazo `"La longitud del nombre del archivo no es válida."`.
3. **Canonización Inclusiva (`REC-xml-c14n-20010315` / `exclusive=False`):**
   - El validador `.NET SignedXml` de la DGII exige `http://www.w3.org/TR/2001/REC-xml-c14n-20010315` (`ECFSigner().firmar(..., exclusive=False)`) y `FechaHoraFirma` sincronizado en hora local de RD (`AST` / `DD-MM-YYYY HH:MM:SS`).
4. **Construcción Guiada por XSD (`ECF-31..47.xsd` y `RFCE-32.xsd`) por Intervalo de Columna (`col_idx`):**
   - Garantiza que el 100% de las celdas del Excel oficial se consuman (`unconsumed == 0`) y pasen validación `lxml.etree.XMLSchema` antes de enviar a la DGII.
5. **Normalización de Cabeceras del Excel DGII:**
   - `ENCF` -> `<eNCF>`, `MontosubRecargo[i]` -> `<MontoSubRecargo>`, y exclusión del marcador `#e`.
6. **Orden Secuencial Estricto en 4 Fases:**
   - **Fase A1 (Bases prioritarias):** `E320000000006`, `E310000000001`, `E310000000034` (referenciadas por `E33` y `E34`).
   - **Fase A2 (Resto de bases e-CF):** `E31`, `E32 >= 250k`, `E41`, `E43`, `E44`, `E45`, `E46`, `E47`.
   - **Fase B (Dependientes `E33` y `E34`):** `E330000000001`, `E340000000001`, `E340000000002`.
   - **Fase C (4 RFCE `< RD$250,000`):** `E320000000011`, `E320000000012`, `E320000000014`, `E320000000015` enviados a `https://fc.dgii.gov.do/certecf/recepcionfc/api/recepcion/ecf` con el `CodigoSeguridadeCF` de 6 caracteres derivado de su `ECF` firmado.
7. **Persistencia Completa en PostgreSQL (`{schema}.ecf` y `{schema}.rfce`):**
   - Deja el checklist de certificación en **7/7 (`ready: true`)** listo para producción.
