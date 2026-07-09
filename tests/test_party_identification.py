#!/usr/bin/env python3
"""test_party_identification.py — P4 : garde d'identification vendeur/acheteur.

``party_identification_error`` doit flaguer EXACTEMENT les cas que le schematron
officiel rejette (BR-CO-26 / BR-S-02 / BR-AE-02), pour router en Factures-Erreur
avant de produire un XML invalide (fournisseur étranger, autoliquidation).

Logique pure → tourne dans tous les environnements.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from facturx_utils import party_identification_error as err


def _inv(vendeur, acheteur=None, codes=("S",)):
    return {
        "vendeur": vendeur,
        "acheteur": acheteur or {"nom": "CLI"},
        "lignes": [{"numero": str(i + 1), "code_tva": c, "montant_net_ht": 100.0,
                    "taux_tva": 20.0 if c == "S" else 0.0} for i, c in enumerate(codes)],
    }


# ── Cas valides (guard = None) ───────────────────────────────────────────────

def test_seller_with_vat_ok():
    assert err(_inv({"nom": "ACME", "tva_intra": "FR12345678901"})) is None


def test_foreign_seller_with_vat_ok():
    assert err(_inv({"nom": "HAECKER", "tva_intra": "DE123456789", "pays_code": "DE"})) is None


def test_seller_siret_only_non_standard_ok():
    """SIRET seul suffit à BR-CO-26 si aucune ligne standard (ex. exonéré)."""
    assert err(_inv({"nom": "ACME", "siret": "12345678901234"}, codes=("E",))) is None


def test_reverse_charge_full_identification_ok():
    inv = _inv(
        {"nom": "SUB", "tva_intra": "FR99887766554"},
        acheteur={"nom": "CLI", "siret": "98765432101234"},
        codes=("AE",),
    )
    assert err(inv) is None


# ── Cas rejetés (guard renvoie un motif) ─────────────────────────────────────

def test_seller_without_any_id_flagged_br_co_26():
    msg = err(_inv({"nom": "ACME"}))
    assert msg and "BR-CO-26" in msg


def test_standard_line_without_seller_vat_flagged_br_s_02():
    """SIRET présent mais pas de TVA + ligne standard → BR-S-02."""
    msg = err(_inv({"nom": "ACME", "siret": "12345678901234"}, codes=("S",)))
    assert msg and "BR-S-02" in msg


def test_reverse_charge_without_seller_vat_flagged():
    msg = err(_inv({"nom": "SUB", "siret": "12345678901234"},
                   acheteur={"nom": "CLI", "siret": "98765432101234"}, codes=("AE",)))
    assert msg and "BR-AE-02" in msg


def test_reverse_charge_without_buyer_id_flagged():
    msg = err(_inv({"nom": "SUB", "tva_intra": "FR99887766554"},
                   acheteur={"nom": "CLI"}, codes=("AE",)))
    assert msg and "BR-AE-02" in msg
