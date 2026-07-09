#!/usr/bin/env python3
"""
test_facturx_schematron.py — Validation OFFICIELLE Factur-X (XSD + Schematron)
==============================================================================

Contrairement à test_br_co_25.py (assertions structurelles sur l'arbre XML),
ce module exécute la **validation métier officielle** de la librairie
``factur-x`` (Akretion), qui rejoue le schematron EN16931 via le moteur
Saxon (``saxonche``). C'est la même validation que les plateformes de
réception de factures électroniques.

Deux chemins de validation sont couverts :
  1. XML seul         → ``facturx.xml_check_xsd`` + ``facturx.xml_check_schematron``
  2. PDF embarqué     → ``facturx.get_xml_from_pdf(..., check_xsd=True,
                          check_schematron=True)`` sur le PDF Factur-X généré.

Règle métier prioritaire vérifiée :
  [BR-CO-25] Si le montant dû (BT-115) est positif, le document DOIT contenir
  soit l'échéance (BT-9), soit les conditions de paiement (BT-20).

Un test de **contrôle négatif** retire BT-9 et BT-20 et vérifie que le
schematron officiel rejette bien le document (preuve que la validation est
réellement exécutée et discriminante, pas un no-op).

Ces tests nécessitent les vraies libs (factur-x, lxml, fitz, saxonche),
présentes en CI (requirements-ci.txt). En environnement léger où conftest
stubifie ces modules (MagicMock), les tests sont ignorés.
"""
import sys
import os
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import facturx
import fitz
from lxml import etree as _etree

import facturx_utils as fx

# Validation officielle indisponible si une dépendance est stubbée (env léger).
_REAL_DEPS = (
    not isinstance(facturx, MagicMock)
    and not isinstance(fitz, MagicMock)
    and not isinstance(_etree, MagicMock)
    and hasattr(facturx, "xml_check_schematron")
)
pytestmark = pytest.mark.skipif(
    not _REAL_DEPS, reason="factur-x / lxml / fitz réels requis (stub MagicMock en local)"
)

FLAVOR = "factur-x"
LEVEL = "en16931"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inv(**over) -> dict:
    inv = {
        "est_facture": True,
        "numero_facture": "F2026-046",
        "date_facture": "2026-06-01",
        "type_facture": "380",
        "devise": "EUR",
        "vendeur": {
            "nom": "ACME", "nom_court": "ACME", "tva_intra": "FR12345678901",
            "adresse_ligne1": "1 rue X", "code_postal": "75001",
            "ville": "Paris", "pays_code": "FR",
        },
        "acheteur": {
            "nom": "CLIENT", "adresse_ligne1": "2 av Y",
            "code_postal": "78114", "ville": "Magny", "pays_code": "FR",
        },
        "lignes": [{
            "numero": "1", "description": "Prestation", "quantite": 1.0,
            "unite": "C62", "prix_unitaire_ht": 500.0, "montant_net_ht": 500.0,
            "taux_tva": 20.0, "code_tva": "S",
        }],
        "montant_ht": 500.0, "montant_tva": 100.0, "montant_ttc": 600.0,
    }
    inv.update(over)
    return inv


