#!/usr/bin/env python3
"""test_totals_reconciliation.py — P1 : recalcul des totaux en Decimal.

Vérifie que ``normalize_invoice_data`` :
  - écrase TOUJOURS les totaux de Gemini par un recalcul depuis les lignes ;
  - recompose la ventilation TVA par taux (BR-S-08) ;
  - détecte les lignes exprimées en TTC et les reconvertit en HT ;
  - arrondit au centime en ROUND_HALF_UP (pas de dérive binaire du float).

Ces tests portent sur de la logique pure (pas de génération XML) : ils tournent
dans tous les environnements, y compris léger (stubs MagicMock pour lxml/facturx).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import facturx_utils as fx


def _inv(lignes, **totaux):
    inv = {
        "est_facture": True, "numero_facture": "F1", "date_facture": "2026-06-01",
        "type_facture": "380", "devise": "EUR",
        "vendeur": {"nom": "ACME", "tva_intra": "FR12345678901", "adresse_ligne1": "1 rue",
                    "code_postal": "75001", "ville": "Paris", "pays_code": "FR"},
        "acheteur": {"nom": "CLI", "adresse_ligne1": "2 av", "code_postal": "78114",
                     "ville": "Magny", "pays_code": "FR"},
        "lignes": lignes,
    }
    inv.update(totaux)
    return inv


def _line(net, rate=20.0, pu=None, qty=1, code="S"):
    return {"numero": "1", "description": "X", "quantite": qty, "unite": "C62",
            "prix_unitaire_ht": pu if pu is not None else net, "montant_net_ht": net,
            "taux_tva": rate, "code_tva": code}


def test_gemini_totals_overwritten_by_line_sum():
    inv = fx.normalize_invoice_data(_inv(
        [_line(30.0), _line(7.77)], montant_ht=999.0, montant_tva=1.0, montant_ttc=1234.0))
    assert inv["montant_ht"] == 37.77
    assert inv["montant_tva"] == 7.55
    assert inv["montant_ttc"] == 45.32


def test_vat_breakdown_recomputed_from_lines():
    inv = fx.normalize_invoice_data(_inv(
        [_line(100.0, 20.0), _line(50.0, 5.5)],
        # ventilation fournie par Gemini volontairement fausse → doit être écrasée
        ventilation_tva=[{"code_tva": "S", "taux": 20.0, "base_ht": 150.0, "montant_tva": 30.0}]))
    ventil = {v["taux"]: v for v in inv["ventilation_tva"]}
    assert set(ventil) == {20.0, 5.5}
    assert ventil[20.0]["montant_tva"] == 20.0
    assert ventil[5.5]["montant_tva"] == 2.75
    assert inv["montant_tva"] == 22.75


def test_ttc_lines_detected_and_reconverted():
    inv = fx.normalize_invoice_data(_inv(
        [_line(120.0, 20.0, pu=12.0, qty=10)],
        montant_ht=100.0, montant_tva=20.0, montant_ttc=120.0))
    assert inv["lignes"][0]["montant_net_ht"] == 100.0
    assert inv["montant_ht"] == 100.0
    assert inv["montant_ttc"] == 120.0


def test_ht_lines_not_wrongly_reconverted():
    """Lignes déjà en HT (net == HT, TTC > net) : PAS de reconversion."""
    inv = fx.normalize_invoice_data(_inv(
        [_line(100.0, 20.0)], montant_ht=100.0, montant_tva=20.0, montant_ttc=120.0))
    assert inv["lignes"][0]["montant_net_ht"] == 100.0
    assert inv["montant_ht"] == 100.0
    assert inv["montant_ttc"] == 120.0


def test_zero_vat_invoice_stable():
    """Facture exonérée (taux 0) : pas de reconversion TTC, totaux cohérents."""
    inv = fx.normalize_invoice_data(_inv(
        [_line(150.0, 0.0, code="Z")], montant_ht=150.0, montant_tva=0.0, montant_ttc=150.0))
    assert inv["montant_ht"] == 150.0
    assert inv["montant_tva"] == 0.0
    assert inv["montant_ttc"] == 150.0


def test_half_up_rounding():
    """0.125 × ... doit arrondir HALF_UP au centime, pas en banker's rounding."""
    # base 12.55 à 20 % = 2.51 exactement ; base 0.125 testée via net qui tombe sur .xx5
    inv = fx.normalize_invoice_data(_inv([_line(2.5, 20.0)]))  # 2.5 × 20% = 0.50
    assert inv["montant_tva"] == 0.50
    inv2 = fx.normalize_invoice_data(_inv([_line(0.025, 20.0)]))  # 0.025×20%=0.005 → 0.01
    assert inv2["montant_tva"] == 0.01
