#!/usr/bin/env python3
"""
test_br_co_25.py — Conditions de paiement (BT-20) & montant dû (BT-115)
=======================================================================

Règle schematron Factur-X visée :
  [BR-CO-25] Si le montant dû (BT-115) est positif, le document DOIT contenir
  soit l'échéance de paiement (BT-9), soit les conditions de paiement (BT-20).

Cas métier déclencheur :
  Facture F2026-046, 600,00 € TTC, montant dû positif, sans échéance ni
  conditions extraites → schematron KO sur BR-CO-25.

Couvre aussi BR-CO-16 (DuePayableAmount = GrandTotalAmount − TotalPrepaidAmount)
pour les factures partiellement/totalement réglées (acompte, acquittée).

Les tests de normalisation tournent sans dépendance lourde.
Les tests de génération XML sont ignorés si lxml n'est pas réellement installé
(stub MagicMock via conftest en environnement léger).
"""
import sys
import os
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import facturx_utils as fx

# La génération XML nécessite le vrai lxml (etree). En env léger, conftest le
# stubifie en MagicMock → on saute alors les tests qui produisent du XML.
from lxml import etree as _etree  # noqa: E402

_LXML_REAL = not isinstance(_etree, MagicMock)
_skip_xml = pytest.mark.skipif(not _LXML_REAL, reason="lxml non installé (stub MagicMock)")

RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inv(**over) -> dict:
    """Facture minimale valide (600 € TTC) ; surcharge via kwargs."""
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


def _payment_terms(xml: bytes) -> dict:
    root = _etree.fromstring(xml)
    pt = root.find(f".//{{{RAM}}}SpecifiedTradePaymentTerms")
    if pt is None:
        return {"present": False, "desc": None, "due": None, "first_is_desc": None}
    desc = pt.find(f"{{{RAM}}}Description")
    due = pt.find(f".//{{{UDT}}}DateTimeString")
    children = list(pt)
    return {
        "present": True,
        "desc": desc.text if desc is not None else None,
        "due": due.text if due is not None else None,
        # Ordre XSD CII : Description (BT-20) doit précéder DueDateDateTime (BT-9).
        "first_is_desc": children[0].tag.endswith("Description") if children else None,
    }