def _make_source_pdf() -> bytes:
    """Génère un PDF source minimal (sans XML) via PyMuPDF — dispo en CI/local."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Facture F2026-046 - test schematron")
    data = doc.tobytes()
    doc.close()
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 1. Validation XML seul (XSD + schematron officiel)
# ─────────────────────────────────────────────────────────────────────────────

def test_schematron_xml_fallback_bt20_valid():
    """du>0 sans échéance → fallback BT-20 → XSD + schematron officiel OK."""
    inv = fx.normalize_invoice_data(_inv())
    assert inv["conditions_paiement"] == fx.DEFAULT_PAYMENT_TERMS
    xml = fx.generate_facturx_xml_en16931(inv)
    assert facturx.xml_check_xsd(xml, flavor=FLAVOR, level=LEVEL) is True
    assert facturx.xml_check_schematron(xml, flavor=FLAVOR, level=LEVEL) is True


def test_schematron_xml_paid_invoice_valid():
    """Facture acquittée : du=0 + TotalPrepaidAmount (BT-113) → schematron OK."""
    inv = fx.normalize_invoice_data(_inv(mention_acquittee=True, montant_du=0.0))
    assert inv["montant_du"] == 0.0
    xml = fx.generate_facturx_xml_en16931(inv)
    # Cohérence BR-CO-16 : prepaid = TTC quand dû = 0.
    assert b"TotalPrepaidAmount" in xml
    assert facturx.xml_check_xsd(xml, flavor=FLAVOR, level=LEVEL) is True
    assert facturx.xml_check_schematron(xml, flavor=FLAVOR, level=LEVEL) is True


def test_schematron_xml_partial_payment_valid():
    """Acompte : du<TTC → TotalPrepaidAmount partiel → schematron OK (BR-CO-16)."""
    inv = fx.normalize_invoice_data(_inv(montant_du=300.0))
    xml = fx.generate_facturx_xml_en16931(inv)
    assert facturx.xml_check_schematron(xml, flavor=FLAVOR, level=LEVEL) is True


def test_schematron_negative_control_rejects_missing_bt9_bt20():
    """Contrôle négatif : sans BT-9 ni BT-20 (du>0), le schematron officiel
    DOIT rejeter le document sur BR-CO-25 (preuve d'exécution réelle)."""
    inv = fx.normalize_invoice_data(_inv())
    # On force l'absence des deux postes (état d'avant correctif).
    inv["conditions_paiement"] = None
    inv["date_echeance"] = None
    xml = fx.generate_facturx_xml_en16931(inv)
    # XSD reste valide (BR-CO-25 est une règle schematron, pas XSD).
    assert facturx.xml_check_xsd(xml, flavor=FLAVOR, level=LEVEL) is True
    with pytest.raises(Exception, match="BR-CO-25"):
        facturx.xml_check_schematron(xml, flavor=FLAVOR, level=LEVEL)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Validation PDF embarqué (XSD + schematron sur le XML extrait du PDF)
# ─────────────────────────────────────────────────────────────────────────────

def test_schematron_pdf_embedded_valid():
    """Pipeline complet : génération XML → embedding PDF/A-3 → extraction +
    validation officielle XSD + schematron du XML EMBARQUÉ dans le PDF."""
    inv = fx.normalize_invoice_data(_inv())
    xml = fx.generate_facturx_xml_en16931(inv)
    embedded_pdf = fx.embed_facturx_in_pdf(_make_source_pdf(), xml)
    assert embedded_pdf[:4] == b"%PDF"
    # get_xml_from_pdf valide XSD + schematron par défaut ; lève si invalide.
    facturx.get_xml_from_pdf(embedded_pdf, check_xsd=True, check_schematron=True)


def test_schematron_pdf_embedded_paid_invoice_valid():
    """PDF embarqué d'une facture acquittée → schematron officiel OK."""
    inv = fx.normalize_invoice_data(_inv(mention_acquittee=True, montant_du=0.0))
    xml = fx.generate_facturx_xml_en16931(inv)
    embedded_pdf = fx.embed_facturx_in_pdf(_make_source_pdf(), xml)
    facturx.get_xml_from_pdf(embedded_pdf, check_xsd=True, check_schematron=True)


# ─────────────────────────────────────────────────────────────────────────────
# 3. P0 — unitCode UN/ECE Rec 20 : cas cible IN-IPSO (facturé au m²)
# ─────────────────────────────────────────────────────────────────────────────

def _inv_m2(**over) -> dict:
    """Facture type IN-IPSO : mélaminé facturé au m² (unité brute « m² »)."""
    return _inv(
        numero_facture="FPA0002846",
        lignes=[{
            "numero": "1", "description": "Panneau mélaminé", "quantite": 12.5,
            "unite": "m²", "prix_unitaire_ht": 24.0, "montant_net_ht": 300.0,
            "taux_tva": 20.0, "code_tva": "S",
        }],
        montant_ht=300.0, montant_tva=60.0, montant_ttc=360.0,
        **over,
    )


def test_schematron_m2_invoice_maps_to_mtk_and_validates():
    """Cas cible IN-IPSO : « m² » brut → MTK après normalize → schematron OK.

    C'est la non-régression du rejet « @unitCode is not allowed » (252 occ.).
    """
    inv = fx.normalize_invoice_data(_inv_m2())
    assert inv["lignes"][0]["unite"] == "MTK"  # m² mappé avant CII
    xml = fx.generate_facturx_xml_en16931(inv)
    assert b'unitCode="MTK"' in xml
    assert facturx.xml_check_xsd(xml, flavor=FLAVOR, level=LEVEL) is True
    assert facturx.xml_check_schematron(xml, flavor=FLAVOR, level=LEVEL) is True


def test_schematron_negative_control_raw_unit_is_rejected():
    """Contrôle négatif : l'unité BRUTE « m² » (état d'avant P0), injectée
    dans le CII SANS passer par normalize, DOIT être rejetée par le schematron
    (« @unitCode is not allowed ») — preuve que c'est bien le mapping qui corrige.
    """
    raw = _inv_m2()
    # On saute volontairement normalize_invoice_data : l'unité reste « m² ».
    assert raw["lignes"][0]["unite"] == "m²"
    xml = fx.generate_facturx_xml_en16931(raw)
    assert 'unitCode="m²"'.encode() in xml
    with pytest.raises(Exception, match="unitCode"):
        facturx.xml_check_schematron(xml, flavor=FLAVOR, level=LEVEL)


# ─────────────────────────────────────────────────────────────────────────────
# 4. P1 — Recalcul des totaux en Decimal (BR-CO-13 / BR-S-08 / BR-CO-15)
# ─────────────────────────────────────────────────────────────────────────────

def _valid(xml) -> bool:
    return (
        facturx.xml_check_xsd(xml, flavor=FLAVOR, level=LEVEL) is True
        and facturx.xml_check_schematron(xml, flavor=FLAVOR, level=LEVEL) is True
    )


def test_p1_incoherent_gemini_totals_are_overwritten():
    """Les totaux incohérents de Gemini sont écrasés par le recalcul Decimal →
    cohérence BR-CO-13/BR-CO-15 et schematron OK."""
    inv = fx.normalize_invoice_data(_inv(
        lignes=[
            {"numero": "1", "description": "A", "quantite": 3, "unite": "C62",
             "prix_unitaire_ht": 10.0, "montant_net_ht": 30.0, "taux_tva": 20.0, "code_tva": "S"},
            {"numero": "2", "description": "B", "quantite": 1, "unite": "C62",
             "prix_unitaire_ht": 7.77, "montant_net_ht": 7.77, "taux_tva": 20.0, "code_tva": "S"},
        ],
        montant_ht=999.0, montant_tva=1.0, montant_ttc=1234.0,  # valeurs fausses
    ))
    assert inv["montant_ht"] == 37.77
    assert inv["montant_tva"] == 7.55   # 37.77 × 20 % arrondi HALF_UP
    assert inv["montant_ttc"] == 45.32
    assert _valid(fx.generate_facturx_xml_en16931(inv))


def test_p1_ttc_lines_are_reconverted_to_ht():
    """Lignes exprimées en TTC (Leroy Merlin chantier) → reconverties en HT
    avant agrégation → schematron OK."""
    inv = fx.normalize_invoice_data(_inv(
        lignes=[{"numero": "1", "description": "Ciment", "quantite": 10, "unite": "C62",
                 "prix_unitaire_ht": 12.0, "montant_net_ht": 120.0, "taux_tva": 20.0, "code_tva": "S"}],
        montant_ht=100.0, montant_tva=20.0, montant_ttc=120.0,  # net ligne == TTC
    ))
    assert inv["montant_ht"] == 100.0          # 120 / 1.20
    assert inv["lignes"][0]["montant_net_ht"] == 100.0
    assert inv["montant_ttc"] == 120.0
    assert _valid(fx.generate_facturx_xml_en16931(inv))


def test_p1_multi_rate_vat_breakdown_valid():
    """Ventilation TVA multi-taux recalculée depuis les lignes → BR-S-08 OK."""
    inv = fx.normalize_invoice_data(_inv(
        lignes=[
            {"numero": "1", "description": "Std", "quantite": 1, "unite": "C62",
             "prix_unitaire_ht": 100.0, "montant_net_ht": 100.0, "taux_tva": 20.0, "code_tva": "S"},
            {"numero": "2", "description": "Réduit", "quantite": 1, "unite": "C62",
             "prix_unitaire_ht": 50.0, "montant_net_ht": 50.0, "taux_tva": 5.5, "code_tva": "S"},
        ],
    ))
    assert inv["montant_tva"] == 22.75         # 20 + 2.75
    assert inv["montant_ttc"] == 172.75
    assert len(inv["ventilation_tva"]) == 2
    assert _valid(fx.generate_facturx_xml_en16931(inv))
