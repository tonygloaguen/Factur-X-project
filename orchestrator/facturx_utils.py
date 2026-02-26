#!/usr/bin/env python3
"""
facturx.py — Fonctions métier pures : OCR, Gemini, XML, PDF
=============================================================

Ce module contient TOUTE la logique de traitement des factures,
sous forme de fonctions Python ordinaires (pas de LangGraph ici).

Design intentionnel :
  - Ces fonctions sont testables indépendamment du workflow
  - Les nœuds LangGraph dans nodes.py appellent ces fonctions
  - Séparation claire : "quoi faire" (ici) vs "dans quel ordre" (graph.py)

Pipeline complet :
  pdf_bytes
    │
    ├─ extract_text_from_pdf()   → ocr_text (natif PyMuPDF ou Tesseract)
    │
    ├─ is_invoice_candidate()    → (bool, raison) — filtre keywords local
    │
    ├─ call_gemini()             → invoice_data (JSON structuré)
    │
    ├─ normalize_invoice_data()  → invoice_data enrichi (valeurs par défaut)
    │
    ├─ generate_facturx_xml_en16931() → xml_bytes (XML CII D16B)
    │
    └─ embed_facturx_in_pdf()   → facturx_pdf (PDF/A-3b + XML embarqué)
"""

import io
import json
import logging
import os
import re
import time
from datetime import datetime

import requests
from lxml import etree

import fitz  # PyMuPDF : extraction texte natif + OCR via Tesseract

from facturx import generate_from_binary  # Akretion : embedding PDF/A-3 + XML

logger = logging.getLogger("orchestrator")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (depuis variables d'environnement)
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

FACTURX_PROFILE = os.environ.get("FACTURX_PROFILE", "en16931").strip().lower()

# Noms de mois en français (pour les sous-dossiers Drive mensuels)
MOIS_FR = {
    1: "Janvier", 2: "Février",   3: "Mars",      4: "Avril",
    5: "Mai",     6: "Juin",      7: "Juillet",   8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}

# ─────────────────────────────────────────────────────────────────────────────
# Filtrage local (évite d'appeler Gemini sur des non-factures évidentes)
# ─────────────────────────────────────────────────────────────────────────────

FACTURE_KEYWORDS = [
    "facture", "invoice", "avoir",
    "tva", "vat",
    "ht", "ttc",
    "total", "montant",
    "échéance", "due date",
    "siret", "siren", "iban", "bic",
    "numéro", "numero", "n°",
]

# Deny HARD : jamais une facture — toujours bloquer
DENY_HARD_KEYWORDS = [
    "curriculum vitae",
    "billet", "boarding pass", "vos billets",
]

# Deny SOFT : souvent pas une facture, MAIS peut apparaître dans une vraie
# (ex: assurance pro MSA, mutuelle entreprise...).
# Bloquer seulement si le score facture est faible (< 3).
DENY_SOFT_KEYWORDS = [
    "notification", "rappel", "documents en retard",
    "convocation", "attestation",
    "relevé de remboursement", "remboursement",
    "mutuelle", "assurance",
    "consultation", "rdv", "rendez-vous",
]


def is_invoice_candidate(text: str) -> tuple[bool, str]:
    """
    Analyse le texte OCR et décide si le document est une facture candidate.

    Retourne (True, raison) ou (False, raison).
    Le filtre est intentionnellement large (FP tolérés, FN évités) :
    Gemini confirme ensuite avec "est_facture": true/false.
    """
    text_l = (text or "").lower()

    # Hard deny : bloque immédiatement
    for kw in DENY_HARD_KEYWORDS:
        if kw in text_l:
            return False, f"deny_hard:{kw}"

    score = sum(1 for kw in FACTURE_KEYWORDS if kw in text_l)

    # Soft deny : bloque seulement si peu d'indices "facture"
    if score < 3:
        for kw in DENY_SOFT_KEYWORDS:
            if kw in text_l:
                return False, f"deny_soft:{kw}|score:{score}"

    if score < 2:
        return False, f"score_trop_bas:{score}"

    return True, f"score:{score}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers : noms de fichiers et de dossiers
# ─────────────────────────────────────────────────────────────────────────────

_filename_bad = re.compile(r'[\\/:*?"<>|]+')


def sanitize_filename(name: str) -> str:
    name = (name or "").strip()
    name = _filename_bad.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 180:
        base, ext = os.path.splitext(name)
        name = base[:170] + ext
    return name or "document.pdf"


def build_filename(inv: dict) -> str:
    """Construit le nom de fichier Drive : YYYY-MM-Fournisseur-NumeroFacture.pdf"""
    vendeur = inv.get("vendeur", {}) or {}
    nom_court = (vendeur.get("nom_court") or "Fournisseur").replace(" ", "_")
    date = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    numero = inv.get("numero_facture") or ""
    suffix = f"_{numero}" if numero else ""
    return sanitize_filename(f"{nom_court}_FacturX_{date}{suffix}.pdf")


def build_folder_name(inv: dict) -> str:
    """Construit le nom du sous-dossier Drive mensuel : 'YYYY-MM Mois'"""
    date_str = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        dt = datetime.now()
    return f"{dt.year}-{dt.month:02d} {MOIS_FR.get(dt.month, '')}".strip()


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 1) Extraction texte PDF (natif + OCR si besoin)
# ─────────────────────────────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extrait le texte d'un PDF.

    Stratégie en 2 temps :
      1. Extraction native (PyMuPDF) — rapide, fiable pour les PDF numériques
      2. Si < 50 chars par page → OCR Tesseract (français + allemand + anglais)
         à 300 DPI via PyMuPDF (sans écrire sur disque)
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    full_text: list[str] = []
    try:
        for page in doc:
            text = page.get_text("text") or ""
            if len(text.strip()) < 50:
                # PDF scanné : fallback OCR Tesseract
                try:
                    tp = page.get_textpage_ocr(language="fra+deu+eng", dpi=300)
                    text = page.get_text("text", textpage=tp) or ""
                except Exception as e:
                    logger.warning("OCR échoué pour une page : %s", e)
            full_text.append(text)
    finally:
        doc.close()
    return "\n".join(full_text)


