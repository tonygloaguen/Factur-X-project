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
import unicodedata
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import requests
from lxml import etree

import fitz  # PyMuPDF : extraction texte natif + OCR via Tesseract

from facturx import generate_from_binary  # Akretion : embedding PDF/A-3 + XML

from unece_units import to_unece  # Normalisation unité → code UN/ECE Rec 20

logger = logging.getLogger("orchestrator")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (depuis variables d'environnement)
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# gemini-2.0-flash : modèle stable sans thinking mode (contrairement à 2.5-flash
# qui active le thinking par défaut depuis mars 2025 → troncatures JSON).
# Surcharger via GEMINI_MODEL=gemini-2.5-flash si on veut le modèle thinking
# (avec thinkingBudget=0 dans le payload pour neutraliser le thinking).
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
# CKV_SEC_1 : la clé API est passée en header (x-goog-api-key) et NON dans l'URL
# pour éviter qu'elle apparaisse dans les logs HTTP (access logs, proxies, HAR, etc.)
GEMINI_BASE_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
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
    "accusé de réception", "accuse de reception",
]

# Deny SOFT : souvent pas une facture, MAIS peut apparaître dans une vraie
# (ex: assurance pro MSA, mutuelle entreprise...).
# Bloquer seulement si le score facture est faible (< 3).
# Note : "rdv" retiré — 3 lettres, trop court, faux positifs fréquents.
DENY_SOFT_KEYWORDS = [
    "notification", "rappel", "documents en retard",
    "convocation", "attestation",
    "relevé de remboursement", "remboursement",
    "mutuelle", "assurance",
    "consultation", "rendez-vous",
]

# Taille maximale du texte extrait pour être candidat facture.
# Au-delà de 60 000 caractères, le document est quasi-certainement un
# catalogue/tarif (ex : TARIF IN-IPSO 2026 : 382 000 chars, 200+ pages).
# Note : 15 000 était trop restrictif pour les factures détaillées multi-pages
# (ex : IPSO FAC0042658 sur 5 pages avec de nombreuses lignes d'aménagement).
# Note : 30 000 rejetait encore de vraies factures très détaillées
# (ex : RAISON HOME F2026-044 sur 19 pages : 34 228 chars, 22 035 € TTC).
MAX_TEXT_LEN_FOR_INVOICE = 60_000

# Taille maximale du fichier PDF pour être candidat facture (5 Mo).
MAX_PDF_SIZE_FOR_INVOICE = 5_000_000


def is_invoice_candidate(text: str) -> tuple[bool, str]:
    """
    Analyse le texte OCR et décide si le document est une facture candidate.

    Retourne (True, raison) ou (False, raison).
    Le filtre est intentionnellement large (FP tolérés, FN évités) :
    Gemini confirme ensuite avec "est_facture": true/false.
    """
    text_l = (text or "").lower()

    # Garde-fou A : texte trop long → catalogue/tarif, pas une facture.
    # Une vraie facture tient en quelques pages (< 15 000 chars).
    # Exemple : TARIF IN-IPSO 2026 = 382 000 chars → rejeté ici.
    if len(text_l) > MAX_TEXT_LEN_FOR_INVOICE:
        return False, f"text_too_long:{len(text_l)}"

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


def build_supplier_folder_name(inv: dict) -> str:
    """Construit le nom du sous-dossier Drive fournisseur.

    Utilisé dans le nom de fichier (pas le dossier Drive depuis batch 2).
    Exemple : 'IPSO', 'GPDIS', 'EDF'
    """
    vendeur = inv.get("vendeur", {}) or {}
    name = (vendeur.get("nom_court") or vendeur.get("nom") or "Fournisseur_Inconnu").strip()
    return sanitize_filename(name.replace(" ", "_")) or "Fournisseur_Inconnu"


# ─────────────────────────────────────────────────────────────────────────────
# Client final (batch 2) — extraction heuristique + blacklist
# ─────────────────────────────────────────────────────────────────────────────

# Entités à NE JAMAIS retenir comme client final.
# Stockées en clair pour lisibilité ; la comparaison se fait après normalisation.
CLIENT_FINAL_BLACKLIST: list[str] = [
    "JMT Déco",
    "JMT Deco",
    "Raison Home",
    "Bénédicte Gloaguen",
    "Benedicte Gloaguen",
    "B. Gloaguen",
]

# Mots usuels du mobilier/bâtiment qui peuvent apparaître capitalisés
# dans des descriptions mais ne sont PAS des noms de clients.
_PRODUCT_WORDS: frozenset[str] = frozenset([
    "cuisine", "cuisines", "meuble", "meubles", "tiroir", "tiroirs",
    "pose", "livraison", "montage", "installation", "service", "services",
    "total", "frais", "taxes", "modele", "reference",
    "article", "produit", "fourniture", "acompte", "solde",
    "renovation", "travaux", "chantier", "amenagement",
    # Batch 3: termes supplémentaires bâtiment/cuisine/salle de bain
    "salle", "bain", "dressing", "placard", "rangement",
    "electromenager", "electromenagers", "robinetterie",
    "carrelage", "parquet", "peinture", "electricite",
    "plomberie", "menuiserie", "maconnerie", "isolation",
    "baignoire", "douche", "lavabo", "evier", "credence",
    "quincaillerie", "accessoire", "accessoires",
    # Hardening: matériaux, finitions et désignations produit capitulatés dans les descriptions
    # qui peuvent ressembler à des noms propres (ex: "Cuve Sable Métal Bonde Manuel Chromé").
    "cuve", "sable", "bonde", "chrome", "metal", "inox",
    "mitigeur", "mitigeurs", "vasque", "vasques",
    "classique", "moderne", "design", "standard", "premium",
    "blanc", "noir", "gris", "beige", "anthracite",
])

