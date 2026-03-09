#!/usr/bin/env python3
"""
nodes.py — Nœuds du graphe LangGraph + fonctions de routage
============================================================

CONCEPT CLÉ : Les Nœuds (Nodes)
---------------------------------
Un nœud LangGraph est une fonction Python ordinaire avec cette signature :

    def mon_noeud(state: InvoiceState) -> dict:
        # 1. Lire depuis l'état
        data = state["ma_donnee"]

        # 2. Faire le travail
        resultat = process(data)

        # 3. Retourner UNIQUEMENT les champs modifiés
        return {"nouveau_champ": resultat}

LangGraph appelle cette fonction et MERGE le dict retourné dans l'état.

PATTERN : Guard Clause
-----------------------
La plupart des nœuds commencent par :

    if state.get("processing_error"):
        return {}  # Erreur upstream → on ne fait rien

Cela permet d'avoir un graphe avec des arêtes directes (simples)
tout en court-circuitant le traitement en cas d'erreur.
Les nœuds "filter_document" et "call_gemini" utilisent quant à eux
des arêtes CONDITIONNELLES pour sauter des opérations coûteuses.

CONCEPT CLÉ : Les Routeurs (Edge Functions)
--------------------------------------------
Un routeur reçoit l'état et retourne le NOM du prochain nœud :

    def route_apres_filtre(state: InvoiceState) -> str:
        if state.get("processing_error"):
            return "log_result"   # court-circuit vers log
        return "call_gemini"      # chemin normal

Le routeur est passé à graph.add_conditional_edges() dans graph.py.

Workflow des 9 nœuds :
  extract_text → filter_document ─[cond]→ call_gemini ─[cond]→ normalize_data
                      │                        │
                      └──────────────────────────→ log_result → END
  normalize_data → generate_xml → embed_facturx → upload_drive → label_gmail → log_result → END
"""

import io
import logging
import os
import re
from datetime import datetime

import requests
from googleapiclient.http import MediaIoBaseUpload

from pydantic import ValidationError
from schemas import GeminiInvoiceOutput, InvoiceExtracted
from state import InvoiceState
from services import GoogleServices, StateDB
from facturx_utils import (
    extract_text_from_pdf,
    is_invoice_candidate,
    call_gemini,
    normalize_invoice_data,
    generate_facturx_xml_en16931,
    embed_facturx_in_pdf,
    build_filename,
    build_folder_name,
    build_supplier_folder_name,
    GeminiJsonDecodeError,
    MAX_PDF_SIZE_FOR_INVOICE,
)

logger = logging.getLogger("orchestrator")

# Variables d'environnement lues une seule fois au chargement du module
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
GMAIL_LABEL_NAME = os.environ.get("GMAIL_LABEL", "Factures-Traitées")

# ─────────────────────────────────────────────────────────────────────────────
# Nœud 1 : extract_text — OCR du PDF
# ─────────────────────────────────────────────────────────────────────────────