# ─────────────────────────────────────────────────────────────────────────────
# 2) Gemini : extraction structurée EN16931
# ─────────────────────────────────────────────────────────────────────────────

# Prompt système détaillé pour l'extraction EN16931-compliant
GEMINI_SYSTEM_PROMPT = """\
Tu es un assistant comptable expert en facturation française et européenne.
Tu reçois le texte brut extrait par OCR d'une facture fournisseur (PDF).

Tu dois extraire TOUTES les données disponibles et les retourner en JSON.
Réponds UNIQUEMENT avec un JSON valide, SANS markdown, SANS commentaire.

Le JSON doit contenir les données nécessaires au profil Factur-X EN16931
(norme européenne EN 16931). C'est CRITIQUE d'extraire les LIGNES de facture.

{
  "est_facture": true,
  "numero_facture": "string ou null",
  "date_facture": "YYYY-MM-DD",
  "date_echeance": "YYYY-MM-DD ou null",
  "type_facture": "380 pour facture, 381 pour avoir, 389 pour auto-facture",
  "devise": "EUR",

  "vendeur": {
    "nom": "Raison sociale complète",
    "nom_court": "Nom nettoyé sans forme juridique",
    "siret": "14 chiffres ou null",
    "siren": "9 chiffres ou null",
    "tva_intra": "FRXX... ou DEXX... ou null",
    "adresse_ligne1": "Numéro et rue ou null",
    "adresse_ligne2": "Complément ou null",
    "code_postal": "string ou null",
    "ville": "string ou null",
    "pays_code": "FR, DE, etc. Défaut FR si non trouvé"
  },

  "acheteur": {
    "nom": "Raison sociale complète ou null",
    "siret": "string ou null",
    "tva_intra": "string ou null",
    "adresse_ligne1": "Numéro et rue ou null",
    "code_postal": "string ou null",
    "ville": "string ou null",
    "pays_code": "FR par défaut si non trouvé"
  },

  "lignes": [
    {
      "numero": "1",
      "description": "Description de l'article ou du service",
      "quantite": 1.0,
      "unite": "C62 pour unité, HUR pour heure, KGM pour kg, MTR pour mètre",
      "prix_unitaire_ht": 100.00,
      "montant_net_ht": 100.00,
      "taux_tva": 20.0,
      "code_tva": "S pour standard, Z pour zéro, E pour exonéré, AE pour autoliquidation"
    }
  ],

  "ventilation_tva": [
    {
      "code_tva": "S",
      "taux": 20.0,
      "base_ht": 100.00,
      "montant_tva": 20.00
    }
  ],

  "montant_total_lignes_net": 0.00,
  "montant_ht": 0.00,
  "montant_tva": 0.00,
  "montant_ttc": 0.00,
  "montant_du": 0.00,

  "reference_commande": "string ou null",
  "code_moyen_paiement": "30 pour virement, 58 pour SEPA, 48 pour carte, null si inconnu",
  "iban": "string ou null",
  "bic": "string ou null",
  "notes": "informations complémentaires ou null"
}

Règles CRITIQUES :
- Le VENDEUR est celui qui ÉMET la facture (le fournisseur)
- L'ACHETEUR est celui qui REÇOIT la facture
- Le nom_court supprime les formes juridiques (SARL, SAS, GmbH, SA, etc.)
- Si tu ne trouves pas une donnée, mets null (ne devine JAMAIS un SIRET/TVA)
- Les montants sont des nombres décimaux (pas des strings)
- Le champ "lignes" est OBLIGATOIRE. Crée au moins 1 objet.
- Si tu ne peux pas identifier les lignes, crée UNE SEULE ligne "Prestation globale"
- Pour chaque ligne : prix_unitaire_ht * quantite doit = montant_net_ht
- La ventilation_tva regroupe les lignes par taux de TVA
- montant_du = montant restant à payer (= montant_ttc si pas d'acompte)
- pays_code TOUJOURS en 2 lettres ISO (FR, DE, BE, CH...). Défaut "FR"
"""


