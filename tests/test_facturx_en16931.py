import io
import os
import sys

import pytest
import fitz  # PyMuPDF
from reportlab.pdfgen import canvas

# Ajoute orchestrator/ au path pour importer facturx.py directement
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import facturx as facturx_module


def _make_minimal_pdf_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Facture test")
    c.drawString(72, 735, "Ligne: Prestation 100.00 EUR HT")
    c.showPage()
    c.save()
    return buf.getvalue()


def _minimal_invoice_data_en16931() -> dict:
    return {
        "est_facture": True,
        "numero_facture": "TEST-0001",
        "date_facture": "2026-02-24",
        "date_echeance": None,
        "type_facture": "380",
        "devise": "EUR",
        "vendeur": {
            "nom": "MSA FRANCE",
            "nom_court": "MSA",
            "siret": None,
            "siren": None,
            "tva_intra": None,
            "adresse_ligne1": "1 rue du Test",
            "adresse_ligne2": None,
            "code_postal": "75001",
            "ville": "Paris",
            "pays_code": "FR",
        },
        "acheteur": {
            "nom": "JMT DECO SARL",
            "siret": None,
            "tva_intra": None,
            "adresse_ligne1": "2 avenue Exemple",
            "code_postal": "78114",
            "ville": "Magny-les-Hameaux",
            "pays_code": "FR",
        },
        "lignes": [
            {
                "numero": "1",
                "description": "Prestation globale",
                "quantite": 1.0,
                "unite": "C62",
                "prix_unitaire_ht": 100.00,
                "montant_net_ht": 100.00,
                "taux_tva": 20.0,
                "code_tva": "S",
            }
        ],
        "ventilation_tva": [
            {
                "code_tva": "S",
                "taux": 20.0,
                "base_ht": 100.00,
                "montant_tva": 20.00,
            }
        ],
        "montant_total_lignes_net": 100.00,
        "montant_ht": 100.00,
        "montant_tva": 20.00,
        "montant_ttc": 120.00,
        "montant_du": 120.00,
        "reference_commande": None,
        "code_moyen_paiement": None,
        "iban": None,
        "bic": None,
        "notes": None,
    }


def test_pdf_to_facturx_en16931_embeds_xml():
    # Force le profil pour ce test (au cas où l'env diffère)
    facturx_module.FACTURX_PROFILE = "en16931"

    pdf_bytes = _make_minimal_pdf_bytes()
    inv = _minimal_invoice_data_en16931()

    xml_bytes = facturx_module.generate_facturx_xml_en16931(inv)
    assert xml_bytes.startswith(b"<?xml")

    facturx_pdf = facturx_module.embed_facturx_in_pdf(pdf_bytes, xml_bytes)

    # Vérifs de base PDF
    assert facturx_pdf[:4] == b"%PDF"

    # Vérifie qu'un fichier est embarqué (XML Factur-X)
    doc = fitz.open(stream=facturx_pdf, filetype="pdf")
    try:
        count = doc.embfile_count()
        assert count >= 1, "Aucun fichier embarqué trouvé dans le PDF Factur-X"

        names = [doc.embfile_info(i)["filename"] for i in range(count)]
        assert any(n.lower().endswith(".xml") for n in names), f"Fichier XML non trouvé. embfiles={names}"
    finally:
        doc.close()
