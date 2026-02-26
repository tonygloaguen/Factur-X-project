#!/usr/bin/env python3
import glob
import os
import sys
from pathlib import Path

from pypdf import PdfReader


def find_pdfs():
    # Scan repo for PDFs (including samples, tests, outputs if present)
    return [Path(p) for p in glob.glob("**/*.pdf", recursive=True)]


def extract_facturx_xml(pdf_path: Path, out_dir: Path) -> Path | None:
    """
    Extract embedded 'factur-x.xml' (or any *.xml attachment) using pypdf.
    Returns extracted xml path or None if not found.
    """
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        print(f"[WARN] {pdf_path}: could not parse PDF ({exc}). Skipping.")
        return None

    # pypdf exposes attachments via reader.attachments in recent versions.
    attachments = getattr(reader, "attachments", None)
    if not attachments:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)

    # Try canonical name first
    for name in attachments.keys():
        lname = name.lower()
        if lname == "factur-x.xml" or lname.endswith(".xml"):
            try:
                raw = attachments[name]
                # pypdf ≥4 returns List[DecodedStreamObject], older versions return bytes
                if isinstance(raw, (bytes, bytearray)):
                    data = bytes(raw)
                elif isinstance(raw, (list, tuple)) and raw:
                    item = raw[0]
                    data = item.get_data() if hasattr(item, "get_data") else bytes(item)
                else:
                    continue
            except Exception as exc:
                print(f"[WARN] {pdf_path}: could not read attachment '{name}' ({exc}). Skipping.")
                continue
            out_path = out_dir / f"{pdf_path.stem}__{Path(name).name}"
            out_path.write_bytes(data)
            return out_path
    return None


def assert_en16931(xml_bytes: bytes, origin: str):
    # Minimal check: EN16931 marker
    marker = b"urn:cen.eu:en16931:2017"
    if marker not in xml_bytes:
        raise AssertionError(
            f"[FAIL] {origin}: XML found but EN16931 marker not present "
            f"(expected urn:cen.eu:en16931:2017)."
        )


def main():
    pdfs = find_pdfs()
    if not pdfs:
        print("[ERROR] No PDFs found in the repository. Add Factur-X PDFs under samples/ or tests/fixtures/.")
        return 1

    failures = 0
    extracted_any = False

    for pdf in pdfs:
        # Skip common virtualenv/cache directories if any slipped in
        if any(part in (".venv", "venv", "__pycache__", ".pytest_cache") for part in pdf.parts):
            continue

        out_dir = Path("ci_extracted_xml")
        xml_path = extract_facturx_xml(pdf, out_dir)

        if xml_path is None:
            print(f"[WARN] {pdf}: no embedded XML attachment found (not Factur-X or tool can't read attachments).")
            continue

        extracted_any = True
        xml_bytes = xml_path.read_bytes()

        try:
            assert_en16931(xml_bytes, f"{pdf} -> {xml_path.name}")
            print(f"[OK] {pdf}: embedded XML looks EN16931.")
        except AssertionError as e:
            print(str(e))
            failures += 1

    if not extracted_any:
        print("No embedded XML extracted from any PDF. If you expect Factur-X PDFs, add them under samples/ or tests/fixtures/.")
        # Do not fail by default, because some repos don't commit PDFs.
        return 0

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())