#!/usr/bin/env python3
"""
test_pdf_attachment.py — Tests unitaires : détection des pièces jointes PDF Gmail
==================================================================================

Couvre la fonction is_pdf_attachment() de facturx_utils.

Cas métier déclencheur :
  Facture Eberhardt 2026014899.pdf envoyée avec Content-Type: application/octet-stream.
  Le pipeline doit l'accepter car le filename finit par .pdf.

Règles testées :
  1. application/pdf + filename .pdf            → accepté (MIME)
  2. application/octet-stream + filename .pdf   → accepté (extension fallback)
  3. application/octet-stream + filename .bin   → refusé
  4. text/plain + filename .txt                 → refusé
  5. Filename en majuscules FACTURE.PDF          → accepté (insensible à la casse)
  6. application/pdf sans filename              → accepté (MIME suffit)
  7. Filename vide + MIME non-PDF               → refusé sans crash
  8. Filename None + MIME non-PDF               → refusé sans crash
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from facturx_utils import is_pdf_attachment


def _part(mime: str, filename: str | None = "") -> dict:
    """Construit un dict simulant une partie de message Gmail."""
    return {"mimeType": mime, "filename": filename}


# ─────────────────────────────────────────────────────────────────────────────
# Cas acceptés
# ─────────────────────────────────────────────────────────────────────────────

def test_application_pdf_with_filename():
    """mimeType application/pdf + filename .pdf → accepté."""
    assert is_pdf_attachment(_part("application/pdf", "facture.pdf")) is True


def test_application_pdf_without_filename():
    """mimeType application/pdf sans filename → accepté (MIME suffit)."""
    assert is_pdf_attachment(_part("application/pdf", "")) is True


def test_application_pdf_filename_none():
    """mimeType application/pdf, filename None → accepté sans crash."""
    assert is_pdf_attachment(_part("application/pdf", None)) is True


def test_octet_stream_pdf_filename():
    """mimeType application/octet-stream + filename 2026014899.pdf → accepté (cas Eberhardt)."""
    assert is_pdf_attachment(_part("application/octet-stream", "2026014899.pdf")) is True


def test_octet_stream_pdf_generic_name():
    """application/octet-stream + filename facture.pdf → accepté."""
    assert is_pdf_attachment(_part("application/octet-stream", "facture.pdf")) is True


def test_pdf_filename_uppercase():
    """Filename FACTURE.PDF (majuscules) → accepté (insensible à la casse)."""
    assert is_pdf_attachment(_part("application/octet-stream", "FACTURE.PDF")) is True


def test_pdf_filename_mixed_case():
    """Filename Facture_2026.Pdf → accepté."""
    assert is_pdf_attachment(_part("application/octet-stream", "Facture_2026.Pdf")) is True


# ─────────────────────────────────────────────────────────────────────────────
# Cas refusés
# ─────────────────────────────────────────────────────────────────────────────

def test_octet_stream_bin_filename():
    """application/octet-stream + filename document.bin → refusé."""
    assert is_pdf_attachment(_part("application/octet-stream", "document.bin")) is False


def test_text_plain():
    """mimeType text/plain + filename notes.txt → refusé."""
    assert is_pdf_attachment(_part("text/plain", "notes.txt")) is False


def test_image_jpeg():
    """mimeType image/jpeg → refusé."""
    assert is_pdf_attachment(_part("image/jpeg", "photo.jpg")) is False


def test_empty_filename_non_pdf_mime():
    """Filename vide + MIME non-PDF → refusé sans crash."""
    assert is_pdf_attachment(_part("application/octet-stream", "")) is False


def test_none_filename_non_pdf_mime():
    """Filename None + MIME non-PDF → refusé sans crash."""
    assert is_pdf_attachment(_part("application/octet-stream", None)) is False


def test_empty_part():
    """Partie vide (pas de mimeType, pas de filename) → refusé sans crash."""
    assert is_pdf_attachment({}) is False


def test_octet_stream_no_extension():
    """application/octet-stream + filename sans extension → refusé."""
    assert is_pdf_attachment(_part("application/octet-stream", "document")) is False


def test_filename_contains_pdf_but_not_extension():
    """Filename 'mon_pdf_document.docx' ne finit pas par .pdf → refusé."""
    assert is_pdf_attachment(_part("application/octet-stream", "mon_pdf_document.docx")) is False