def call_gemini(ocr_text: str, email_context: str = "") -> dict:
    """
    Appelle l'API Gemini pour extraire les données structurées d'une facture.

    Gestion des erreurs :
      - 429 (rate limit) : backoff exponentiel avec Retry-After header
      - Autres HTTP 4xx/5xx : raise immédiat (propagé au nœud call_gemini)

    Returns:
        dict : données JSON de la facture (champ "est_facture" inclus)

    Raises:
        requests.exceptions.HTTPError : erreur HTTP Gemini (dont 429)
        ValueError : GEMINI_API_KEY non configurée
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY non configurée dans les variables d'environnement")

    user_message = f"Texte OCR de la facture :\n\n{(ocr_text or '')[:8000]}"
    if email_context:
        user_message += f"\n\nContexte email :\n{email_context[:2000]}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": GEMINI_SYSTEM_PROMPT + "\n\n" + user_message}]}
        ],
        "generationConfig": {
            "temperature": 0.1,               # Déterministe (extraction, pas créatif)
            "responseMimeType": "application/json",
        },
    }

    max_attempts = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "4"))
    base_sleep = float(os.environ.get("GEMINI_BACKOFF_BASE_SECONDS", "5"))

    last_status = None
    for attempt in range(1, max_attempts + 1):
        resp = requests.post(GEMINI_URL, json=payload, timeout=90)
        last_status = resp.status_code

        if resp.status_code == 429:
            # Respecter le Retry-After si présent, sinon backoff exponentiel
            ra = resp.headers.get("Retry-After")
            try:
                sleep_s = int(ra) if ra and ra.isdigit() else min(60, base_sleep * (2 ** (attempt - 1)))
            except Exception:
                sleep_s = min(60, base_sleep * (2 ** (attempt - 1)))
            sleep_s += attempt * 0.2  # jitter léger
            logger.warning("Gemini 429 — tentative %d/%d, pause %.1fs", attempt, max_attempts, sleep_s)
            time.sleep(sleep_s)
            continue

        if resp.status_code >= 400:
            logger.error("Erreur HTTP Gemini (%d): %s", resp.status_code, resp.text[:200])
            resp.raise_for_status()

        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]

        # Nettoyer le markdown éventuel (```json ... ```)
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)

        return json.loads(cleaned)

    raise requests.exceptions.HTTPError(
        f"Gemini rate limit (429) après {max_attempts} tentatives",
        response=type("R", (), {"status_code": last_status})(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3) Normalisation des données Gemini pour EN16931
# ─────────────────────────────────────────────────────────────────────────────

def normalize_invoice_data(inv: dict) -> dict:
    """
    Normalise et complète les données extraites par Gemini.

    Garantit qu'un XML EN16931 valide peut être généré :
      - Vendeur + Acheteur avec adresse et code pays (BR-08/09, BR-10/11)
      - Au moins 1 ligne de facture (BR-16)
      - Ventilation TVA cohérente (BG-23)
      - Totaux monétaires complets (BG-22)
    """
    # --- Vendeur ---
    vendeur = inv.get("vendeur") or {}
    vendeur.setdefault("nom", "Fournisseur inconnu")
    vendeur.setdefault("nom_court", vendeur["nom"])
    vendeur.setdefault("pays_code", "FR")
    vendeur.setdefault("adresse_ligne1", "")
    vendeur.setdefault("code_postal", "")
    vendeur.setdefault("ville", "")
    inv["vendeur"] = vendeur

    # --- Acheteur ---
    acheteur = inv.get("acheteur") or {}
    acheteur.setdefault("nom", "Acheteur")
    acheteur.setdefault("pays_code", "FR")
    acheteur.setdefault("adresse_ligne1", "")
    acheteur.setdefault("code_postal", "")
    acheteur.setdefault("ville", "")
    inv["acheteur"] = acheteur

    # --- Lignes de facture : garantir au moins 1 (BR-16) ---
    lignes = inv.get("lignes") or []
    if not lignes:
        ht = _safe_float(inv.get("montant_ht"))
        taux = _safe_float(inv.get("taux_tva_principal", 20.0))
        lignes = [{
            "numero": "1",
            "description": "Prestation globale",
            "quantite": 1.0,
            "unite": "C62",
            "prix_unitaire_ht": ht,
            "montant_net_ht": ht,
            "taux_tva": taux,
            "code_tva": "S",
        }]
    else:
        for i, line in enumerate(lignes):
            line.setdefault("numero", str(i + 1))
            line.setdefault("description", "Article")
            line.setdefault("quantite", 1.0)
            line.setdefault("unite", "C62")
            line.setdefault("code_tva", "S")
            line.setdefault("taux_tva", 20.0)
            pu = _safe_float(line.get("prix_unitaire_ht"))
            qty = _safe_float(line.get("quantite"), 1.0)
            net = _safe_float(line.get("montant_net_ht"))
            # Recalculer si un champ est manquant
            if net == 0.0 and pu > 0:
                net = round(pu * qty, 2)
            elif pu == 0.0 and net > 0 and qty > 0:
                pu = round(net / qty, 2)
            line["prix_unitaire_ht"] = pu
            line["quantite"] = qty
            line["montant_net_ht"] = net
    inv["lignes"] = lignes

    # --- Ventilation TVA (BG-23) : calculer si absente ---
    if not inv.get("ventilation_tva"):
        tva_map: dict[tuple[str, float], dict] = {}
        for line in lignes:
            code = line.get("code_tva", "S")
            taux = _safe_float(line.get("taux_tva", 20.0))
            net = _safe_float(line.get("montant_net_ht"))
            key = (code, taux)
            if key not in tva_map:
                tva_map[key] = {"code_tva": code, "taux": taux, "base_ht": 0.0, "montant_tva": 0.0}
            tva_map[key]["base_ht"] += net
            tva_map[key]["montant_tva"] += round(net * taux / 100, 2)
        inv["ventilation_tva"] = list(tva_map.values())

    for v in inv["ventilation_tva"]:
        v["base_ht"] = round(_safe_float(v.get("base_ht")), 2)
        v["montant_tva"] = round(_safe_float(v.get("montant_tva")), 2)

    # --- Totaux monétaires (BG-22) : recalculer si incohérents ---
    sum_lines = round(sum(_safe_float(l.get("montant_net_ht")) for l in lignes), 2)
    sum_tva = round(sum(_safe_float(v.get("montant_tva")) for v in inv["ventilation_tva"]), 2)

    ht = _safe_float(inv.get("montant_ht"))
    tva = _safe_float(inv.get("montant_tva"))
    ttc = _safe_float(inv.get("montant_ttc"))

    if ht == 0.0:
        ht = sum_lines
    if tva == 0.0:
        tva = sum_tva
    if ttc == 0.0:
        ttc = round(ht + tva, 2)

    inv["montant_total_lignes_net"] = sum_lines
    inv["montant_ht"] = ht
    inv["montant_tva"] = tva
    inv["montant_ttc"] = ttc
    inv["montant_du"] = _safe_float(inv.get("montant_du")) or ttc

    # --- Divers ---
    inv.setdefault("devise", "EUR")
    inv.setdefault("type_facture", "380")
    inv.setdefault("date_facture", datetime.now().strftime("%Y-%m-%d"))
    inv.setdefault("code_moyen_paiement", "30" if inv.get("iban") else None)

    return inv


# ─────────────────────────────────────────────────────────────────────────────
# 4) Génération XML Factur-X profil EN16931 (CII D16B/D22B)
# ─────────────────────────────────────────────────────────────────────────────

def generate_facturx_xml_en16931(inv: dict) -> bytes:
    """
    Génère le XML Factur-X profil EN16931 (Cross Industry Invoice).

    Respecte les règles de gestion obligatoires de la norme EN 16931 :
      BR-08/BR-09 : adresse postale vendeur + code pays
      BR-10/BR-11 : adresse postale acheteur + code pays
      BR-16       : au moins 1 ligne de facture
      BG-13       : section Delivery (obligatoire même si vide)
      BG-22       : totaux monétaires complets
      BG-23       : ventilation TVA par catégorie
    """
    NSMAP = {
        "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
        "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
        "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
        "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
    }

    def _el(ns, tag):
        return f"{{{NSMAP[ns]}}}{tag}"

    root = etree.Element(_el("rsm", "CrossIndustryInvoice"), nsmap=NSMAP)

    # ── ExchangedDocumentContext ─────────────────────────────────────────────
    ctx = etree.SubElement(root, _el("rsm", "ExchangedDocumentContext"))
    guide_ctx = etree.SubElement(ctx, _el("ram", "GuidelineSpecifiedDocumentContextParameter"))
    etree.SubElement(guide_ctx, _el("ram", "ID")).text = "urn:cen.eu:en16931:2017"

    # ── ExchangedDocument ────────────────────────────────────────────────────
    doc = etree.SubElement(root, _el("rsm", "ExchangedDocument"))
    etree.SubElement(doc, _el("ram", "ID")).text = inv.get("numero_facture") or "SANS-NUMERO"
    etree.SubElement(doc, _el("ram", "TypeCode")).text = str(inv.get("type_facture", "380"))

    issue_dt = etree.SubElement(doc, _el("ram", "IssueDateTime"))
    date_str = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    etree.SubElement(issue_dt, _el("udt", "DateTimeString"), format="102").text = date_str.replace("-", "")

    notes_text = inv.get("notes")
    if notes_text:
        note_el = etree.SubElement(doc, _el("ram", "IncludedNote"))
        etree.SubElement(note_el, _el("ram", "Content")).text = str(notes_text)[:500]

    # ── SupplyChainTradeTransaction ──────────────────────────────────────────
    txn = etree.SubElement(root, _el("rsm", "SupplyChainTradeTransaction"))

    # Lignes de facture (BR-16 : obligatoire d'en avoir au moins 1)
    for line in inv.get("lignes", []):
        line_item = etree.SubElement(txn, _el("ram", "IncludedSupplyChainTradeLineItem"))

        assoc_doc = etree.SubElement(line_item, _el("ram", "AssociatedDocumentLineDocument"))
        etree.SubElement(assoc_doc, _el("ram", "LineID")).text = str(line.get("numero", "1"))

        product = etree.SubElement(line_item, _el("ram", "SpecifiedTradeProduct"))
        etree.SubElement(product, _el("ram", "Name")).text = str(line.get("description", "Article"))[:256]

        agree = etree.SubElement(line_item, _el("ram", "SpecifiedLineTradeAgreement"))
        net_price = etree.SubElement(agree, _el("ram", "NetPriceProductTradePrice"))
        etree.SubElement(net_price, _el("ram", "ChargeAmount")).text = f"{_safe_float(line.get('prix_unitaire_ht')):.2f}"

        delivery = etree.SubElement(line_item, _el("ram", "SpecifiedLineTradeDelivery"))
        etree.SubElement(
            delivery, _el("ram", "BilledQuantity"),
            unitCode=str(line.get("unite", "C62"))
        ).text = f"{_safe_float(line.get('quantite'), 1.0):.4f}"

        settle_line = etree.SubElement(line_item, _el("ram", "SpecifiedLineTradeSettlement"))
        line_tax = etree.SubElement(settle_line, _el("ram", "ApplicableTradeTax"))
        etree.SubElement(line_tax, _el("ram", "TypeCode")).text = "VAT"
        etree.SubElement(line_tax, _el("ram", "CategoryCode")).text = str(line.get("code_tva", "S"))
        etree.SubElement(line_tax, _el("ram", "RateApplicablePercent")).text = f"{_safe_float(line.get('taux_tva', 20.0)):.2f}"

        line_summ = etree.SubElement(settle_line, _el("ram", "SpecifiedTradeSettlementLineMonetarySummation"))
        etree.SubElement(line_summ, _el("ram", "LineTotalAmount")).text = f"{_safe_float(line.get('montant_net_ht')):.2f}"

    # ── ApplicableHeaderTradeAgreement ──────────────────────────────────────
    agree_h = etree.SubElement(txn, _el("ram", "ApplicableHeaderTradeAgreement"))

    buyer_ref = inv.get("reference_commande")
    if buyer_ref:
        etree.SubElement(agree_h, _el("ram", "BuyerReference")).text = str(buyer_ref)

    # Vendeur (BG-4) — BR-08 adresse, BR-09 code pays
    vendeur = inv.get("vendeur", {}) or {}
    seller = etree.SubElement(agree_h, _el("ram", "SellerTradeParty"))
    etree.SubElement(seller, _el("ram", "Name")).text = vendeur.get("nom") or "Fournisseur inconnu"

    if vendeur.get("siret"):
        spec_legal = etree.SubElement(seller, _el("ram", "SpecifiedLegalOrganization"))
        etree.SubElement(spec_legal, _el("ram", "ID"), schemeID="0002").text = vendeur["siret"]

    seller_addr = etree.SubElement(seller, _el("ram", "PostalTradeAddress"))
    if vendeur.get("code_postal"):
        etree.SubElement(seller_addr, _el("ram", "PostcodeCode")).text = vendeur["code_postal"]
    if vendeur.get("adresse_ligne1"):
        etree.SubElement(seller_addr, _el("ram", "LineOne")).text = vendeur["adresse_ligne1"]
    if vendeur.get("adresse_ligne2"):
        etree.SubElement(seller_addr, _el("ram", "LineTwo")).text = vendeur["adresse_ligne2"]
    if vendeur.get("ville"):
        etree.SubElement(seller_addr, _el("ram", "CityName")).text = vendeur["ville"]
    etree.SubElement(seller_addr, _el("ram", "CountryID")).text = vendeur.get("pays_code") or "FR"

    if vendeur.get("tva_intra"):
        seller_tax = etree.SubElement(seller, _el("ram", "SpecifiedTaxRegistration"))
        etree.SubElement(seller_tax, _el("ram", "ID"), schemeID="VA").text = vendeur["tva_intra"]

    # Acheteur (BG-7) — BR-10 adresse, BR-11 code pays
    acheteur = inv.get("acheteur", {}) or {}
    buyer = etree.SubElement(agree_h, _el("ram", "BuyerTradeParty"))
    etree.SubElement(buyer, _el("ram", "Name")).text = acheteur.get("nom") or "Acheteur"

    if acheteur.get("siret"):
        spec_legal_b = etree.SubElement(buyer, _el("ram", "SpecifiedLegalOrganization"))
        etree.SubElement(spec_legal_b, _el("ram", "ID"), schemeID="0002").text = acheteur["siret"]

    buyer_addr = etree.SubElement(buyer, _el("ram", "PostalTradeAddress"))
    if acheteur.get("code_postal"):
        etree.SubElement(buyer_addr, _el("ram", "PostcodeCode")).text = acheteur["code_postal"]
    if acheteur.get("adresse_ligne1"):
        etree.SubElement(buyer_addr, _el("ram", "LineOne")).text = acheteur["adresse_ligne1"]
    if acheteur.get("ville"):
        etree.SubElement(buyer_addr, _el("ram", "CityName")).text = acheteur["ville"]
    etree.SubElement(buyer_addr, _el("ram", "CountryID")).text = acheteur.get("pays_code") or "FR"

    if acheteur.get("tva_intra"):
        buyer_tax = etree.SubElement(buyer, _el("ram", "SpecifiedTaxRegistration"))
        etree.SubElement(buyer_tax, _el("ram", "ID"), schemeID="VA").text = acheteur["tva_intra"]

    # ── ApplicableHeaderTradeDelivery (BG-13 : obligatoire, peut être vide) ─
    etree.SubElement(txn, _el("ram", "ApplicableHeaderTradeDelivery"))

    # ── ApplicableHeaderTradeSettlement ─────────────────────────────────────
    settle_h = etree.SubElement(txn, _el("ram", "ApplicableHeaderTradeSettlement"))

    devise = inv.get("devise") or "EUR"
    etree.SubElement(settle_h, _el("ram", "InvoiceCurrencyCode")).text = devise

    # Moyens de paiement (BG-16) avec IBAN + BIC
    payment_code = inv.get("code_moyen_paiement")
    if payment_code:
        pm = etree.SubElement(settle_h, _el("ram", "SpecifiedTradeSettlementPaymentMeans"))
        etree.SubElement(pm, _el("ram", "TypeCode")).text = str(payment_code)
        if inv.get("iban"):
            acct = etree.SubElement(pm, _el("ram", "PayeePartyCreditorFinancialAccount"))
            etree.SubElement(acct, _el("ram", "IBANID")).text = inv["iban"].replace(" ", "")
            if inv.get("bic"):
                inst = etree.SubElement(pm, _el("ram", "PayeeSpecifiedCreditorFinancialInstitution"))
                etree.SubElement(inst, _el("ram", "BICID")).text = inv["bic"].replace(" ", "")

    # Ventilation TVA (BG-23) — DOIT être avant SpecifiedTradePaymentTerms (ordre XSD)
    for vat_break in inv.get("ventilation_tva", []):
        tax_el = etree.SubElement(settle_h, _el("ram", "ApplicableTradeTax"))
        etree.SubElement(tax_el, _el("ram", "CalculatedAmount")).text = f"{_safe_float(vat_break.get('montant_tva')):.2f}"
        etree.SubElement(tax_el, _el("ram", "TypeCode")).text = "VAT"
        etree.SubElement(tax_el, _el("ram", "BasisAmount")).text = f"{_safe_float(vat_break.get('base_ht')):.2f}"
        etree.SubElement(tax_el, _el("ram", "CategoryCode")).text = str(vat_break.get("code_tva", "S"))
        etree.SubElement(tax_el, _el("ram", "RateApplicablePercent")).text = f"{_safe_float(vat_break.get('taux')):.2f}"

    # Échéance de paiement (BT-9) — APRÈS ApplicableTradeTax
    date_ech = inv.get("date_echeance")
    if date_ech:
        pt = etree.SubElement(settle_h, _el("ram", "SpecifiedTradePaymentTerms"))
        due_dt = etree.SubElement(pt, _el("ram", "DueDateDateTime"))
        etree.SubElement(due_dt, _el("udt", "DateTimeString"), format="102").text = date_ech.replace("-", "")

    # Totaux monétaires (BG-22)
    summ = etree.SubElement(settle_h, _el("ram", "SpecifiedTradeSettlementHeaderMonetarySummation"))
    ht = _safe_float(inv.get("montant_ht"))
    tva_total = _safe_float(inv.get("montant_tva"))
    ttc = _safe_float(inv.get("montant_ttc"))
    du = _safe_float(inv.get("montant_du")) or ttc
    sum_lines_net = _safe_float(inv.get("montant_total_lignes_net")) or ht

    etree.SubElement(summ, _el("ram", "LineTotalAmount")).text = f"{sum_lines_net:.2f}"
    etree.SubElement(summ, _el("ram", "TaxBasisTotalAmount"), currencyID=devise).text = f"{ht:.2f}"
    etree.SubElement(summ, _el("ram", "TaxTotalAmount"), currencyID=devise).text = f"{tva_total:.2f}"
    etree.SubElement(summ, _el("ram", "GrandTotalAmount"), currencyID=devise).text = f"{ttc:.2f}"
    etree.SubElement(summ, _el("ram", "DuePayableAmount"), currencyID=devise).text = f"{du:.2f}"

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ─────────────────────────────────────────────────────────────────────────────
# 5) Embedding XML dans le PDF + marqueurs PDF/A-3b
# ─────────────────────────────────────────────────────────────────────────────

def _inject_pdfa3_markers(pdf_bytes: bytes) -> bytes:
    """
    Injecte les marqueurs PDF/A-3b dans le PDF Factur-X :
      - OutputIntent sRGB (requis par PDF/A-3)
      - Patch XMP : ajoute pdfaid:part=3 / pdfaid:conformance=B

    Stratégie : modifier le XMP existant plutôt que le remplacer,
    car la lib factur-x génère son propre XMP qu'on enrichit.
    """
    if b"pdfaid" in pdf_bytes and b"OutputIntent" in pdf_bytes:
        return pdf_bytes  # Déjà conforme, rien à faire

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject, DictionaryObject, NameObject,
        TextStringObject, DecodedStreamObject, IndirectObject,
    )

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    writer.clone_reader_document_root(reader)

    # 1. OutputIntent sRGB
    if b"OutputIntent" not in pdf_bytes:
        oi = DictionaryObject()
        oi[NameObject("/Type")] = NameObject("/OutputIntent")
        oi[NameObject("/S")] = NameObject("/GTS_PDFA1")
        oi[NameObject("/OutputConditionIdentifier")] = TextStringObject("sRGB")
        oi[NameObject("/RegistryName")] = TextStringObject("http://www.color.org")
        oi[NameObject("/Info")] = TextStringObject("sRGB IEC61966-2.1")
        writer._root_object[NameObject("/OutputIntents")] = ArrayObject(
            [writer._add_object(oi)]
        )

    # 2. Patch XMP
    pdfaid_block = b' xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"'
    pdfaid_tags = (
        b'<pdfaid:part>3</pdfaid:part>'
        b'<pdfaid:conformance>B</pdfaid:conformance>'
    )

    existing_xmp = b""
    meta_ref = writer._root_object.get("/Metadata")
    if meta_ref is not None:
        try:
            meta_obj = meta_ref.get_object() if isinstance(meta_ref, IndirectObject) else meta_ref
            existing_xmp = meta_obj.get_data()
        except Exception:
            pass

    if existing_xmp and b"pdfaid" not in existing_xmp:
        marker = b"</rdf:Description>"
        if marker in existing_xmp:
            patched = existing_xmp.replace(
                b'<rdf:Description rdf:about=""',
                b'<rdf:Description rdf:about=""' + pdfaid_block, 1,
            )
            patched = patched.replace(marker, pdfaid_tags + marker, 1)
            existing_xmp = patched
        else:
            marker2 = b"</rdf:RDF>"
            if marker2 in existing_xmp:
                inject = (
                    b'<rdf:Description rdf:about=""' + pdfaid_block + b'>'
                    + pdfaid_tags + b'</rdf:Description>'
                )
                existing_xmp = existing_xmp.replace(marker2, inject + marker2, 1)

        xmp_stream = DecodedStreamObject()
        xmp_stream.set_data(existing_xmp)
        xmp_stream[NameObject("/Type")] = NameObject("/Metadata")
        xmp_stream[NameObject("/Subtype")] = NameObject("/XML")
        writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp_stream)

    elif not existing_xmp:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        xmp = (
            '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about=""'
            ' xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
            '<pdfaid:part>3</pdfaid:part>'
            '<pdfaid:conformance>B</pdfaid:conformance>'
            '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Factur-X Invoice</rdf:li></rdf:Alt></dc:title>'
            '<xmp:CreateDate>' + now + '</xmp:CreateDate>'
            '</rdf:Description></rdf:RDF></x:xmpmeta>'
            '<?xpacket end="w"?>'
        ).encode("utf-8")
        xmp_stream = DecodedStreamObject()
        xmp_stream.set_data(xmp)
        xmp_stream[NameObject("/Type")] = NameObject("/Metadata")
        xmp_stream[NameObject("/Subtype")] = NameObject("/XML")
        writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp_stream)

    buf = io.BytesIO()
    writer.write(buf)
    logger.info("PDF/A-3b marqueurs injectés (%d → %d octets)", len(pdf_bytes), buf.tell())
    return buf.getvalue()


def embed_facturx_in_pdf(pdf_bytes: bytes, xml_bytes: bytes) -> bytes:
    """
    Génère le Factur-X final en 2 étapes :
      1. Embedding XML dans le PDF (lib Akretion factur-x)
      2. Injection des marqueurs PDF/A-3b (OutputIntent + pdfaid XMP)
    """
    facturx_bytes = generate_from_binary(
        pdf_bytes,
        xml_bytes,
        flavor="factur-x",
        level=FACTURX_PROFILE,
        check_xsd=True,
        xmp_compression=False,
    )
    return _inject_pdfa3_markers(facturx_bytes)
