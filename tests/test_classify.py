#!/usr/bin/env python3
"""test_classify.py — Calcul du triplet mois/contremarque/fournisseur.

Cas exigés par le brief : piège Häcker (chemin cible exact), exception
Raison Home (émetteur vs enseigne), vente JMT (contremarque = client), avoir
GPDIS (préfixe AVOIR_), multi-contremarque dominante, fallback _A_CLASSER.
Logique pure (stdlib) → tourne partout.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from classify import classify, is_credit_note, month_folder
from supplier_registry import SupplierRegistry

SEED = Path(__file__).resolve().parent.parent / "suppliers_registry.seed.json"


@pytest.fixture
def registry(tmp_path):
    return SupplierRegistry.load(path=tmp_path / "reg.json", seed=SEED)


def _inv(**over):
    inv = {
        "date_facture": "2026-07-09", "numero_facture": "F1", "type_facture": "380",
        "vendeur": {"nom": "Fournisseur", "tva_intra": ""},
        "acheteur": {"nom": "JMT", "tva_intra": "FR41944684497"},
        "montant_ttc": 100.0, "lignes": [],
    }
    inv.update(over)
    return inv


# ── Piège Häcker : chemin cible exact du brief ───────────────────────────────

def test_hacker_exact_target_path(registry):
    inv = _inv(
        numero_facture="126181723",
        vendeur={"nom": "SARL JMT deco", "tva_intra": "FR41944684497"},  # en-tête = acheteur
        acheteur={"nom": "Hacker", "tva_intra": "DE174736262"},
        montant_ttc=1200.0,
    )
    c = classify(inv, "Contremarque: FEUVRIER\nHacker DE174736262 JMT FR41944684497", registry)
    assert c.folder_path == "2026-07 Juillet/FEUVRIER/HACKER"
    assert c.filename == "HACKER_FacturX_2026-07-09_126181723.pdf"
    assert c.route == "normal"


# ── Exception Communication (émetteur = franchiseur par ID fort) ──────────────

def test_raison_home_emitter_routes_to_communication(registry):
    inv = _inv(vendeur={"nom": "Raison Home", "tva_intra": "FR88428155956"})
    c = classify(inv, "Raison Home franchiseur FR88428155956 JMT FR41944684497", registry)
    assert c.route == "communication"
    assert c.folder_path == "2026-07 Juillet/Communication"


def test_raison_home_brand_mention_does_not_trigger_exception(registry):
    """« RAISON HOME » accolé à JMT comme client ne déclenche PAS l'exception."""
    inv = _inv(
        vendeur={"nom": "IN-IPSO", "tva_intra": "FR28753043371"},
        acheteur={"nom": "JMT Deco Raison Home", "tva_intra": "FR41944684497"},
    )
    c = classify(inv, "IN-IPSO FR28753043371 Contremarque: DUPONT\nJMT Deco Raison Home FR41944684497", registry)
    assert c.route == "normal"
    assert c.fournisseur == "IN-IPSO"


# ── Vente JMT : contremarque = destinataire, fournisseur = JMT_DECO ───────────

def test_jmt_sale_contremarque_is_client_family_name(registry):
    inv = _inv(
        numero_facture="F-100",
        vendeur={"nom": "Societe JMT Deco", "tva_intra": "FR41944684497"},
        acheteur={"nom": "Aurelie SCHWEITZER"},
    )
    c = classify(inv, "JMT Deco FR41944684497 acompte client Aurelie SCHWEITZER", registry)
    assert c.is_self_sale is True
    assert c.fournisseur == "JMT_DECO"
    assert c.contremarque == "SCHWEITZER"   # nom de famille


# ── Avoir GPDIS : préfixe AVOIR_ ─────────────────────────────────────────────

def test_gpdis_credit_note_prefixed(registry):
    inv = _inv(
        numero_facture="RCL000962826", type_facture="381",
        vendeur={"nom": "GPDIS", "tva_intra": "FR64327127247"}, montant_ttc=-717.84,
    )
    c = classify(inv, "GPDIS FR64327127247 V/Ref: DURAND\nclient JMT FR41944684497", registry)
    assert c.filename.startswith("AVOIR_")
    assert c.fournisseur == "GPDIS"
    assert c.contremarque == "DURAND"


def test_is_credit_note_detection():
    assert is_credit_note({"type_facture": "381"}) is True
    assert is_credit_note({"montant_ttc": -5.0}) is True
    assert is_credit_note({"type_facture": "380", "montant_ttc": 5.0}) is False


# ── Multi-contremarque : dominante par montant ───────────────────────────────

def test_multiple_contremarques_picks_dominant(registry):
    inv = _inv(
        numero_facture="MJH-1",
        vendeur={"nom": "Menuiserie JH"},
        lignes=[
            {"description": "SAV GRAIRE reparation", "montant_net_ht": 100.0},
            {"description": "SAV MARTINEAU pose", "montant_net_ht": 900.0},
        ],
    )
    c = classify(inv, "Menuiserie JH SIREN 978827517 JMT FR41944684497", registry)
    assert c.contremarque == "MARTINEAU"          # 900 > 100
    assert "GRAIRE" in c.alternates


# ── Contremarque introuvable → _A_CLASSER ────────────────────────────────────

def test_missing_contremarque_falls_back(registry):
    inv = _inv(vendeur={"nom": "IN-IPSO", "tva_intra": "FR28753043371"})
    c = classify(inv, "IN-IPSO FR28753043371 aucune reference client JMT FR41944684497", registry)
    assert c.route == "a_classer"
    assert c.folder_path == "2026-07 Juillet/_A_CLASSER"
    assert "contremarque introuvable" in c.warnings


def test_never_uses_raison_home_as_contremarque(registry):
    inv = _inv(vendeur={"nom": "IN-IPSO", "tva_intra": "FR28753043371"})
    c = classify(inv, "IN-IPSO FR28753043371 Contremarque: RAISON HOME", registry)
    # 'RAISON HOME' est blacklistée → pas retenue → _A_CLASSER.
    assert c.contremarque != "RAISON_HOME"
    assert c.route == "a_classer"


def test_month_folder_format():
    assert month_folder({"date_facture": "2026-07-09"}) == "2026-07 Juillet"
    assert month_folder({"date_facture": "2026-01-01"}) == "2026-01 Janvier"
