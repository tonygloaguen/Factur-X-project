#!/usr/bin/env python3
"""test_reclasse.py — Logique pure de la migration (décision de déplacement).

Vérifie l'idempotence (fichier déjà bien rangé → no-op) et la construction du
plan de déplacement. Le parcours Drive / l'extraction XML (I/O) ne sont pas
testés ici (nécessitent des identifiants réels).
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from classify import classify
from supplier_registry import SupplierRegistry
from reclasse_existant import decide_move, _iso_date

SEED = Path(__file__).resolve().parent.parent / "suppliers_registry.seed.json"


@pytest.fixture
def registry(tmp_path):
    return SupplierRegistry.load(path=tmp_path / "reg.json", seed=SEED)


def _hacker_plan(registry):
    inv = {
        "date_facture": "2026-07-09", "numero_facture": "126181723",
        "vendeur": {"nom": "SARL JMT deco", "tva_intra": "FR41944684497"},
        "acheteur": {"nom": "Hacker", "tva_intra": "DE174736262"}, "montant_ttc": 1200.0,
    }
    return classify(inv, "Contremarque: FEUVRIER\nHacker DE174736262 JMT FR41944684497", registry)


def test_move_from_old_two_level_path(registry):
    plan = _hacker_plan(registry)
    move = decide_move("f1", "JMT_DECO_FacturX_2026-07-09_126181723.pdf",
                       "2026-07 Juillet/HACKER", plan)  # ancien chemin 2 niveaux + mauvais nom
    assert move.is_noop is False
    assert move.new_path == "2026-07 Juillet/FEUVRIER/HACKER"
    assert move.new_name == "HACKER_FacturX_2026-07-09_126181723.pdf"


def test_idempotent_when_already_well_placed(registry):
    plan = _hacker_plan(registry)
    move = decide_move("f1", plan.filename, "2026-07 Juillet/FEUVRIER/HACKER", plan)
    assert move.is_noop is True


def test_iso_date_from_cii_format():
    assert _iso_date("20260709") == "2026-07-09"
    assert _iso_date("") == ""
