#!/usr/bin/env python3
"""
Extracts an official DGII Set de Pruebas Excel file ({RNC}-{timestamp}.xlsx)
into an ordered JSON structure preserving column indices (col_idx), column letters,
and header names while stripping '#e' empty sentinel cells.
"""
from __future__ import annotations

import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def col_letter_to_index(col_str: str) -> int:
    idx = 0
    for ch in col_str:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx


def split_cell_ref(ref: str) -> tuple[str, int]:
    col_part = "".join(c for c in ref if c.isalpha())
    row_part = int("".join(c for c in ref if c.isdigit()))
    return col_part, row_part


def parse_xlsx_ordered(xlsx_path: Path) -> dict:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        shared_strings = []
        if "xl/sharedStrings.xml" in zf.namelist():
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall("main:si", NS):
                text_parts = [t.text or "" for t in si.findall(".//main:t", NS)]
                shared_strings.append("".join(text_parts))

        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels_root}

        result = {}
        for sheet in wb_root.findall(".//main:sheet", NS):
            name = sheet.attrib["name"]
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = rid_to_target[rid]
            if not target.startswith("xl/"):
                target = f"xl/{target}"

            sh_root = ET.fromstring(zf.read(target))
            rows_map: dict[int, dict[int, tuple[str, str]]] = {}

            for row_el in sh_root.findall(".//main:row", NS):
                r_idx = int(row_el.attrib["r"])
                rows_map[r_idx] = {}
                for c_el in row_el.findall("main:c", NS):
                    ref = c_el.attrib["r"]
                    col_let, _ = split_cell_ref(ref)
                    col_idx = col_letter_to_index(col_let)
                    t_attr = c_el.attrib.get("t", "")
                    v_el = c_el.find("main:v", NS)
                    is_el = c_el.find("main:is", NS)

                    val = ""
                    if t_attr == "s" and v_el is not None and v_el.text is not None:
                        val = shared_strings[int(v_el.text)]
                    elif t_attr == "inlineStr" and is_el is not None:
                        val = "".join(t.text or "" for t in is_el.findall(".//main:t", NS))
                    elif v_el is not None and v_el.text is not None:
                        val = v_el.text

                    rows_map[r_idx][col_idx] = (col_let, val.strip())

            headers = rows_map.get(1, {})
            sheet_rows = []
            for r_idx in sorted(k for k in rows_map.keys() if k > 1):
                row_cells = []
                for col_idx in sorted(rows_map[r_idx].keys()):
                    col_let, val = rows_map[r_idx][col_idx]
                    if val == "" or val == "#e":
                        continue
                    header_name = headers.get(col_idx, (col_let, f"COL_{col_idx}"))[1]
                    row_cells.append(
                        {
                            "col_idx": col_idx,
                            "col_let": col_let,
                            "header": header_name,
                            "val": val,
                        }
                    )
                if row_cells:
                    sheet_rows.append({"row_idx": r_idx, "cells": row_cells})
            result[name] = sheet_rows
        return result


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <input.xlsx> <output.json>", file=sys.stderr)
        sys.exit(1)
    xlsx_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    data = parse_xlsx_ordered(xlsx_path)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] Extracted {len(data.get('ECF', []))} ECF rows and {len(data.get('RFCE', []))} RFCE rows -> {out_path}")


if __name__ == "__main__":
    main()
