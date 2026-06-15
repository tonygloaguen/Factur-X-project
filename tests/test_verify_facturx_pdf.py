#!/usr/bin/env python3
"""
test_verify_facturx_pdf.py — Tests de l'outil tools/verify_facturx_pdf.py
==========================================================================

Vérifie le comportement de l'outil de preuve indépendante (extraction du XML
embarqué SANS validation, puis validation explicite XSD + schematron officiel).

Contrôle NÉGATIF central : un PDF dont le XML n'a ni BT-9 ni BT-20 (montant dû
positif) DOIT être rejeté par le schematron sur BR-CO-25 — et l'absence de XML
(None, None) ne doit JAMAIS être considérée comme un succès.

Nécessite les vraies libs (factur-x, lxml, fitz, saxonche), présentes en CI.
En environnement léger (stubs MagicMock), les tests sont ignorés.
"""
import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

ORCH = os.path.join(os.path.dirname(__file__), "..", "orchestrator")
TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "verify_facturx_pdf.py")
sys.path.insert(0, ORCH)

import facturx
import fitz
from lxml import etree as _etree

import facturx_utils as fx

_REAL_DEPS = (
    not isinstance(facturx, MagicMock)
    and not isinstance(fitz, MagicMock)
    and not isinstance(_etree, MagicMock)
    and hasattr(facturx, "xml_check_schematron")
)
pytestmark = pytest.mark.skipif(
    not _REAL_DEPS, reason="factur-x / lxml / fitz réels requis (stub MagicMock en local)"
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers : fabrication de PDF de test
# ─────────────────────────────────────────────────────────────────────────────

def _inv(**over):
    inv = {
        "est_facture": True, "numero_facture": "F2026-046", "date_facture": "2026-06-01",
        "type_facture": "380", "devise": "EUR",
        "vendeur": {"nom": "ACME", "nom_court": "ACME", "tva_intra": "FR12345678901",
                    "adresse_ligne1": "1 rue X", "code_postal": "75001",
                    "ville": "Paris", "pays_code": "FR"},
        "acheteur": {"nom": "CLIENT", "adresse_ligne1": "2 av Y",
                     "code_postal": "78114", "ville": "Magny", "pays_code": "FR"},
        "lignes": [{"numero": "1", "description": "P", "quantite": 1.0, "unite": "C62",
                    "prix_unitaire_ht": 500.0, "montant_net_ht": 500.0,
                    "taux_tva": 20.0, "code_tva": "S"}],
        "montant_ht": 500.0, "montant_tva": 100.0, "montant_ttc": 600.0,
    }
    inv.update(over)
    return inv


def _source_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "F2026-046")
    data = doc.tobytes()
    doc.close()
    return data


def _valid_facturx_pdf() -> bytes:
    """PDF Factur-X valide (montant dû positif → fallback BT-20)."""
    inv = fx.normalize_invoice_data(_inv())
    return fx.embed_facturx_in_pdf(_source_pdf(), fx.generate_facturx_xml_en16931(inv))


def _br_co_25_invalid_xml() -> bytes:
    """XML EN16931 avec montant dû positif mais SANS BT-9 ni BT-20."""
    inv = fx.normalize_invoice_data(_inv())
    inv["conditions_paiement"] = None
    inv["date_echeance"] = None
    return fx.generate_facturx_xml_en16931(inv)


def _br_co_25_invalid_pdf() -> bytes:
    """PDF embarquant le XML BR-CO-25 invalide (schematron désactivé à l'embed)."""
    return facturx.generate_from_binary(
        _source_pdf(), _br_co_25_invalid_xml(),
        flavor="factur-x", level="en16931",
        check_xsd=True, check_schematron=False,
    )


def _run_tool(pdf_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, TOOL, pdf_path],
        capture_output=True, text=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Contrôle négatif explicite (niveau bibliothèque)
# ─────────────────────────────────────────────────────────────────────────────

def test_schematron_raises_br_co_25_on_missing_bt9_bt20():
    """xml_check_schematron DOIT lever sur un XML sans BT-9 ni BT-20,
    avec 'BR-CO-25' explicitement présent dans le message d'erreur."""
    xml = _br_co_25_invalid_xml()
    with pytest.raises(Exception) as excinfo:
        facturx.xml_check_schematron(xml, flavor="factur-x", level="en16931")
    # Le motif BR-CO-25 doit être visible dans le message de l'exception levée.
    assert "BR-CO-25" in str(excinfo.value)


def test_none_none_extraction_is_not_success():
    """Un PDF sans XML embarqué renvoie (None, None) — jamais un succès."""
    xml_filename, xml_bytes = facturx.get_xml_from_pdf(
        _source_pdf(), check_xsd=False, check_schematron=False
    )
    assert not xml_filename and not xml_bytes


# ─────────────────────────────────────────────────────────────────────────────
# Comportement CLI de l'outil (codes de sortie + messages)
# ─────────────────────────────────────────────────────────────────────────────

def test_tool_ok_on_valid_pdf(tmp_path):
    p = tmp_path / "valid.pdf"
    p.write_bytes(_valid_facturx_pdf())
    r = _run_tool(str(p))
    assert r.returncode == 0, r.stderr
    assert "OK - PDF Factur-X valide XSD + schematron" in r.stdout


def test_tool_exit4_br_co_25_on_invalid(tmp_path):
    p = tmp_path / "br_co_25_ko.pdf"
    p.write_bytes(_br_co_25_invalid_pdf())
    r = _run_tool(str(p))
    assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)
    assert "Schematron invalid (BR-CO-25)" in r.stderr
    assert "BR-CO-25" in r.stderr


def test_tool_exit3_on_pdf_without_facturx(tmp_path):
    p = tmp_path / "plain.pdf"
    p.write_bytes(_source_pdf())
    r = _run_tool(str(p))
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert "PDF sans XML Factur-X embarqué" in r.stderr


def test_tool_exit2_on_missing_file(tmp_path):
    r = _run_tool(str(tmp_path / "does_not_exist.pdf"))
    assert r.returncode == 2
    assert "introuvable" in r.stderr
