#!/usr/bin/env python3
"""test_unece_units.py — Tests du mapping unité libre → UN/ECE Rec 20.

Couvre TOUTES les clés du mapping (contrôle qu'aucune ne régresse), le piège
métier ``ml`` = mètre linéaire (``MTR``, pas ``MLT``), la robustesse aux accents
et à la casse, et le fallback ``C62`` pour toute unité inconnue.

Tests purs stdlib : s'exécutent dans tous les environnements (pas de dépendance
lourde stubbée).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from unece_units import DEFAULT_UNIT_CODE, _UNECE, to_unece


# ── Couverture exhaustive du mapping ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", sorted(_UNECE.items()))
def test_every_mapping_key_resolves(raw, expected):
    """Chaque clé déclarée dans _UNECE doit résoudre vers son code attendu."""
    assert to_unece(raw) == expected


# ── Piège métier central : ml = mètre linéaire ───────────────────────────────

@pytest.mark.parametrize("raw", ["ml", "ML", "mL", "Ml", "ml.", "m.l", " ml "])
def test_ml_is_metre_lineaire_not_millilitre(raw):
    """« ml » chez ces fournisseurs = mètre linéaire (MTR), jamais MLT."""
    assert to_unece(raw) == "MTR"
    assert to_unece(raw) != "MLT"


# ── m² / surface (cause n°1 des rejets IN-IPSO) ──────────────────────────────

@pytest.mark.parametrize(
    "raw", ["m2", "m²", "M2", "M²", "Mètre carré", "metre carre", "  m²  ", "mc"]
)
def test_surface_resolves_to_mtk(raw):
    """Toutes les graphies de mètre carré → MTK."""
    assert to_unece(raw) == "MTK"


# ── Robustesse : accents, casse, espaces, point final ────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("UNITÉ", "C62"), ("Unité", "C62"), ("PIÈCE", "C62"), ("Pièce.", "C62"),
        ("HEURE", "HUR"), ("Heure", "HUR"), ("  KG  ", "KGM"), ("Litre", "LTR"),
        ("Mètre", "MTR"), ("MÈTRE LINÉAIRE", "MTR"),
    ],
)
def test_accents_and_casing_normalized(raw, expected):
    assert to_unece(raw) == expected


# ── Fallback : unité inconnue / vide / None ──────────────────────────────────

@pytest.mark.parametrize("raw", ["", "   ", "blob", "xyz", "zzz", ".", None])
def test_unknown_falls_back_to_c62(raw):
    assert to_unece(raw) == DEFAULT_UNIT_CODE == "C62"


def test_custom_default_is_honoured():
    assert to_unece("totally-unknown", default="H87") == "H87"


def test_never_raises_on_weird_input():
    """to_unece ne doit jamais lever, quelle que soit l'entrée."""
    for weird in [None, "", "🤯", "123", "m³/h", "€/m²"]:
        assert isinstance(to_unece(weird), str)


# ── Idempotence : un code UN/ECE déjà valide est conservé (garde anti-C62) ────

@pytest.mark.parametrize("code", sorted(set(_UNECE.values())))
def test_valid_unece_code_is_preserved(code):
    """Gemini est prompté pour renvoyer un code UN/ECE : s'il le fait, le code
    DOIT être conservé, jamais retombé sur C62 ('MTK' reste 'MTK')."""
    assert to_unece(code) == code
    assert to_unece(code.lower()) == code
    assert to_unece(f"  {code}  ") == code
