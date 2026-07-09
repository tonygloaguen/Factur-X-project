#!/usr/bin/env python3
"""unece_units.py — Normalisation déterministe des unités vers UN/ECE Rec 20.

Le schematron EN16931 rejette tout ``BilledQuantity/@unitCode`` qui n'est pas
un code de la *UN/ECE Recommendation 20* (``@unitCode is not allowed``). Gemini
renvoie l'unité **en clair** telle qu'imprimée sur la facture (« m² », « UNI »,
« ml », « U »…) : ces valeurs brutes ne DOIVENT jamais atteindre le CII.

Ce module fournit la seule porte d'entrée autorisée — :func:`to_unece` — appelée
dans ``normalize_invoice_data`` avant la construction du XML. Le mapping est
volontairement déterministe (pas d'appel LLM) et ne lève jamais : toute unité
inconnue retombe sur ``C62`` (pièce), l'unité par défaut du pipeline.

Piège métier explicite : chez les fournisseurs menuisier/agenceur du parc,
« ml » désigne le **mètre linéaire** (``MTR``), PAS le millilitre (``MLT``).
"""
from __future__ import annotations

import logging
import unicodedata

logger = logging.getLogger("orchestrator")

# Unité par défaut du pipeline (pièce) — UN/ECE Rec 20.
DEFAULT_UNIT_CODE = "C62"

# Mapping unité libre (normalisée) → code UN/ECE Recommendation 20.
# Les clés sont déjà passées par _norm (ascii, minuscules, sans point final) ;
# n'ajouter que des clés sous cette forme normalisée.
_UNECE: dict[str, str] = {
    # ── Pièce / unité (C62) ──────────────────────────────────────────────────
    "piece": "C62", "pieces": "C62", "pce": "C62", "pc": "C62", "pcs": "C62",
    "p": "C62", "u": "C62", "un": "C62", "uni": "C62", "unite": "C62",
    "unites": "C62", "u.v": "C62", "uv": "C62", "": "C62",
    "ea": "C62", "each": "C62", "lot": "C62", "lots": "C62", "ens": "C62",
    "ensemble": "C62", "forfait": "C62", "fft": "C62", "qte": "C62",
    # ── Surface (MTK = mètre carré) — manquant n°1, cause des rejets IN-IPSO ──
    "m2": "MTK", "m²": "MTK", "metre carre": "MTK", "metres carres": "MTK",
    "metre carres": "MTK", "m carre": "MTK", "mc": "MTK",
    # ── Longueur (MTR = mètre) — « ml » = mètre LINÉAIRE, pas millilitre ──────
    "ml": "MTR", "m.l": "MTR", "ml.": "MTR", "m": "MTR", "metre": "MTR",
    "metres": "MTR", "metre lineaire": "MTR", "metres lineaires": "MTR",
    "metre lin": "MTR", "cm": "CMT", "mm": "MMT", "km": "KMT",
    # ── Volume ───────────────────────────────────────────────────────────────
    "m3": "MTQ", "metre cube": "MTQ", "l": "LTR", "litre": "LTR",
    "litres": "LTR", "cl": "CLT", "dl": "DLT",
    # ── Masse ────────────────────────────────────────────────────────────────
    "kg": "KGM", "kgs": "KGM", "kilo": "KGM", "kilos": "KGM",
    "kilogramme": "KGM", "g": "GRM", "gr": "GRM", "gramme": "GRM",
    "t": "TNE", "tonne": "TNE", "tonnes": "TNE",
    # ── Temps ────────────────────────────────────────────────────────────────
    "h": "HUR", "hr": "HUR", "heure": "HUR", "heures": "HUR",
    "jour": "DAY", "jours": "DAY", "j": "DAY", "mois": "MON", "an": "ANN",
    "annee": "ANN", "min": "MIN", "minute": "MIN",
    # ── Conditionnement ──────────────────────────────────────────────────────
    "paire": "PR", "paires": "PR", "boite": "XBX", "boites": "XBX",
    "carton": "XCT", "cartons": "XCT", "palette": "XPX", "palettes": "XPX",
    "rouleau": "NRL", "rouleaux": "NRL", "sac": "XBG", "sachet": "XBG",
    "pack": "XPK", "colis": "XPK", "jeu": "SET", "jeux": "SET", "set": "SET",
    # ── Pourcentage / divers ─────────────────────────────────────────────────
    "%": "P1", "pourcent": "P1",
}


# Ensemble des codes UN/ECE que ce module émet. Sert de garde d'idempotence :
# si Gemini renvoie DÉJÀ un code valide (« MTK », « KGM »…), on le conserve tel
# quel au lieu de le retomber bêtement sur C62 (« mtk » n'est pas une clé libre).
_VALID_CODES: frozenset[str] = frozenset(_UNECE.values())


def _norm(raw: str) -> str:
    """Normalise une unité libre : ascii, minuscules, sans espaces ni point final.

    Args:
        raw: Unité telle qu'extraite (peut contenir accents, casse, espaces).

    Returns:
        Chaîne normalisée servant de clé de lookup dans ``_UNECE``.
    """
    # « m² » contient l'exposant U+00B2 : NFKD le décompose en « m2 ».
    s = unicodedata.normalize("NFKD", raw or "")
    s = s.encode("ascii", "ignore").decode()
    return s.lower().strip().rstrip(".").strip()


def to_unece(raw: str, *, default: str = DEFAULT_UNIT_CODE) -> str:
    """Mappe une unité libre vers un code UN/ECE Rec 20.

    Ne lève jamais : une unité inconnue est journalisée et retombe sur
    ``default`` (``C62`` par défaut), garantissant un ``@unitCode`` toujours
    valide pour le schematron.

    Args:
        raw: Unité en clair issue de l'extraction (« m² », « UNI », « ml »…).
        default: Code de repli si l'unité est inconnue.

    Returns:
        Code UN/ECE Rec 20 (ex. ``"MTK"`` pour « m² », ``"MTR"`` pour « ml »).
    """
    # Garde d'idempotence : Gemini est prompté pour renvoyer un code UN/ECE ;
    # s'il l'a déjà fait correctement, on le conserve (« MTK » ne doit pas
    # devenir C62). N'accepte QUE les codes que ce module émet lui-même.
    if (raw or "").strip().upper() in _VALID_CODES:
        return (raw or "").strip().upper()

    key = _norm(raw)
    code = _UNECE.get(key)
    if code is None:
        logger.warning("unitCode inconnu '%s' (normalisé '%s') → fallback %s", raw, key, default)
        return default
    return code