def node_extract_text(state: InvoiceState) -> dict:
    """
    Extrait le texte du PDF (natif PyMuPDF ou OCR Tesseract en fallback).

    Produit : ocr_text
    Erreur si : PDF vide ou extraction impossible
    """
    logger.info(
        "[ 1/9 ] extract_text : %s (%d Ko)",
        state["pdf_filename"], len(state["pdf_bytes"]) // 1024
    )
    try:
        ocr_text = extract_text_from_pdf(state["pdf_bytes"])

        if len((ocr_text or "").strip()) < 20:
            logger.warning("PDF trop peu de texte extrait (%d chars) — PDF vide ou image non-OCR ?", len(ocr_text or ""))
            return {
                "ocr_text": ocr_text or "",
                "processing_error": "pdf_vide_ou_illisible",
            }

        logger.info("Texte extrait : %d caractères", len(ocr_text))
        return {"ocr_text": ocr_text}

    except Exception as e:
        logger.error("Erreur extraction texte : %s", e)
        return {"ocr_text": "", "processing_error": f"erreur_ocr:{e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Nœud 2 : filter_document — Filtrage local rapide (pas de Gemini)
# ─────────────────────────────────────────────────────────────────────────────

def node_filter_document(state: InvoiceState) -> dict:
    """
    Filtre le document par keywords AVANT d'appeler Gemini.

    Économie de quota : si le document n'est clairement pas une facture,
    on le rejette ici (gratuit) sans consommer de quota Gemini.

    Produit : processing_error si rejeté (sinon aucun champ nouveau)
    Routage : add_conditional_edges après ce nœud (route_after_filter)
    """
    # Guard clause : si erreur upstream (OCR), on passe sans analyser
    if state.get("processing_error"):
        return {}

    logger.info("[ 2/9 ] filter_document : analyse keywords...")

    # Garde-fou B : fichier PDF trop volumineux → catalogue/tarif, pas une facture.
    # Une vraie facture fait rarement plus de 5 Mo (quelques pages A4 scannées).
    # Exemple : TARIF IN-IPSO 2026 = 14 Mo → rejeté ici avant même l'analyse texte.
    pdf_size = len(state.get("pdf_bytes", b""))
    if pdf_size > MAX_PDF_SIZE_FOR_INVOICE:
        reason = f"file_too_large:{pdf_size}"
        logger.info("Document rejeté (filtrage local) : %s", reason)
        return {"processing_error": f"not_invoice:{reason}"}

    ok, reason = is_invoice_candidate(state["ocr_text"])
    if not ok:
        logger.info("Document rejeté (filtrage local) : %s", reason)
        return {"processing_error": f"not_invoice:{reason}"}

    logger.info("Document pré-validé comme facture candidat (%s)", reason)
    return {}  # Pas de nouveau champ — juste validation OK


# ─────────────────────────────────────────────────────────────────────────────
# Nœud 3 : call_gemini — Extraction IA structurée EN16931
# ─────────────────────────────────────────────────────────────────────────────

def _validate_invoice_strict(invoice_data: dict) -> list[str]:
    """Valide strictement les champs clés d'une facture extraite par Gemini.

    Construit un ``InvoiceExtracted`` depuis ``invoice_data`` et capture
    toutes les erreurs Pydantic en une liste de messages lisibles.

    Args:
        invoice_data: Dict brut après coercition ``GeminiInvoiceOutput``.

    Returns:
        Liste vide si valide, liste de messages d'erreur sinon.
    """
    try:
        InvoiceExtracted.from_invoice_data(invoice_data)
        return []
    except ValidationError as exc:
        return [
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]


def node_call_gemini(state: InvoiceState) -> dict:
    """
    Appelle Gemini pour extraire les données structurées de la facture.

    Ce nœud est atteint UNIQUEMENT si filter_document a validé le document
    (grâce à l'arête conditionnelle dans graph.py).

    Produit : invoice_data (JSON), gemini_used (True)
    Erreur si : pas une facture, rate limit 429, erreur réseau

    Note : gemini_used=True même en cas d'erreur (quota consommé quand même).
    """
    logger.info("[ 3/9 ] call_gemini : extraction Gemini EN16931...")

    email_context = (
        f"Objet: {state.get('subject', '')}\n"
        f"Expéditeur: {state.get('sender', '')}\n"
        f"Corps: {state.get('body', '')[:1000]}"
    )

    try:
        invoice_data = call_gemini(state["ocr_text"], email_context)

        # Validation Pydantic : coerce les types, détecte les hallucinations de type
        # (ex: montant_ttc="cent euros" → 0.0, lignes=null → [])
        try:
            invoice_data = GeminiInvoiceOutput.model_validate(invoice_data).model_dump()
        except Exception as val_err:
            logger.warning("Validation Pydantic invoice_data : %s — données brutes conservées", val_err)

        if not invoice_data.get("est_facture"):
            logger.info("Gemini : document confirmé non-facture (est_facture=false)")
            return {
                "invoice_data": invoice_data,
                "gemini_used": True,
                "processing_error": "not_invoice_gemini:est_facture=false",
            }

        # Validation STRICTE : champs obligatoires + cohérence métier
        # (après la coercition permissive GeminiInvoiceOutput ci-dessus)
        validation_errors = _validate_invoice_strict(invoice_data)
        if validation_errors:
            fields_str = "; ".join(validation_errors)
            logger.warning(
                "Validation stricte KO — révision manuelle requise : %s", fields_str
            )
            return {
                "invoice_data": invoice_data,
                "gemini_used": True,
                "processing_error": f"validation_ko:{fields_str}",
            }

        logger.info(
            "Gemini : facture détectée — %s, %s€ TTC",
            invoice_data.get("numero_facture", "?"),
            invoice_data.get("montant_ttc", "?"),
        )
        return {"invoice_data": invoice_data, "gemini_used": True}

    except GeminiJsonDecodeError as e:
        # JSON invalide après nettoyage ET retry → erreur permanente (non-retriable).
        # Marqué "error" dans SQLite via le préfixe non-"not_invoice".
        # Statut distinct "erreur_json_permanent" pour faciliter l'audit des logs.
        logger.error("JSON Gemini invalide de façon permanente — marquage non-retriable : %s", e)
        return {"gemini_used": True, "processing_error": f"erreur_json_permanent:{e}"}

    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        if status == 429:
            logger.warning("Gemini rate limit (429) — sera retenté au prochain cycle")
            # NE PAS mettre gemini_used=True : pas marqué dans SQLite → retentable
            return {"processing_error": "rate_limit_429"}
        logger.error("Erreur HTTP Gemini (%s)", status)
        return {"gemini_used": True, "processing_error": f"erreur_http_gemini:{status}"}

    except Exception as e:
        logger.error("Erreur appel Gemini : %s", e)
        return {"gemini_used": True, "processing_error": f"erreur_gemini:{e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Nœud 4 : normalize_data — Normalisation pour EN16931
# ─────────────────────────────────────────────────────────────────────────────

def node_normalize_data(state: InvoiceState) -> dict:
    """
    Normalise et complète les données extraites par Gemini.

    Garantit la conformité EN16931 :
      - Adresses vendeur/acheteur avec code pays
      - Au moins 1 ligne de facture (BR-16)
      - Ventilation TVA cohérente (BG-23)
      - Totaux monétaires recalculés si incohérents (BG-22)

    Produit : invoice_data normalisé (écrase l'existant)
    """
    if state.get("processing_error"):
        return {}

    logger.info("[ 4/9 ] normalize_data : normalisation EN16931...")

    try:
        normalized = normalize_invoice_data(dict(state["invoice_data"]))
        logger.info(
            "Normalisé : %d ligne(s), HT=%.2f€, TVA=%.2f€, TTC=%.2f€",
            len(normalized.get("lignes", [])),
            normalized.get("montant_ht", 0),
            normalized.get("montant_tva", 0),
            normalized.get("montant_ttc", 0),
        )

        # Garde-fou C : montants nuls ET aucun numéro de facture
        # → catalogue/tarif mal interprété par Gemini (ex : TARIF IN-IPSO).
        # ATTENTION : ne pas rejeter si numéro présent même avec TTC=0
        # (vraies factures SAV avec remise 100% : ex Interbat FA130927).
        has_invoice_number = bool(normalized.get("numero_facture"))
        has_amounts = (
            normalized.get("montant_ttc", 0.0) != 0.0
            or normalized.get("montant_ht", 0.0) != 0.0
        )
        if not has_invoice_number and not has_amounts:
            logger.info(
                "Rejeté après normalisation : montants nuls + aucun numéro facture "
                "(probablement catalogue/tarif mal interprété par Gemini)"
            )
            return {
                "invoice_data": normalized,
                "processing_error": "not_invoice_gemini:no_number_no_amount",
            }

        return {"invoice_data": normalized}

    except Exception as e:
        logger.error("Erreur normalisation : %s", e)
        return {"processing_error": f"erreur_normalisation:{e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Nœud 5 : generate_xml — Génération du XML Factur-X EN16931
# ─────────────────────────────────────────────────────────────────────────────

def node_generate_xml(state: InvoiceState) -> dict:
    """
    Génère le XML Factur-X (Cross Industry Invoice, profil EN16931).

    Le XML est conforme à la norme EN 16931 (CII D16B/D22B) avec
    les namespaces rsm, ram, qdt, udt.

    Produit : xml_bytes (XML UTF-8 complet)
    """
    if state.get("processing_error"):
        return {}

    logger.info("[ 5/9 ] generate_xml : génération XML EN16931...")

    try:
        xml_bytes = generate_facturx_xml_en16931(state["invoice_data"])
        logger.info("XML généré : %d octets", len(xml_bytes))
        return {"xml_bytes": xml_bytes}

    except Exception as e:
        logger.error("Erreur génération XML : %s", e)
        return {"processing_error": f"erreur_xml:{e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Nœud 6 : embed_facturx — Embedding XML dans le PDF (PDF/A-3b)
# ─────────────────────────────────────────────────────────────────────────────

def node_embed_facturx(state: InvoiceState) -> dict:
    """
    Embarque le XML Factur-X dans le PDF original et génère un PDF/A-3b.

    Deux étapes internes :
      1. Embedding XML (lib Akretion factur-x)
      2. Injection marqueurs PDF/A-3b (OutputIntent + pdfaid XMP)

    Produit : facturx_pdf (bytes), invoice_filename, invoice_folder
    """
    if state.get("processing_error"):
        return {}

    logger.info("[ 6/9 ] embed_facturx : création PDF/A-3b Factur-X...")

    try:
        facturx_pdf = embed_facturx_in_pdf(state["pdf_bytes"], state["xml_bytes"])

        inv = state["invoice_data"]
        filename = build_filename(inv)
        folder = build_folder_name(inv)

        logger.info("PDF/A-3b créé : %s → dossier '%s'", filename, folder)
        return {
            "facturx_pdf": facturx_pdf,
            "invoice_filename": filename,
            "invoice_folder": folder,
        }

    except Exception as e:
        logger.error("Erreur embedding Factur-X : %s", e)
        return {"processing_error": f"erreur_embedding:{e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Nœud 7 : upload_drive — Upload sur Google Drive
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_drive_folder(services: GoogleServices, name: str, parent_id: str) -> str:
    """Cherche ou crée un dossier Drive par nom sous un parent donné. Retourne l'ID."""
    query = (
        f"name = '{name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    results = (
        services.drive.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    folders = results.get("files", [])
    if folders:
        return folders[0]["id"]
    folder_meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = services.drive.files().create(body=folder_meta, fields="id").execute()
    logger.info("Dossier Drive créé : '%s' (ID: %s)", name, folder["id"])
    return folder["id"]


def node_upload_drive(state: InvoiceState) -> dict:
    """
    Upload le PDF Factur-X sur Google Drive dans la hiérarchie :
      ROOT / YYYY-MM Mois / Fournisseur / fichier.pdf

    Les sous-dossiers mensuel et fournisseur sont créés à la volée si absents.
    Retourne l'ID Drive et le lien partageable pour l'audit SQLite.

    Produit : drive_file_id, drive_file_url
    """
    if state.get("processing_error"):
        return {}

    if not DRIVE_FOLDER_ID:
        logger.error("DRIVE_FOLDER_ID non configuré dans .env !")
        return {"processing_error": "drive_folder_id_manquant"}

    services: GoogleServices = state["services"]
    month_folder_name = state["invoice_folder"]
    supplier_folder_name = build_supplier_folder_name(state.get("invoice_data", {}))
    filename = state["invoice_filename"]

    logger.info(
        "[ 7/9 ] upload_drive : upload '%s' → '%s/%s'...",
        filename, month_folder_name, supplier_folder_name,
    )

    try:
        # Niveau 1 : sous-dossier mensuel (ex : "2026-03 Mars")
        month_id = _get_or_create_drive_folder(services, month_folder_name, DRIVE_FOLDER_ID)
        logger.info("Dossier mensuel : '%s'", month_folder_name)

        # Niveau 2 : sous-dossier fournisseur (ex : "IPSO", "GPDIS")
        supplier_id = _get_or_create_drive_folder(services, supplier_folder_name, month_id)
        logger.info("Dossier fournisseur : '%s'", supplier_folder_name)

        # Upload du fichier dans le dossier fournisseur
        file_meta = {"name": filename, "parents": [supplier_id]}
        media = MediaIoBaseUpload(
            io.BytesIO(state["facturx_pdf"]),
            mimetype="application/pdf",
            resumable=True,
        )
        uploaded = (
            services.drive.files()
            .create(body=file_meta, media_body=media, fields="id, webViewLink")
            .execute()
        )

        file_id = uploaded["id"]
        file_url = uploaded.get("webViewLink", "")
        logger.info("Drive ← '%s' : %s", filename, file_url)

        return {"drive_file_id": file_id, "drive_file_url": file_url}

    except Exception as e:
        logger.error("Erreur upload Drive : %s", e)
        return {"processing_error": f"erreur_drive:{e}"}



# ─────────────────────────────────────────────────────────────────────────────
# Nœud 9 : label_gmail — Labellisation Gmail
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_supplier_name(name: str) -> str:
    """Normalise le nom fournisseur pour le label Gmail 'Fournisseurs/{name}'.

    Retire les formes juridiques courantes et nettoie les espaces multiples.
    La casse est conservée pour permettre la correspondance case-insensitive
    avec les labels Gmail existants (via get_or_create_label).
    """
    if not name:
        return "Inconnu"
    name = re.sub(r"\b(SAS|SARL|SA|SCI|EURL|EI|SNC)\b", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Inconnu"


def node_label_gmail(state: InvoiceState) -> dict:
    """
    Applique deux labels Gmail à l'email traité :
    - 'Factures-Traitées' : anti-replay indispensable pour le polling
    - 'Fournisseurs/{nom}' : classement par fournisseur

    Skippé si le traitement amont a échoué (guard clause) :
    pas de label si le fichier n'est pas sur Drive.

    Ce nœud est "best-effort" : une erreur ici est loggée mais non-fatale.
    """
    if state.get("processing_error"):
        return {}

    services: GoogleServices = state["services"]

    # Extraire le nom du fournisseur depuis invoice_data
    invoice_data = state.get("invoice_data") or {}
    vendeur = invoice_data.get("vendeur") or {}
    raw_name = vendeur.get("nom_court") or vendeur.get("nom") or ""
    supplier_name = _normalize_supplier_name(raw_name)
    supplier_label = f"Fournisseurs/{supplier_name}"

    logger.info(
        "[ 8/9 ] label_gmail : labels '%s' + '%s' → email %s...",
        GMAIL_LABEL_NAME, supplier_label, state["message_id"],
    )

    try:
        label_ids = [
            services.get_or_create_label(GMAIL_LABEL_NAME),
            services.get_or_create_label(supplier_label),
        ]
        services.gmail.users().messages().modify(
            userId="me",
            id=state["message_id"],
            body={"addLabelIds": label_ids},
        ).execute()
        logger.info(
            "Labels '%s' + '%s' appliqués à l'email %s",
            GMAIL_LABEL_NAME, supplier_label, state["message_id"],
        )

    except Exception as e:
        # Erreur non-bloquante : le fichier est déjà sur Drive
        logger.error("Erreur label Gmail (non-bloquant) : %s", e)

    return {}



# ─────────────────────────────────────────────────────────────────────────────
# Nœud 9 : log_result — Log final + écriture SQLite
# ─────────────────────────────────────────────────────────────────────────────

def node_log_result(state: InvoiceState) -> dict:
    """
    Nœud terminal : log le résultat et écrit dans SQLite (anti-replay).

    Centralise toutes les écritures SQLite (ni extract_text ni call_gemini
    n'écrivent directement en base — c'est ce nœud qui décide).

    Logique de marquage :
      - "not_invoice*" → statut "not_invoice" (skip aux prochains cycles)
      - "rate_limit_429" → PAS de marquage (retentable au prochain cycle)
      - autre erreur → statut "error" (skip pour éviter les boucles)
      - pas d'erreur → statut "success" avec lien Drive
    """
    db: StateDB = state["state_db"]
    error = state.get("processing_error", "")

    logger.info("[ 9/9 ] log_result : finalisation...")

    if error.startswith("not_invoice"):
        # Document rejeté (filtre local ou Gemini) → marquer pour ne plus le traiter
        db.mark(
            state["message_id"], state["pdf_filename"],
            "not_invoice", detail=error[:200],
        )
        logger.info("⏭️  Non-facture : '%s' / %s — %s",
                    state.get("subject", "?")[:50], state["pdf_filename"], error)

    elif error == "rate_limit_429":
        # Rate limit Gemini : NE PAS marquer dans SQLite → sera retenté
        logger.warning(
            "⏱️  Rate limit Gemini — '%s' sera retenté au prochain cycle",
            state.get("subject", "?")[:50],
        )

    elif error:
        # Autre erreur → marquer "error" (évite de boucler infiniment)
        db.mark(
            state["message_id"], state["pdf_filename"],
            "error", detail=error[:200],
        )
        logger.warning(
            "❌ Erreur : [%s] '%s' / %s — %s",
            state.get("sender", "?"), state.get("subject", "?")[:50],
            state["pdf_filename"], error,
        )

    else:
        # Succès complet !
        inv = state.get("invoice_data", {})
        vendor = inv.get("vendeur", {}).get("nom_court", "?")
        numero = inv.get("numero_facture", "?")
        ttc = inv.get("montant_ttc", "?")
        url = state.get("drive_file_url", "?")

        db.mark(
            state["message_id"], state["pdf_filename"],
            "success",
            detail=f"{vendor} | {numero} | {ttc}€",
            drive_url=url,
        )

        logger.info("✅ Succès : %s | %s | %s€ TTC → %s", vendor, numero, ttc, url)

    return {}  # Nœud terminal : aucun champ à mettre à jour


# ─────────────────────────────────────────────────────────────────────────────
# Nœud manual_review — Révision manuelle des factures rejetées par validation
# ─────────────────────────────────────────────────────────────────────────────

def node_manual_review(state: InvoiceState) -> dict:
    """Nœud déclenché quand la validation stricte ``InvoiceExtracted`` échoue.

    Ce nœud est "best-effort" : il logge les anomalies de façon structurée
    pour permettre une intervention humaine, puis laisse ``log_result``
    écrire le statut ``error`` dans SQLite.

    Aucune écriture sur Drive ni label Gmail n'est effectuée (guard clauses
    dans les nœuds aval bloquent tout traitement si ``processing_error`` est positionné).

    Le message d'email N'EST PAS labellisé ``Factures-Traitées`` : il pourra
    donc être retraité manuellement ou après correction des données sources.
    """
    err = state.get("processing_error", "")
    inv = state.get("invoice_data") or {}
    vendeur = inv.get("vendeur") or {}

    logger.warning(
        "╔══ RÉVISION MANUELLE REQUISE ══════════════════════════════════════╗"
    )
    logger.warning("║  Email      : %s", state.get("message_id", "?"))
    logger.warning("║  Fournisseur: %s", vendeur.get("nom_court") or vendeur.get("nom") or "?")
    logger.warning("║  Numéro     : %s", inv.get("numero_facture") or "?")
    logger.warning("║  TTC        : %s€", inv.get("montant_ttc") or "?")
    logger.warning("║  Erreurs    : %s", err.removeprefix("validation_ko:"))
    logger.warning(
        "╚═══════════════════════════════════════════════════════════════════╝"
    )
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Routeurs — Fonctions de décision pour les arêtes conditionnelles
# ─────────────────────────────────────────────────────────────────────────────

def route_after_filter(state: InvoiceState) -> str:
    """
    Routeur après filter_document.

    CONCEPT : Un routeur est une fonction qui retourne le NOM du prochain nœud.
    LangGraph utilise ce nom pour choisir quelle arête emprunter.

    La map passée à add_conditional_edges() fait correspondre les valeurs
    retournées aux nœuds réels du graphe.

    Ici : si le document est rejeté (processing_error positionné),
    on saute directement à log_result sans appeler Gemini.
    Économie : 1 appel API Gemini évité sur les non-factures évidentes.
    """
    return "log_result" if state.get("processing_error") else "call_gemini"


def route_after_gemini(state: InvoiceState) -> str:
    """
    Routeur après call_gemini.

    Si Gemini dit "pas une facture" ou si on a un rate limit 429,
    on saute directement à log_result sans générer de XML (opération coûteuse).

    Note : les deux cas d'erreur (not_invoice et rate_limit) sont gérés
    différemment dans log_result (l'un marque SQLite, l'autre non).

    Cas supplémentaire : si la validation stricte ``InvoiceExtracted`` a échoué
    (``processing_error`` commence par ``"validation_ko:"``), on route vers
    ``manual_review`` au lieu de ``log_result`` directement, afin de loguer
    les champs en erreur de façon structurée avant la persistence SQLite.
    """
    err = state.get("processing_error", "")
    if not err:
        return "normalize_data"
    if err.startswith("validation_ko:"):
        return "manual_review"
    return "log_result"
