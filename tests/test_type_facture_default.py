#!/usr/bin/env python3
"""test_type_facture_default.py — P3 : robustesse type_facture / devise = None.

Gemini renvoie fréquemment ``type_facture: null`` (12 occurrences observées).
Le défaut d'un champ Pydantic ne s'applique QUE si la clé est absente, pas si
elle vaut None → sans validator ``before``, model_validate lève et TOUTE la
coercition est perdue (nodes.py conserve alors les données brutes).

Deux niveaux de protection sont testés :
  - schéma GeminiInvoiceOutput (validator before) — nécessite pydantic réel ;
  - garde de normalize_invoice_data — tourne partout.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import facturx_utils as fx


# ── Niveau schéma (pydantic réel requis) ─────────────────────────────────────

import pydantic  # noqa: E402

_REAL_PYDANTIC = not isinstance(pydantic, MagicMock) and hasattr(pydantic, "BaseModel")
_schema_only = pytest.mark.skipif(
    not _REAL_PYDANTIC, reason="pydantic réel requis (stub MagicMock en env léger)"
)


@_schema_only
@pytest.mark.parametrize(
    "raw,expected",
    [(None, "380"), ("", "380"), ("999", "380"), ("avoir", "380"),
     ("380", "380"), ("381", "381"), ("384", "384"), ("389", "389"), (380, "380")],
)
def test_schema_type_facture_defaults(raw, expected):
    from schemas import GeminiInvoiceOutput
    d = GeminiInvoiceOutput.model_validate({"est_facture": True, "type_facture": raw}).model_dump()
    assert d["type_facture"] == expected


@_schema_only
def test_schema_none_does_not_lose_coercion():
    """type_facture=None ne doit PAS faire échouer la validation : la coercition
    des autres champs (bool, float, list) doit rester appliquée."""
    from schemas import GeminiInvoiceOutput
    d = GeminiInvoiceOutput.model_validate({
        "est_facture": "oui", "type_facture": None, "devise": None,
        "montant_ttc": "pas un nombre", "lignes": None,
    }).model_dump()
    assert d["est_facture"] is True
    assert d["montant_ttc"] == 0.0
    assert d["lignes"] == []
    assert d["type_facture"] == "380"
    assert d["devise"] == "EUR"


# ── Niveau normalize (tourne partout) ────────────────────────────────────────

def _minimal(**over):
    inv = {"est_facture": True, "lignes": [
        {"numero": "1", "description": "X", "quantite": 1, "unite": "C62",
         "prix_unitaire_ht": 10.0, "montant_net_ht": 10.0, "taux_tva": 20.0, "code_tva": "S"}]}
    inv.update(over)
    return inv


@pytest.mark.parametrize("raw", [None, ""])
def test_normalize_forces_type_facture_default(raw):
    inv = fx.normalize_invoice_data(_minimal(type_facture=raw))
    assert inv["type_facture"] == "380"


@pytest.mark.parametrize("raw", [None, ""])
def test_normalize_forces_devise_default(raw):
    inv = fx.normalize_invoice_data(_minimal(devise=raw))
    assert inv["devise"] == "EUR"


def test_normalize_preserves_valid_type_and_currency():
    inv = fx.normalize_invoice_data(_minimal(type_facture="381", devise="USD"))
    assert inv["type_facture"] == "381"
    assert inv["devise"] == "USD"
