#!/usr/bin/env python3
"""
state.py — Définition de l'état partagé du workflow LangGraph
=============================================================

CONCEPT CLÉ : L'État en LangGraph
-----------------------------------
LangGraph passe un TypedDict comme "mémoire partagée" entre tous les nœuds.

Règle fondamentale :
  1. Chaque nœud REÇOIT l'état complet en lecture
  2. Chaque nœud RETOURNE un dict partiel (seuls les champs qu'il modifie)
  3. LangGraph MERGE automatiquement ce dict partiel dans l'état global

      nœud(état_complet) → dict_partiel
      état_n+1 = {**état_n, **dict_partiel}

Pourquoi pas des variables globales ?
  ✅ Testabilité  — on crée un état de test sans démarrer l'app
  ✅ Traçabilité  — chaque nœud déclare explicitement ses sorties
  ✅ Checkpointing — LangGraph peut sauvegarder/restaurer l'état à tout moment
  ✅ Parallélisme  — des nœuds indépendants peuvent tourner en //
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, Any

# Imports tardifs uniquement pour les annotations de type
# (évite les dépendances circulaires à l'import)
if TYPE_CHECKING:
    from services import GoogleServices, StateDB


class InvoiceState(TypedDict):
    """
    État complet d'un workflow de traitement d'une facture.

    Cycle de vie des champs :
    ┌──────────────────────┬──────────────────────────────────────────────────┐
    │ Injection initiale   │ message_id, subject, sender, body                │
    │ (avant invoke)       │ pdf_bytes, pdf_filename, services, state_db      │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ extract_text         │ + ocr_text                                       │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ filter_document      │ (pas de nouveau champ — décision de routage)     │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ call_gemini          │ + invoice_data, gemini_used                      │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ normalize_data       │ invoice_data enrichi                             │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ generate_xml         │ + xml_bytes                                      │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ embed_facturx        │ + facturx_pdf, invoice_filename, invoice_folder  │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ upload_drive         │ + drive_file_id, drive_file_url                  │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ label_gmail          │ (pas de nouveau champ)                           │
    ├──────────────────────┼──────────────────────────────────────────────────┤
    │ log_result           │ (effets de bord uniquement : SQLite + logs)      │
    └──────────────────────┴──────────────────────────────────────────────────┘
    """

    # ── Données d'entrée (email) ──────────────────────────────────────────────
    message_id: str       # ID Gmail unique — clé primaire SQLite (anti-replay)
    subject: str          # Objet de l'email
    sender: str           # Expéditeur ("Nom <email@domain.com>")
    body: str             # Corps de l'email (texte brut, tronqué à ~1000 chars)
    pdf_bytes: bytes      # Contenu brut du PDF — jamais écrit sur disque
    pdf_filename: str     # Nom original du fichier ("facture-jan-2026.pdf")

    # ── Nœud extract_text ────────────────────────────────────────────────────
    ocr_text: str         # Texte extrait : natif PyMuPDF OU Tesseract OCR

    # ── Nœud call_gemini ─────────────────────────────────────────────────────
    invoice_data: dict    # JSON structuré retourné par Gemini (puis normalisé)
    gemini_used: bool     # True → incrémente le compteur de quota journalier

    # ── Nœuds generate_xml + embed_facturx ───────────────────────────────────
    xml_bytes: bytes      # XML EN16931 brut (format CII D16B/D22B)
    facturx_pdf: bytes    # PDF/A-3b avec XML embarqué — livrable final
    invoice_filename: str # "2026-02-EDF-INV001.pdf" (calculé depuis invoice_data)
    invoice_folder: str   # "2026-02 Février" (sous-dossier Drive mensuel)

    # ── Nœud upload_drive ────────────────────────────────────────────────────
    drive_file_id: str    # ID Google Drive (pour référence et audit)
    drive_file_url: str   # Lien partageable (logué dans SQLite)

    # ── Gestion des erreurs ───────────────────────────────────────────────────
    # Chaque nœud peut positionner ce champ pour signaler une erreur.
    # Les nœuds suivants vérifient ce champ en entrée (guard clause).
    # log_result lit ce champ pour écrire le bon statut dans SQLite.
    #
    # Valeurs spéciales :
    #   "rate_limit_429"      → NE PAS marquer dans SQLite (sera retenté)
    #   str commençant par
    #   "not_invoice"         → marquer "not_invoice" (skip futur garanti)
    #   autre chaîne non-vide → marquer "error" dans SQLite
    processing_error: str

    # ── Services partagés (singletons injectés une seule fois au démarrage) ──
    # Pattern : injection de dépendances via l'état plutôt que variables globales.
    # Avantage : facile à mocker en test (passer un dict avec des faux services).
    services: Any               # Clients Gmail + Drive (OAuth2)
    state_db: Any          # SQLite : anti-replay + compteur quotas Gemini
