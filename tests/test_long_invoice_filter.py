#!/usr/bin/env python3
"""
test_long_invoice_filter.py — Tests : factures longues (filtre + troncature Gemini)
====================================================================================

Cas métier déclencheur :
  Facture RAISON HOME F2026-044 (JMT Déco, 19 pages, 34 228 chars, 22 035 € TTC)
  rejetée par le filtre local avec text_too_long car MAX_TEXT_LEN_FOR_INVOICE
  valait 30 000. De plus, la troncature naïve [:8000] envoyée à Gemini coupait
  les totaux ("Total affaire TTC" en position ~31 400, dernières pages).

Règles testées :
  1. is_invoice_candidate accepte une vraie facture détaillée de ~35 000 chars
  2. is_invoice_candidate rejette toujours les catalogues (> 60 000 chars)
  3. _truncate_ocr_for_gemini renvoie le texte court intact
  4. _truncate_ocr_for_gemini préserve tête (n°, date) ET queue (totaux)
  5. La sortie tronquée reste bornée (head + tail + marqueur)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from facturx_utils import (
    GEMINI_OCR_HEAD_CHARS,
    GEMINI_OCR_TAIL_CHARS,
    MAX_TEXT_LEN_FOR_INVOICE,
    _truncate_ocr_for_gemini,
    is_invoice_candidate,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _facture_longue(n_chars: int) -> str:
    """Simule une facture détaillée multi-pages : en-tête réel, corps de lignes
    d'aménagement, totaux en fin de document (comme RAISON HOME F2026-044)."""
    header = (
        "Société JMT Déco — Franchisé RAISON Home\n"
        "SIRET 944 684 497 00010 — TVA FR41944684497\n"
        "Facture N°F2026-044 du 28/05/2026\n"
        "Madame Florence LEROY\n"
    )
    footer = (
        "Total affaire TTC : 22 035,57 €\n"
        "Dont TVA (10,00 %) Base HT : 17 206,91 € 1 720,69 €\n"
        "Paiement N°1 Acompte à la commande de 8 814,23 € TTC\n"
        "Montant total numéro échéance IBAN\n"
    )
    ligne = "12 BD60C3Y 1 10,00 % 481,68 € 529,85 € Meuble bas 2 tiroirs\n"
    body_len = n_chars - len(header) - len(footer)
    body = (ligne * (body_len // len(ligne) + 1))[:body_len]
    return header + body + footer


# ─────────────────────────────────────────────────────────────────────────────
# 1-2. Filtre local : factures longues vs catalogues
# ─────────────────────────────────────────────────────────────────────────────

def test_facture_detaillee_35k_acceptee():
    """RAISON HOME F2026-044 (34 228 chars) doit passer le filtre local."""
    text = _facture_longue(35_000)
    ok, reason = is_invoice_candidate(text)
    assert ok, f"facture détaillée rejetée : {reason}"


def test_catalogue_au_dela_de_60k_rejete():
    """Un catalogue/tarif (ex : TARIF IN-IPSO, 382 000 chars) reste rejeté."""
    text = "tarif référence prix conseillé " * 15_000  # ~465 000 chars
    ok, reason = is_invoice_candidate(text)
    assert not ok
    assert reason.startswith("text_too_long:")


def test_seuil_max_text_len():
    """Le seuil doit couvrir les factures détaillées connues (≥ 35 000)."""
    assert MAX_TEXT_LEN_FOR_INVOICE >= 35_000


# ─────────────────────────────────────────────────────────────────────────────
# 3-5. Troncature tête + queue pour Gemini
# ─────────────────────────────────────────────────────────────────────────────

def test_texte_court_intact():
    text = "facture n°123 total ttc 100,00 € tva 20%"
    assert _truncate_ocr_for_gemini(text) == text


def test_texte_vide_et_none():
    assert _truncate_ocr_for_gemini("") == ""
    assert _truncate_ocr_for_gemini(None) == ""


def test_tete_et_queue_preservees():
    """Sur une facture longue, l'en-tête (n°, date) ET les totaux finaux
    doivent figurer dans le texte envoyé à Gemini."""
    text = _facture_longue(35_000)
    out = _truncate_ocr_for_gemini(text)
    # En-tête (début du document)
    assert "Facture N°F2026-044" in out
    assert "Florence LEROY" in out
    # Totaux (fin du document — perdus avec l'ancienne troncature [:8000])
    assert "Total affaire TTC : 22 035,57 €" in out
    assert "Paiement N°1" in out
    # Marqueur de coupure visible pour Gemini
    assert "document tronqué" in out


def test_sortie_tronquee_bornee():
    """La sortie ne doit pas dépasser head + tail + marqueur (~100 chars)."""
    text = _facture_longue(400_000)
    out = _truncate_ocr_for_gemini(text)
    assert len(out) <= GEMINI_OCR_HEAD_CHARS + GEMINI_OCR_TAIL_CHARS + 100


def test_limite_exacte_pas_de_troncature():
    """Texte exactement à la limite head+tail : renvoyé intact, sans marqueur."""
    limit = GEMINI_OCR_HEAD_CHARS + GEMINI_OCR_TAIL_CHARS
    text = "x" * limit
    assert _truncate_ocr_for_gemini(text) == text