def _totals(xml: bytes) -> dict:
    root = _etree.fromstring(xml)

    def g(tag):
        el = root.find(f".//{{{RAM}}}{tag}")
        return el.text if el is not None else None

    return {
        "grand": g("GrandTotalAmount"),
        "prepaid": g("TotalPrepaidAmount"),
        "due": g("DuePayableAmount"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation — montant dû (BT-115) & conditions de paiement (BT-20)
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_terms_when_due_positive_and_no_due_date():
    """du>0, ni échéance ni conditions → BT-20 fallback ajouté (BR-CO-25)."""
    inv = fx.normalize_invoice_data(_inv())
    assert inv["montant_du"] == 600.0
    assert inv["conditions_paiement"] == fx.DEFAULT_PAYMENT_TERMS


def test_no_fallback_when_due_date_present():
    """Échéance (BT-9) présente → pas de fallback BT-20 imposé."""
    inv = fx.normalize_invoice_data(_inv(date_echeance="2026-07-01"))
    assert inv["conditions_paiement"] is None


def test_existing_terms_preserved():
    """Conditions de paiement déjà extraites → conservées telles quelles."""
    inv = fx.normalize_invoice_data(_inv(conditions_paiement="Paiement à 30 jours"))
    assert inv["conditions_paiement"] == "Paiement à 30 jours"


def test_existing_terms_preserved_even_with_due_date():
    """Conditions ET échéance présentes → les deux conservées."""
    inv = fx.normalize_invoice_data(
        _inv(conditions_paiement="Paiement à 30 jours", date_echeance="2026-07-01")
    )
    assert inv["conditions_paiement"] == "Paiement à 30 jours"
    assert inv["date_echeance"] == "2026-07-01"


def test_paid_invoice_zero_due_via_flag():
    """mention_acquittee + montant_du=0 → solde nul + BT-20 'Facture acquittée'."""
    inv = fx.normalize_invoice_data(_inv(mention_acquittee=True, montant_du=0.0))
    assert inv["montant_du"] == 0.0
    assert inv["conditions_paiement"] == fx.PAID_PAYMENT_TERMS


def test_paid_invoice_detected_from_text():
    """Mention textuelle fiable (notes) sans reste à payer → solde nul."""
    inv = fx.normalize_invoice_data(_inv(notes="Facture acquittée le 02/06/2026", montant_du=0.0))
    assert inv["montant_du"] == 0.0
    assert inv["conditions_paiement"] == fx.PAID_PAYMENT_TERMS


def test_positive_remaining_not_forced_to_zero():
    """Mention 'payée' MAIS reste à payer positif → ne pas forcer à 0."""
    inv = fx.normalize_invoice_data(_inv(notes="Acompte payé le 02/06", montant_du=300.0))
    assert inv["montant_du"] == 300.0  # reste positif conservé
    # du>0 sans échéance → fallback BT-20 (BR-CO-25)
    assert inv["conditions_paiement"] == fx.DEFAULT_PAYMENT_TERMS


def test_due_defaults_to_ttc_when_absent():
    """Aucun montant_du extrait → défaut = TTC (comportement existant)."""
    inv = fx.normalize_invoice_data(_inv())
    assert inv["montant_du"] == 600.0


# ─────────────────────────────────────────────────────────────────────────────
# Génération XML — BT-20 / BT-9 / BR-CO-16
# ─────────────────────────────────────────────────────────────────────────────

@_skip_xml
def test_xml_emits_bt20_description():
    inv = fx.normalize_invoice_data(_inv())
    pt = _payment_terms(fx.generate_facturx_xml_en16931(inv))
    assert pt["present"]
    assert pt["desc"] == fx.DEFAULT_PAYMENT_TERMS
    assert pt["due"] is None


@_skip_xml
def test_xml_emits_bt9_when_due_date():
    inv = fx.normalize_invoice_data(_inv(date_echeance="2026-07-01"))
    pt = _payment_terms(fx.generate_facturx_xml_en16931(inv))
    assert pt["due"] == "20260701"
    assert pt["desc"] is None


@_skip_xml
def test_xml_bt20_before_bt9_order():
    """Ordre XSD : Description (BT-20) avant DueDateDateTime (BT-9)."""
    inv = fx.normalize_invoice_data(
        _inv(conditions_paiement="Paiement à 30 jours", date_echeance="2026-07-01")
    )
    pt = _payment_terms(fx.generate_facturx_xml_en16931(inv))
    assert pt["desc"] == "Paiement à 30 jours"
    assert pt["due"] == "20260701"
    assert pt["first_is_desc"] is True


@_skip_xml
@pytest.mark.parametrize("over", [
    {},                                                # du = ttc
    {"mention_acquittee": True, "montant_du": 0.0},    # acquittée
    {"notes": "Acompte payé", "montant_du": 300.0},    # acompte partiel
    {"date_echeance": "2026-07-01"},                   # échéance
])
def test_xml_br_co_16_consistency(over):
    """DuePayableAmount = GrandTotalAmount − TotalPrepaidAmount (BR-CO-16)."""
    inv = fx.normalize_invoice_data(_inv(**over))
    t = _totals(fx.generate_facturx_xml_en16931(inv))
    grand = float(t["grand"])
    prepaid = float(t["prepaid"] or 0.0)
    due = float(t["due"])
    assert abs(due - (grand - prepaid)) < 0.005


@_skip_xml
def test_xml_paid_invoice_prepaid_equals_grand_total():
    """Facture acquittée : TotalPrepaidAmount = TTC, DuePayable = 0."""
    inv = fx.normalize_invoice_data(_inv(mention_acquittee=True, montant_du=0.0))
    t = _totals(fx.generate_facturx_xml_en16931(inv))
    assert t["prepaid"] == "600.00"
    assert t["due"] == "0.00"