# Motif pour les champs métier signalant explicitement le client final dans le texte OCR.
# Priorité maximale : plus fiable que l'acheteur ou les lignes.
_CONTREMARQUE_RE = re.compile(
    r'(?:contremarque|chantier|rep[eè]re\s*(?:client)?|ref\.?\s*chantier|'
    r'r[eé]f[eé]rence(?!\s*(?:client|commande))|'  # "Référence : X" mais pas "Réf. client"
    r'client\s*final|destinataire\s*final|dossier\s*client|affaire)\s*[:\-–]\s*'
    r'([A-Za-zÀ-ÿ0-9][^\n,;|]{1,60})',
    re.IGNORECASE,
)

# Contremarque abrégée "C/M NOM" ou "CM NOM" — format courant chez les fournisseurs
# cuisine/salle de bains (Interbat, BAUS, etc.).
# Exemple : "C/M MARTINEAU" dans la section lignes de la facture FA131242.
_CM_RE = re.compile(
    r'\bC[./]?M\.?\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ]{2,}(?:[ \t][A-ZÀ-Ÿ][A-Za-zÀ-ÿ]{2,})*)',
)

# Ligne opérationnelle "86 LEROY 17.03.2026" — format Eberhardt et fournisseurs similaires.
# Structure : <numéro_ref> <NOM_CLIENT> <date_livraison>.
# La présence d'une date après le nom ancre le candidat entre deux éléments structurés,
# ce qui le rend plus fiable que les noms propres seuls dans les lignes de description.
# Exclut "RG 140980 Bamba" (pas de date immédiatement après Bamba).
_OP_LINE_RE = re.compile(
    r'(?:^|[\s\n])\d+\s+'
    r'([A-ZÀ-Ÿ][A-Za-zÀ-ÿ]{2,}(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-ÿ]{2,})*)'
    r'\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}(?=[\s\n,;|]|$)',
    re.MULTILINE,
)

# Nom propre : deux mots ou plus commençant par une majuscule, min 3 chars chacun.
# Couvre : "Brigitte Whitechurch", "Jean-Paul Dupont", "Marie Leclerc".
_WORD_CAP = r'[A-ZÀ-ÿ][a-zA-Zà-ÿ]{2,}(?:\-[A-ZÀ-ÿ][a-zA-Zà-ÿ]{2,})*'
_PROPER_NAME_RE = re.compile(
    r'\b(' + _WORD_CAP + r'(?:\s+' + _WORD_CAP + r')+)\b'
)


def _normalize_for_cmp(s: str) -> str:
    """Normalise pour comparaison : minuscules, sans accents, espaces normalisés."""
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _clean_candidate_name(candidate: str) -> str:
    """Retire les mots-produits en tête et en queue d'un candidat nom propre.

    Évite les faux positifs comme "Pose Cuisine Martin Dupont" → "Martin Dupont".
    Retourne une chaîne vide si tous les mots sont des mots-produits.
    """
    words = candidate.split()
    while words and _normalize_for_cmp(words[0]) in _PRODUCT_WORDS:
        words = words[1:]
    while words and _normalize_for_cmp(words[-1]) in _PRODUCT_WORDS:
        words = words[:-1]
    return " ".join(words)


def _is_client_blacklisted(name: str, vendor_name: str = "") -> bool:
    """Retourne True si le nom ne peut pas être un client final valide.

    Vérifie :
      1. Présence dans CLIENT_FINAL_BLACKLIST (JMT Déco, Raison Home, Bénédicte Gloaguen…)
      2. Identité avec le fournisseur (acheteur = vendeur → pas un client final)
      3. Mot produit seul ou trop court
    """
    norm = _normalize_for_cmp(name)
    if not norm or len(norm) < 3:
        return True

    # Mots produits seuls (ex : "Cuisine Pose" → blacklisté)
    words = norm.split()
    if all(w in _PRODUCT_WORDS for w in words):
        return True

    # Blacklist statique
    for entry in CLIENT_FINAL_BLACKLIST:
        bl = _normalize_for_cmp(entry)
        if bl and (bl in norm or norm in bl):
            return True

    # Même nom que le fournisseur
    if vendor_name:
        vn = _normalize_for_cmp(vendor_name)
        if vn and (vn in norm or norm in vn):
            return True

    return False


def extract_final_client(invoice_data: dict, ocr_text: str = "") -> str:
    """
    Extrait le client final depuis les données de facture et le texte OCR.

    Ordre de priorité :
      1. Contremarque / chantier / repère explicite dans l'OCR
         → signal le plus fiable : "Contremarque : GARNIER" → GARNIER
      2. Noms propres dans les descriptions de lignes (après filtre blacklist)
         → "Brigitte Whitechurch" dans lignes BAUS
      3. acheteur.nom s'il est différent du fournisseur et non blacklisté
         → cas simple où l'acheteur est le client final réel
      4. Fallback : "A_CLASSER" si aucune info fiable

    La blacklist exclut : JMT Déco, Raison Home, Bénédicte Gloaguen,
    le fournisseur lui-même.
    """
    vendeur = invoice_data.get("vendeur") or {}
    vendor_name = vendeur.get("nom_court") or vendeur.get("nom") or ""
    # Villes connues dans les données structurées : rejeter si le pattern OCR capture
    # la ville du chantier plutôt que le client (ex: "Chantier : MAGNY LES HAMEAUX").
    _known_cities = {
        v.upper().strip()
        for v in [
            (invoice_data.get("acheteur") or {}).get("ville") or "",
            (invoice_data.get("vendeur") or {}).get("ville") or "",
        ]
        if v.strip()
    }

    # ── Priorité 1 : contremarque / chantier / référence / C/M dans l'OCR ───────
    if ocr_text:
        # Format abrégé "C/M NOM" testé en premier : notation explicite la plus fiable.
        # Évite que _CONTREMARQUE_RE capte une adresse de livraison entremêlée par
        # PyMuPDF avant d'atteindre la ligne "C/M CLIENT" (ex: FA131485 Interbat).
        for m in _CM_RE.finditer(ocr_text):
            candidate = m.group(1).strip()
            if candidate and not _is_client_blacklisted(candidate, vendor_name):
                logger.debug("client_final (C/M) : %s", candidate)
                return candidate.upper()

        for m in _CONTREMARQUE_RE.finditer(ocr_text):
            candidate = m.group(1).strip().rstrip(".,;: ")
            # Rejeter les captures qui contiennent une forme sociale entre parenthèses
            # (ex : "BAUS INT'L (S.N.P.E.C)") — indique une adresse, pas un client final.
            if "(" in candidate or ")" in candidate:
                continue
            # Rejeter si le candidat est une ville connue des données structurées
            # (ex: "Chantier : MAGNY LES HAMEAUX" → ville de livraison, pas un client).
            if candidate.upper() in _known_cities:
                continue
            if candidate and not _is_client_blacklisted(candidate, vendor_name):
                logger.debug("client_final (contremarque) : %s", candidate)
                return candidate.upper()

        # Ligne opérationnelle "86 LEROY 17.03.2026" — Eberhardt et fournisseurs similaires
        # (numéro_ref + NOM_CLIENT + date : la date ancre le client entre deux repères)
        for m in _OP_LINE_RE.finditer(ocr_text):
            candidate = m.group(1).strip()
            if candidate and not _is_client_blacklisted(candidate, vendor_name):
                logger.debug("client_final (ligne opérationnelle) : %s", candidate)
                return candidate.upper()

    # ── Priorité 2 : noms propres dans les lignes de facture ───────────────────
    # Exige au moins 2 mots après nettoyage pour éviter les noms de gammes produit
    # qui ressemblent à des prénoms (ex : "Evier Amelia" → "Amelia" seul → rejeté).
    for ligne in (invoice_data.get("lignes") or []):
        desc = (ligne.get("description") or "").strip()
        if not desc:
            continue
        for m in _PROPER_NAME_RE.finditer(desc):
            candidate = _clean_candidate_name(m.group(1).strip())
            if not candidate or len(candidate.split()) < 2:
                continue
            if not _is_client_blacklisted(candidate, vendor_name):
                logger.debug("client_final (ligne) : %s", candidate)
                return candidate

    # ── Priorité 3 : acheteur.nom si fiable ────────────────────────────────────
    acheteur = invoice_data.get("acheteur") or {}
    buyer_name = (acheteur.get("nom_court") or acheteur.get("nom") or "").strip()
    if buyer_name and not _is_client_blacklisted(buyer_name, vendor_name):
        logger.debug("client_final (acheteur) : %s", buyer_name)
        return buyer_name

    # ── Priorité 3b : reference_commande / notes — champs structurés ──────────
    # Cas IN IPSO : acheteur blacklisté (JMT Déco) mais reference_commande
    # ou notes peut contenir le client final réel.
    # Stratégie : d'abord _CONTREMARQUE_RE (ex: "Chantier: GARNIER"),
    # puis _PROPER_NAME_RE en fallback (ex: "Brigitte Whitechurch").
    for field_text in [invoice_data.get("reference_commande"), invoice_data.get("notes")]:
        if not field_text:
            continue
        text = str(field_text)
        for m in _CONTREMARQUE_RE.finditer(text):
            candidate = m.group(1).strip().rstrip(".,;: ")
            if candidate and not _is_client_blacklisted(candidate, vendor_name):
                logger.debug("client_final (ref/notes contremarque) : %s", candidate)
                return candidate.upper()
        for m in _PROPER_NAME_RE.finditer(text):
            candidate = _clean_candidate_name(m.group(1).strip())
            if not candidate:
                continue
            if not _is_client_blacklisted(candidate, vendor_name):
                logger.debug("client_final (ref/notes nom propre) : %s", candidate)
                return candidate

    # ── Fallback ────────────────────────────────────────────────────────────────
    logger.debug("client_final : fallback A_CLASSER")
    return "A_CLASSER"


def is_pdf_attachment(part: dict) -> bool:
    """Retourne True si une partie de message Gmail est un PDF à traiter.

    Règles d'acceptation (dans l'ordre) :
      1. mimeType == "application/pdf"  → accepté quel que soit le filename
      2. filename se termine par ".pdf" (insensible à la casse) → accepté quel
         que soit le mimeType (couvre application/octet-stream, etc.)
      3. Tout autre cas → refusé

    Justification : certains MUAs (Outlook, relais SMTP) déclarent les PDF avec
    Content-Type: application/octet-stream.  Le filename reste le signal fiable.
    Un filename absent ou vide ne déclenche pas de crash.
    """
    mime_type = part.get("mimeType", "")
    filename = (part.get("filename") or "").strip()

    if mime_type == "application/pdf":
        logger.debug("PJ acceptée par MIME application/pdf : %s", filename or "(sans nom)")
        return True

    if filename.lower().endswith(".pdf"):
        if mime_type == "application/octet-stream":
            logger.debug("PJ acceptée par extension .pdf (octet-stream) : %s", filename)
        else:
            logger.debug(
                "PJ acceptée par extension .pdf (MIME : %s) : %s", mime_type or "(vide)", filename
            )
        return True

    if filename or mime_type:
        logger.debug(
            "PJ ignorée (non PDF) : %s — MIME : %s", filename or "(sans nom)", mime_type or "(vide)"
        )
    return False


def is_transient_error(error: str) -> bool:
    """Retourne True si l'erreur est transiente : retriable, ne doit pas être persistée dans SQLite.

    Couvre :
      - rate_limit_429          : quota Gemini dépassé
      - erreur_transient:NNN    : HTTP 502/503/504 Gemini
      - erreur_transient:reseau : ConnectionError / Timeout / DNS
    """
    s = error or ""
    return s == "rate_limit_429" or s.startswith("erreur_transient:")


def build_client_folder_name(invoice_data: dict, ocr_text: str = "") -> str:
    """Retourne le nom de dossier Drive sanitisé pour le client final.

    Espaces → underscores, caractères spéciaux éliminés.
    Exemple : "GARNIER", "Brigitte_Whitechurch", "A_CLASSER"
    """
    client = extract_final_client(invoice_data, ocr_text)
    return sanitize_filename(client.replace(" ", "_")) or "A_CLASSER"


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ── Arithmétique monétaire exacte (P1 : BR-CO-13 / BR-S-08) ──────────────────
# Les totaux du LLM ne sont JAMAIS dignes de confiance : on recalcule tout en
# Decimal (ROUND_HALF_UP, quantize 0.01) et on écrase les agrégats de Gemini.
_CENT = Decimal("0.01")


def _dec(val) -> Decimal:
    """Convertit une valeur quelconque en Decimal (via str pour éviter le bruit
    binaire du float). Retombe sur 0 si non convertible."""
    return Decimal(str(_safe_float(val)))


def _q2(val: Decimal) -> Decimal:
    """Arrondit un Decimal au centime (ROUND_HALF_UP)."""
    return val.quantize(_CENT, rounding=ROUND_HALF_UP)


#: Conditions de paiement (BT-20) par défaut quand le montant dû est positif
#: mais qu'aucune échéance (BT-9) ni condition n'est extraite (cf. BR-CO-25).
DEFAULT_PAYMENT_TERMS = "Paiement à réception de facture"
#: Conditions de paiement (BT-20) pour une facture déjà réglée.
PAID_PAYMENT_TERMS = "Facture acquittée"

#: Indices texte fiables d'une facture acquittée / payée / soldée.
#: Volontairement conservateur (expressions sans ambiguïté) pour éviter de
#: marquer payée une facture qui décrit seulement ses modalités de paiement.
_PAID_INVOICE_MARKERS: tuple[str, ...] = (
    "facture acquittée", "facture acquittee",
    "facture payée", "facture payee",
    "facture réglée", "facture reglee",
    "facture soldée", "facture soldee",
    "acquittée le", "acquittee le",
    "payée le", "payee le",
    "réglée le", "reglee le",
    "déjà réglé", "deja regle",
    "déjà payé", "deja paye",
    "solde nul", "solde : 0", "reste à payer : 0", "reste a payer : 0",
)


def _is_invoice_paid(inv: dict) -> bool:
    """Détecte si la facture porte des indices fiables de règlement (acquittée).

    Sources, par ordre de fiabilité :
      1. ``mention_acquittee`` : booléen explicite extrait par Gemini.
      2. Recherche d'expressions sans ambiguïté dans ``conditions_paiement``
         et ``notes`` (texte libre de la facture).

    Returns:
        True si la facture est manifestement déjà payée, False sinon.
    """
    if inv.get("mention_acquittee") is True:
        return True
    haystack = " ".join(
        str(inv.get(field) or "") for field in ("conditions_paiement", "notes")
    ).lower()
    return any(marker in haystack for marker in _PAID_INVOICE_MARKERS)


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
    "tva_intra": "N° TVA intracom AVEC préfixe pays (FR.., DE.., BE..). OBLIGATOIRE si présent — chercher 'USt-IdNr', 'VAT', 'TVA', 'IntraCom'. null seulement si vraiment absent",
    "adresse_ligne1": "Numéro et rue ou null",
    "adresse_ligne2": "Complément ou null",
    "code_postal": "string ou null",
    "ville": "string ou null",
    "pays_code": "FR, DE, etc. Défaut FR si non trouvé"
  },

  "acheteur": {
    "nom": "Raison sociale complète ou null",
    "siret": "string ou null",
    "tva_intra": "N° TVA acheteur AVEC préfixe pays — requis en cas d'autoliquidation (mention 'autoliquidation'/'reverse charge'/'Steuerschuldnerschaft des Leistungsempfängers'). null sinon",
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
      "unite": "unité TELLE QU'IMPRIMÉE sur la facture (ex: 'm²', 'ml', 'U', 'h', 'kg') — ne pas convertir",
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
  "conditions_paiement": "Conditions de règlement telles qu'écrites sur la facture (ex: 'Paiement à 30 jours', 'Paiement à réception') ou null",
  "mention_acquittee": false,
  "iban": "string ou null",
  "bic": "string ou null",
  "notes": "informations complémentaires ou null"
}

Règles CRITIQUES :
- Le VENDEUR est celui qui ÉMET la facture (le fournisseur qui demande le paiement)
- L'ACHETEUR est celui qui REÇOIT la facture (le client qui doit payer)
- VENDEUR = l'expéditeur de l'email (champ "Expéditeur" du contexte email). Si le domaine de l'expéditeur correspond à une société visible dans le PDF, c'est le VENDEUR.
- Ne confonds JAMAIS le client (destinataire de facturation, souvent en haut du PDF) avec le vendeur
- Le numero_facture doit IMPÉRATIVEMENT être extrait du corps du PDF. N'utilise JAMAIS le sujet de l'email pour remplir numero_facture. Si aucun numéro n'est visible dans le texte du PDF, retourne null.
- Le nom_court supprime les formes juridiques (SARL, SAS, GmbH, SA, etc.)
- Si tu ne trouves pas une donnée, mets null (ne devine JAMAIS un SIRET/TVA)
- Les montants sont des nombres décimaux (pas des strings)
- Le champ "lignes" est OBLIGATOIRE. Crée au moins 1 objet.
- Si tu ne peux pas identifier les lignes, crée UNE SEULE ligne "Prestation globale"
- Pour chaque ligne : prix_unitaire_ht * quantite doit = montant_net_ht
- La ventilation_tva regroupe les lignes par taux de TVA
- montant_du = montant restant à payer (= montant_ttc si pas d'acompte ; 0 si la facture est déjà réglée)
- conditions_paiement = recopie EXACTE de la mention de règlement présente sur la facture, sinon null (ne l'invente pas)
- mention_acquittee = true UNIQUEMENT si la facture indique clairement qu'elle est déjà payée/acquittée/réglée/soldée
- pays_code TOUJOURS en 2 lettres ISO (FR, DE, BE, CH...). Défaut "FR"
- Un accusé de réception, un bon de livraison, un devis, une offre commerciale, une confirmation de commande ou tout document qui n'est PAS une demande de paiement → retourne "est_facture": false
"""


class GeminiJsonDecodeError(ValueError):
    """
    Levée quand le JSON renvoyé par Gemini est invalide après nettoyage ET retry.

    Distincte de ValueError/JSONDecodeError pour permettre au nœud call_gemini
    de la capturer spécifiquement et d'utiliser le statut 'erreur_json_permanent'
    (non-retriable dans StateDB, contrairement aux erreurs réseau transitoires).
    """


def clean_gemini_json(raw: str) -> str:
    """
    Nettoie le JSON renvoyé par Gemini avant parsing.

    Gemini viole parfois le format JSON malgré responseMimeType="application/json" :
      - Blocs markdown ```json ... ``` résiduels
      - Commentaires // en fin de ligne (invalides en JSON)
      - Virgules trailing avant } ou ] (invalides en JSON)
    """
    # Supprimer les blocs markdown ```json ... ```
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE)
    # Supprimer les commentaires // ... en fin de ligne
    raw = re.sub(r"//[^\n]*", "", raw)
    # Supprimer les virgules trailing avant } ou ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return raw.strip()


def _extract_response_text(response_data: dict) -> str:
    """Extrait le texte de réponse JSON depuis une réponse Gemini.

    Gemini 2.5-flash (mode thinking) renvoie les parts dans cet ordre :
      parts[0] = {"thought": True,  "text": "...raisonnement interne..."}
      parts[1] = {"thought": False, "text": "{...JSON facture...}"}

    Le code doit ignorer les parts de thinking et prendre la dernière
    part non-thought, qui contient la réponse JSON réelle.
    Ce helper est une défense en profondeur : thinkingBudget=0 dans le
    payload devrait déjà empêcher l'apparition de parts de thinking.
    """
    parts = response_data["candidates"][0]["content"]["parts"]
    response_parts = [p for p in parts if not p.get("thought", False)]
    # Si toutes les parts sont des thoughts (ne devrait pas arriver), fallback
    target = response_parts[-1] if response_parts else parts[-1]
    return target["text"]


# Budget de caractères du texte OCR envoyé à Gemini (limite le coût en tokens).
# Sur les factures longues, les totaux/TVA/paiements sont en DERNIÈRES pages :
# une troncature [:N] naïve les coupe (ex : RAISON HOME F2026-044, 19 pages,
# 34 228 chars — "Total affaire TTC" en position ~31 400). On envoie donc
# tête + queue pour préserver à la fois l'en-tête (n°, date, parties) et les totaux.
GEMINI_OCR_HEAD_CHARS = 6_000
GEMINI_OCR_TAIL_CHARS = 4_000


def _truncate_ocr_for_gemini(ocr_text: str) -> str:
    """Tronque le texte OCR en préservant le début ET la fin du document.

    - Texte court (≤ head+tail) : renvoyé intact.
    - Texte long : head + marqueur de coupure + tail, pour que Gemini voie
      l'en-tête (numéro, date, fournisseur, client) et les totaux finaux.
    """
    text = ocr_text or ""
    limit = GEMINI_OCR_HEAD_CHARS + GEMINI_OCR_TAIL_CHARS
    if len(text) <= limit:
        return text
    return (
        text[:GEMINI_OCR_HEAD_CHARS]
        + "\n\n[... document tronqué : pages intermédiaires omises ...]\n\n"
        + text[-GEMINI_OCR_TAIL_CHARS:]
    )


def call_gemini(ocr_text: str, email_context: str = "") -> dict:
    """
    Appelle l'API Gemini pour extraire les données structurées d'une facture.

    Gestion des erreurs :
      - 429 (rate limit) : backoff exponentiel avec Retry-After header
      - Autres HTTP 4xx/5xx : raise immédiat (propagé au nœud call_gemini)
      - JSON invalide : nettoyage + 1 retry → GeminiJsonDecodeError si toujours invalide

    Returns:
        dict : données JSON de la facture (champ "est_facture" inclus)

    Raises:
        requests.exceptions.HTTPError : erreur HTTP Gemini (dont 429)
        GeminiJsonDecodeError : JSON invalide après nettoyage et retry
        ValueError : GEMINI_API_KEY non configurée
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY non configurée dans les variables d'environnement")

    user_message = f"Texte OCR de la facture :\n\n{_truncate_ocr_for_gemini(ocr_text)}"
    if email_context:
        user_message += f"\n\nContexte email :\n{email_context[:2000]}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": GEMINI_SYSTEM_PROMPT + "\n\n" + user_message}]}
        ],
        "generationConfig": {
            "temperature": 0.1,               # Déterministe (extraction, pas créatif)
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,          # 8192 évite les troncatures JSON sur factures denses
            # Désactive le mode "thinking" de Gemini 2.5-flash :
            #   - Le thinking consomme le budget maxOutputTokens → troncature JSON
            #   - Il ajoute des parts {"thought": true} qui cassent l'extraction parts[0]
            #   - L'extraction JSON structurée ne bénéficie pas du raisonnement interne
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    max_attempts = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "4"))
    base_sleep = float(os.environ.get("GEMINI_BACKOFF_BASE_SECONDS", "5"))

    # CKV_SEC_1 : clé API passée en header x-goog-api-key (pas dans l'URL)
    # pour éviter qu'elle apparaisse dans les access logs / proxies HTTP
    _headers = {"x-goog-api-key": GEMINI_API_KEY}

    # ── Étape 1 : appel HTTP avec retry 429 ──────────────────────────────────
    last_status = None
    raw_text = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(GEMINI_BASE_URL, headers=_headers, json=payload, timeout=30)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as net_err:
            # Erreur réseau (DNS, connexion refusée, timeout) → transiente.
            sleep_s = min(60, base_sleep * (2 ** (attempt - 1))) + attempt * 0.3
            logger.warning(
                "Erreur réseau Gemini (%s) — tentative %d/%d, pause %.1fs",
                type(net_err).__name__, attempt, max_attempts, sleep_s,
            )
            if attempt < max_attempts:
                time.sleep(sleep_s)
                continue
            raise  # Dernière tentative : propager vers node_call_gemini
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

        if resp.status_code in (502, 503, 504):
            # Erreur transiente côté Gemini (service indisponible, gateway timeout).
            # Même stratégie que le 429 : backoff exponentiel + retry.
            # Ces codes NE doivent PAS marquer l'email comme "error" définitif dans SQLite.
            sleep_s = min(60, base_sleep * (2 ** (attempt - 1))) + attempt * 0.3
            logger.warning(
                "Gemini %d (service indisponible) — tentative %d/%d, pause %.1fs",
                resp.status_code, attempt, max_attempts, sleep_s,
            )
            time.sleep(sleep_s)
            continue

        if resp.status_code >= 400:
            logger.error("Erreur HTTP Gemini (%d): %s", resp.status_code, resp.text[:200])
            resp.raise_for_status()

        data = resp.json()
        raw_text = _extract_response_text(data)
        break  # Réponse HTTP 200 obtenue

    else:
        # Toutes les tentatives ont échoué sur une erreur transiente (429, 503, 502, 504).
        # On lève HTTPError avec le dernier status_code pour que node_call_gemini
        # puisse distinguer l'erreur transiente de l'erreur permanente.
        raise requests.exceptions.HTTPError(
            f"Gemini erreur transiente ({last_status}) après {max_attempts} tentatives",
            response=type("R", (), {"status_code": last_status})(),
        )

    # ── Étape 2 : parsing JSON avec nettoyage + 1 retry si invalide ──────────
    # Gemini peut renvoyer du JSON malformé (trailing commas, commentaires //, etc.)
    # même avec responseMimeType="application/json". On nettoie et on retente 1 fois.
    MAX_JSON_RETRIES = 1
    for json_attempt in range(1 + MAX_JSON_RETRIES):
        if json_attempt > 0:
            # Nouvel appel Gemini pour obtenir un JSON valide
            logger.warning(
                "JSON Gemini invalide — retry JSON %d/%d (nouvel appel API)...",
                json_attempt, MAX_JSON_RETRIES,
            )
            time.sleep(5)
            resp2 = requests.post(GEMINI_BASE_URL, headers=_headers, json=payload, timeout=30)
            if resp2.status_code >= 400:
                logger.error("Erreur HTTP Gemini au retry JSON (%d)", resp2.status_code)
                resp2.raise_for_status()
            data2 = resp2.json()
            raw_text = _extract_response_text(data2)

        cleaned = clean_gemini_json(raw_text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            if json_attempt < MAX_JSON_RETRIES:
                logger.warning(
                    "JSON Gemini invalide (tentative %d/%d) : %s",
                    json_attempt + 1, MAX_JSON_RETRIES, e,
                )
                logger.debug("JSON brut (500 chars) : %s", raw_text[:500])
            else:
                logger.error(
                    "JSON Gemini invalide même après nettoyage et %d retry : %s",
                    MAX_JSON_RETRIES, e,
                )
                logger.debug("JSON brut (500 chars) : %s", raw_text[:500])
                raise GeminiJsonDecodeError(
                    f"JSON invalide après nettoyage et {MAX_JSON_RETRIES} retry : {e}"
                ) from e


# ─────────────────────────────────────────────────────────────────────────────
# 3) Normalisation des données Gemini pour EN16931
# ─────────────────────────────────────────────────────────────────────────────

def party_identification_error(inv: dict) -> Optional[str]:
    """Détecte un défaut d'identification vendeur/acheteur qui ferait rejeter le
    schematron (BR-CO-26 / BR-S-02 / BR-AE-02), afin de router l'email en
    ``Factures-Erreur`` AVANT de produire un XML invalide (P4).

    Règles appliquées (mêmes conditions que le schematron officiel EN16931) :
      - BR-CO-26 : le vendeur doit porter au moins un identifiant — TVA (BT-31)
        ou SIRET/immatriculation légale (BT-30).
      - BR-S-02 : une ligne au taux standard (code « S ») exige la TVA vendeur.
      - BR-AE-02 : en autoliquidation (« AE »), TVA vendeur ET identifiant
        acheteur (TVA ou SIRET) sont obligatoires.

    Args:
        inv: Facture normalisée (ou brute) — lit vendeur/acheteur/lignes.

    Returns:
        Une chaîne de motif si un rejet est certain, sinon ``None``.
    """
    vendeur = inv.get("vendeur") or {}
    acheteur = inv.get("acheteur") or {}
    seller_vat = str(vendeur.get("tva_intra") or "").strip()
    seller_siret = str(vendeur.get("siret") or "").strip()
    buyer_vat = str(acheteur.get("tva_intra") or "").strip()
    buyer_siret = str(acheteur.get("siret") or "").strip()
    codes = {str(l.get("code_tva", "S")).upper() for l in (inv.get("lignes") or [])}

    if not seller_vat and not seller_siret:
        return "vendeur_non_identifie:BR-CO-26 (ni TVA ni SIRET vendeur)"
    if "S" in codes and not seller_vat:
        return "tva_vendeur_absente:BR-S-02 (ligne standard sans TVA vendeur)"
    if "AE" in codes:
        if not seller_vat:
            return "autoliq_tva_vendeur_absente:BR-AE-02 (TVA vendeur requise)"
        if not buyer_vat and not buyer_siret:
            return "autoliq_acheteur_non_identifie:BR-AE-02 (TVA/SIRET acheteur requis)"
    return None


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
            # P0 (fix cii @unitCode) : mapper l'unité libre de Gemini (« m² »,
            # « UNI », « ml »…) vers un code UN/ECE Rec 20 AVANT le CII. La valeur
            # brute ne doit jamais atteindre BilledQuantity/@unitCode (rejet
            # schematron « @unitCode is not allowed »).
            line["unite"] = to_unece(line.get("unite") or "C62")
            line.setdefault("code_tva", "S")
            line.setdefault("taux_tva", 20.0)
            pu = _safe_float(line.get("prix_unitaire_ht"))
            qty = _safe_float(line.get("quantite"), 1.0)
            net = _safe_float(line.get("montant_net_ht"))
            # BR-27 : BT-146 (Item net price) shall NOT be negative.
            # Prix négatif = avoir/remise : on inverse pu et qty pour préserver le total.
            if pu < 0:
                pu = -pu
                qty = -qty
            # Recalculer si un champ est manquant
            if net == 0.0 and pu > 0:
                net = round(pu * qty, 2)
            elif pu == 0.0 and net > 0 and qty > 0:
                pu = round(net / qty, 2)
            line["prix_unitaire_ht"] = pu
            line["quantite"] = qty
            line["montant_net_ht"] = net
    inv["lignes"] = lignes

    # --- BR-AE-02 : auto-liquidation (AE) exige un identifiant acheteur --------
    # Les factures BTP en auto-liquidation n'impriment souvent pas le SIRET de
    # l'acheteur. BUYER_SIRET (env) permet de l'injecter une bonne fois pour
    # toutes. Format : 14 chiffres sans espaces (SIRET de votre société).
    _buyer_siret_env = os.environ.get("BUYER_SIRET", "").strip()
    if _buyer_siret_env:
        has_ae = any(l.get("code_tva") == "AE" for l in lignes)
        if has_ae:
            acheteur = inv.get("acheteur") or {}
            if not acheteur.get("siret") and not acheteur.get("tva_intra"):
                acheteur["siret"] = _buyer_siret_env
                inv["acheteur"] = acheteur
                logger.info(
                    "BR-AE-02 : SIRET acheteur absent → injecté depuis BUYER_SIRET (%s)",
                    _buyer_siret_env,
                )

    # --- P1 : lignes exprimées en TTC (ex. Leroy Merlin chantier) ------------
    # Certains fournisseurs impriment le prix/montant de ligne TTC. On le détecte
    # quand la somme des montants de ligne colle au TTC de Gemini (et non au HT)
    # avec une TVA positive sur chaque ligne, puis on reconvertit en HT au taux
    # de la ligne AVANT toute agrégation (sinon BR-CO-13/BR-S-08 échouent).
    sum_net_raw = sum((_dec(l.get("montant_net_ht")) for l in lignes), Decimal(0))
    g_ht = _dec(inv.get("montant_ht"))
    g_ttc = _dec(inv.get("montant_ttc"))
    all_lines_taxed = bool(lignes) and all(_safe_float(l.get("taux_tva")) > 0 for l in lignes)
    if g_ttc > 0 and sum_net_raw > 0 and all_lines_taxed:
        close_to_ttc = abs(sum_net_raw - g_ttc) <= g_ttc * Decimal("0.02")
        exceeds_ht = g_ht > 0 and sum_net_raw > g_ht * Decimal("1.02")
        if close_to_ttc and exceeds_ht:
            for line in lignes:
                factor = Decimal(1) + _dec(line.get("taux_tva")) / Decimal(100)
                if factor > 0:
                    line["montant_net_ht"] = float(_q2(_dec(line.get("montant_net_ht")) / factor))
                    line["prix_unitaire_ht"] = float(_q2(_dec(line.get("prix_unitaire_ht")) / factor))
            logger.info("P1 : lignes détectées en TTC → reconverties en HT (%d ligne(s))", len(lignes))

    # --- Ventilation TVA (BG-23) recalculée depuis les lignes en Decimal ------
    # Toujours écrasée (jamais fiée à Gemini) : garantit BR-S-08 (base × taux =
    # montant de TVA par taux) et BR-CO-14 (TVA totale = Σ ventilation).
    tva_map: dict[tuple[str, str], dict] = {}
    for line in lignes:
        code = line.get("code_tva", "S")
        taux = _dec(line.get("taux_tva", 20.0))
        net = _dec(line.get("montant_net_ht"))
        key = (code, str(taux))
        entry = tva_map.setdefault(
            key, {"code_tva": code, "taux": float(taux), "_base": Decimal(0)}
        )
        entry["_base"] += net
    ventilation = []
    for entry in tva_map.values():
        base = _q2(entry["_base"])
        montant = _q2(base * _dec(entry["taux"]) / Decimal(100))
        ventilation.append({
            "code_tva": entry["code_tva"], "taux": entry["taux"],
            "base_ht": float(base), "montant_tva": float(montant),
        })
    inv["ventilation_tva"] = ventilation

    # --- Totaux monétaires (BG-22) recalculés en Decimal — écrasent Gemini ----
    # BR-CO-13 : TaxBasisTotal (BT-109) = Σ montants nets de ligne (pas de
    # remise/charge document-level ici). BR-CO-15 : TTC = HT + TVA totale.
    total_ht = _q2(sum((_dec(l.get("montant_net_ht")) for l in lignes), Decimal(0)))
    total_vat = _q2(sum((_dec(v["montant_tva"]) for v in ventilation), Decimal(0)))
    total_ttc = _q2(total_ht + total_vat)

    inv["montant_total_lignes_net"] = float(total_ht)
    inv["montant_ht"] = float(total_ht)
    inv["montant_tva"] = float(total_vat)
    inv["montant_ttc"] = float(total_ttc)
    ttc = float(total_ttc)

    # --- Montant dû (BT-115) + conditions de paiement (BT-20) : BR-CO-25 ------
    # BR-CO-25 : si le montant dû (BT-115) est positif, le XML DOIT contenir
    # soit une échéance (BT-9, date_echeance), soit des conditions de paiement
    # (BT-20, conditions_paiement). Sinon la validation schematron échoue.
    acquittee = _is_invoice_paid(inv)
    montant_du_brut = _safe_float(inv.get("montant_du"))
    if montant_du_brut > 0:
        # Reste à payer explicite et positif → on le conserve tel quel,
        # même si la facture porte une mention d'acquittement contradictoire.
        du = montant_du_brut
    elif acquittee:
        # Facture acquittée et aucun reste à payer positif → solde nul cohérent.
        du = 0.0
    else:
        # Cas normal (pas d'acompte ni de paiement) : dû = TTC.
        du = ttc
    inv["montant_du"] = du

    date_ech = inv.get("date_echeance")
    conditions = (inv.get("conditions_paiement") or "").strip()
    if du > 0 and not date_ech and not conditions:
        # Fallback BR-CO-25 : garantir BT-20 quand BT-9 et BT-20 sont absents.
        conditions = DEFAULT_PAYMENT_TERMS
    elif acquittee and du == 0.0 and not conditions:
        conditions = PAID_PAYMENT_TERMS
    inv["conditions_paiement"] = conditions or None

    # --- Divers ---
    # setdefault ne remplace pas une valeur None explicite (Gemini renvoie souvent
    # type_facture/devise = null) : on force donc un défaut métier via `or`.
    inv["devise"] = (inv.get("devise") or "EUR")
    inv["type_facture"] = (inv.get("type_facture") or "380")
    inv.setdefault("date_facture", datetime.now().strftime("%Y-%m-%d"))
    inv["date_facture"] = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
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

    # ── ApplicableHeaderTradeDelivery (BG-13 : requis par XSD CII) ──────────
    # PEPPOL-EN16931-R008 interdit les éléments vides → on ajoute la date de
    # livraison (BT-72) si disponible, sinon on replie sur la date de facture.
    delivery = etree.SubElement(txn, _el("ram", "ApplicableHeaderTradeDelivery"))
    delivery_date = inv.get("date_livraison") or inv.get("date_facture")
    if delivery_date:
        event = etree.SubElement(delivery, _el("ram", "ActualDeliverySupplyChainEvent"))
        occ = etree.SubElement(event, _el("ram", "OccurrenceDateTime"))
        etree.SubElement(occ, _el("udt", "DateTimeString"), format="102").text = delivery_date.replace("-", "")

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
        vat_code = str(vat_break.get("code_tva", "S"))
        tax_el = etree.SubElement(settle_h, _el("ram", "ApplicableTradeTax"))
        etree.SubElement(tax_el, _el("ram", "CalculatedAmount")).text = f"{_safe_float(vat_break.get('montant_tva')):.2f}"
        etree.SubElement(tax_el, _el("ram", "TypeCode")).text = "VAT"
        # BR-AE-10 : ExemptionReason (BT-120) — position XSD : après TypeCode
        if vat_code == "AE":
            etree.SubElement(tax_el, _el("ram", "ExemptionReason")).text = "Autoliquidation"
        etree.SubElement(tax_el, _el("ram", "BasisAmount")).text = f"{_safe_float(vat_break.get('base_ht')):.2f}"
        etree.SubElement(tax_el, _el("ram", "CategoryCode")).text = vat_code
        # BR-AE-10 : ExemptionReasonCode (BT-121) — position XSD : après CategoryCode
        if vat_code == "AE":
            etree.SubElement(tax_el, _el("ram", "ExemptionReasonCode")).text = "VATEX-EU-AE"
        etree.SubElement(tax_el, _el("ram", "RateApplicablePercent")).text = f"{_safe_float(vat_break.get('taux')):.2f}"

    # Conditions (BT-20) + échéance (BT-9) de paiement — APRÈS ApplicableTradeTax.
    # BR-CO-25 : si DuePayableAmount (BT-115) > 0, au moins l'un des deux doit
    # être présent. Ordre XSD CII : Description (BT-20) AVANT DueDateDateTime (BT-9).
    date_ech = inv.get("date_echeance")
    conditions = (inv.get("conditions_paiement") or "").strip()
    if conditions or date_ech:
        pt = etree.SubElement(settle_h, _el("ram", "SpecifiedTradePaymentTerms"))
        if conditions:
            etree.SubElement(pt, _el("ram", "Description")).text = conditions
        if date_ech:
            due_dt = etree.SubElement(pt, _el("ram", "DueDateDateTime"))
            etree.SubElement(due_dt, _el("udt", "DateTimeString"), format="102").text = date_ech.replace("-", "")

    # Totaux monétaires (BG-22)
    summ = etree.SubElement(settle_h, _el("ram", "SpecifiedTradeSettlementHeaderMonetarySummation"))
    ht = _safe_float(inv.get("montant_ht"))
    tva_total = _safe_float(inv.get("montant_tva"))
    ttc = _safe_float(inv.get("montant_ttc"))
    # Respecter un montant dû explicite (y compris 0 pour une facture acquittée).
    # Fallback sur le TTC uniquement si le champ est totalement absent.
    du = _safe_float(inv.get("montant_du")) if inv.get("montant_du") is not None else ttc
    sum_lines_net = _safe_float(inv.get("montant_total_lignes_net")) or ht
    # BT-113 (TotalPrepaidAmount) : part déjà payée (acompte ou facture acquittée).
    # BR-CO-16 : DuePayableAmount = GrandTotalAmount − TotalPrepaidAmount.
    # Sans ce poste, un montant dû < TTC violerait BR-CO-16.
    prepaid = round(ttc - du, 2)

    etree.SubElement(summ, _el("ram", "LineTotalAmount")).text = f"{sum_lines_net:.2f}"
    etree.SubElement(summ, _el("ram", "TaxBasisTotalAmount")).text = f"{ht:.2f}"
    etree.SubElement(summ, _el("ram", "TaxTotalAmount"), currencyID=devise).text = f"{tva_total:.2f}"
    etree.SubElement(summ, _el("ram", "GrandTotalAmount")).text = f"{ttc:.2f}"
    if prepaid > 0.005:
        etree.SubElement(summ, _el("ram", "TotalPrepaidAmount")).text = f"{prepaid:.2f}"
    etree.SubElement(summ, _el("ram", "DuePayableAmount")).text = f"{du:.2f}"

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
