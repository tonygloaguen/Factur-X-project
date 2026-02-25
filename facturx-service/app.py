#!/usr/bin/env python3
"""
Micro-service Flask : OCR + IA Gemini + Conversion Factur-X
============================================================
Reçoit un PDF, extrait le texte (natif + OCR si besoin), filtre localement
pour éviter Gemini sur des non-factures, appelle Gemini pour extraire des
données structurées, génère le XML Factur-X (profil EN16931), et produit
un PDF/A-3 Factur-X (XML embarqué).

Profil EN16931 — exigences clés :
- Au moins 1 ligne de facture (BR-16) avec quantité, prix unitaire, TVA
- Adresse postale vendeur (BG-5) avec code pays (BR-09)
- Adresse postale acheteur (BG-8) avec code pays (BR-11)
- Ventilation TVA (BG-23) avec catégorie, taux, base imposable, montant
- Delivery (BG-13) obligatoire
- Totaux monétaires complets (BG-22)
"""

import io
import json
import logging
import os
import re
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request, send_file
from lxml import etree

import fitz  # PyMuPDF (texte natif + OCR via Tesseract)

# Akretion / factur-x (embedding PDF/A-3 + XML)
from facturx import generate_from_binary

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

FACTURX_PROFILE = os.environ.get("FACTURX_PROFILE", "en16931").strip().lower()

MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}

# ---------------------------------------------------------------------------
# Filtrage local (évite d'appeler Gemini sur des documents non-factures)
# ---------------------------------------------------------------------------
FACTURE_KEYWORDS = [
    "facture", "invoice", "avoir",
    "tva", "vat",
    "ht", "ttc",
    "total", "montant",
    "échéance", "due date",
    "siret", "siren", "iban", "bic",
    "numéro", "numero", "n°",
]

# Deny HARD : jamais une facture, bloque toujours
DENY_HARD_KEYWORDS = [
    "curriculum vitae",
    "billet", "boarding pass", "vos billets",
]

# Deny SOFT : souvent pas une facture, mais peut apparaître dans une vraie
# facture (assurance pro, mutuelle entreprise, MSA...).
# Bloque uniquement si le score facture est faible (< 3).
DENY_SOFT_KEYWORDS = [
    "notification", "rappel", "documents en retard",
    "convocation", "attestation",
    "relevé de remboursement", "remboursement",
    "mutuelle", "assurance",
    "consultation", "rdv", "rendez-vous",
]


def is_invoice_candidate(text: str) -> tuple[bool, str]:
    text_l = (text or "").lower()

    # Hard deny : bloque toujours
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
        return False, f"score:{score}"
    return True, f"score:{score}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
    vendeur = inv.get("vendeur", {}) or {}
    nom_court = (vendeur.get("nom_court") or "Fournisseur").replace(" ", "_")
    date = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    numero = inv.get("numero_facture") or ""
    suffix = f"_{numero}" if numero else ""
    return sanitize_filename(f"{nom_court}_FacturX_{date}{suffix}.pdf")


def build_folder_name(inv: dict) -> str:
    date_str = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        dt = datetime.now()
    return f"{dt.year}-{dt.month:02d} {MOIS_FR.get(dt.month, '')}".strip()


def _safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 1) Extraction texte PDF (natif + OCR si besoin)
# ---------------------------------------------------------------------------
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    full_text: list[str] = []
    try:
        for page in doc:
            text = page.get_text("text") or ""
            if len(text.strip()) < 50:
                try:
                    tp = page.get_textpage_ocr(language="fra+deu+eng", dpi=300)
                    text = page.get_text("text", textpage=tp) or ""
                except Exception as e:
                    logger.warning("OCR échoué pour une page : %s", e)
            full_text.append(text)
    finally:
        doc.close()
    return "\n".join(full_text)


# ---------------------------------------------------------------------------
# 2) Gemini : extraction structurée (prompt EN16931)
# ---------------------------------------------------------------------------
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
      "unite": "C62 pour unité, HUR pour heure, KGM pour kg, MTR pour mètre, LTR pour litre, EA pour chacun",
      "prix_unitaire_ht": 100.00,
      "montant_net_ht": 100.00,
      "taux_tva": 20.0,
      "code_tva": "S pour standard, Z pour zéro, E pour exonéré, AE pour autoliquidation, K pour intracommunautaire"
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
  "code_moyen_paiement": "30 pour virement, 58 pour SEPA, 48 pour carte, 10 pour espèces, null si inconnu",
  "iban": "string ou null",
  "bic": "string ou null",
  "notes": "informations complémentaires ou null"
}

Règles CRITIQUES :
- Le VENDEUR est celui qui ÉMET la facture (le fournisseur)
- L'ACHETEUR est celui qui REÇOIT la facture
- Pour le type_facture : utilise le code UN/CEFACT (380=facture, 381=avoir)
- Le nom_court supprime les formes juridiques (SARL, SAS, GmbH, SA, etc.)
- Si tu ne trouves pas une donnée, mets null (ne devine JAMAIS un SIRET/TVA)
- Les montants sont des nombres décimaux (pas de strings)
- Le champ "lignes" est OBLIGATOIRE et CRITIQUE. Même s'il n'y a qu'une seule
  ligne sur la facture, crée un tableau avec au moins 1 objet.
- Si tu ne peux pas identifier les lignes individuelles, crée UNE SEULE ligne
  avec la description "Prestation globale" et le montant total HT.
- Pour chaque ligne, prix_unitaire_ht * quantite doit = montant_net_ht
- La ventilation_tva regroupe les lignes par taux de TVA
- montant_total_lignes_net = somme de tous les montant_net_ht des lignes
- montant_du = montant restant à payer (= montant_ttc si pas d'acompte)
- Les codes TVA : S=standard, Z=zéro, E=exonéré, AE=autoliquidation, K=intracom
- Le code unité par défaut est "C62" (unité de comptage)
- Le code moyen de paiement par défaut est "30" (virement) si IBAN présent
- pays_code TOUJOURS en 2 lettres ISO (FR, DE, BE, CH...). Défaut "FR"
"""


def call_gemini(ocr_text: str, email_context: str = "") -> dict:
    """Appelle l'API Gemini (avec backoff sur 429)."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY non configurée")

    user_message = f"Texte OCR de la facture :\n\n{(ocr_text or '')[:8000]}"
    if email_context:
        user_message += f"\n\nContexte email :\n{email_context[:2000]}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": GEMINI_SYSTEM_PROMPT + "\n\n" + user_message}]}
        ],
        "generationConfig": {
            "temperature": 0.1,
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
            ra = resp.headers.get("Retry-After")
            try:
                sleep_s = int(ra) if ra and ra.isdigit() else min(60, base_sleep * (2 ** (attempt - 1)))
            except Exception:
                sleep_s = min(60, base_sleep * (2 ** (attempt - 1)))
            sleep_s = sleep_s + (attempt * 0.2)
            logger.warning("Gemini 429 (rate limit) tentative %s/%s, pause %.1fs", attempt, max_attempts, sleep_s)
            time.sleep(sleep_s)
            continue

        if resp.status_code >= 400:
            snippet = (resp.text or "")[:200]
            logger.error("Erreur HTTP Gemini (%s): %s", resp.status_code, snippet)
            resp.raise_for_status()

        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]

        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)

        return json.loads(cleaned)

    raise requests.exceptions.HTTPError(f"Gemini rate limit (429) after retries; last_status={last_status}")


# ---------------------------------------------------------------------------
# 3) Normalisation des données Gemini pour EN16931
# ---------------------------------------------------------------------------
def normalize_invoice_data(inv: dict) -> dict:
    """
    Normalise et complète les données Gemini pour garantir un XML EN16931 valide.
    Ajoute les valeurs par défaut manquantes, recalcule les totaux si incohérents.
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

    # --- Lignes : garantir au moins 1 (BR-16) ---
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
            if net == 0.0 and pu > 0:
                net = round(pu * qty, 2)
            elif pu == 0.0 and net > 0 and qty > 0:
                pu = round(net / qty, 2)
            line["prix_unitaire_ht"] = pu
            line["quantite"] = qty
            line["montant_net_ht"] = net
    inv["lignes"] = lignes

    # --- Ventilation TVA ---
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

    # --- Totaux ---
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


# ---------------------------------------------------------------------------
# 4) Génération XML Factur-X profil EN16931
# ---------------------------------------------------------------------------
def generate_facturx_xml_en16931(inv: dict) -> bytes:
    """
    Génère le XML Factur-X profil EN16931 (CII D16B/D22B).

    Couvre les règles obligatoires :
    - BR-08/BR-09 : adresse postale vendeur + code pays
    - BR-10/BR-11 : adresse postale acheteur + code pays
    - BR-16 : au moins 1 ligne de facture
    - BG-22 : totaux monétaires complets
    - BG-23 : ventilation TVA
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

    # ===== ExchangedDocumentContext =====
    ctx = etree.SubElement(root, _el("rsm", "ExchangedDocumentContext"))
    guide_ctx = etree.SubElement(ctx, _el("ram", "GuidelineSpecifiedDocumentContextParameter"))
    guide_id = etree.SubElement(guide_ctx, _el("ram", "ID"))
    guide_id.text = "urn:cen.eu:en16931:2017"

    # ===== ExchangedDocument =====
    doc = etree.SubElement(root, _el("rsm", "ExchangedDocument"))
    doc_id = etree.SubElement(doc, _el("ram", "ID"))
    doc_id.text = inv.get("numero_facture") or "SANS-NUMERO"

    type_code = etree.SubElement(doc, _el("ram", "TypeCode"))
    type_code.text = str(inv.get("type_facture", "380"))

    issue_dt = etree.SubElement(doc, _el("ram", "IssueDateTime"))
    issue_dts = etree.SubElement(issue_dt, _el("udt", "DateTimeString"), format="102")
    date_str = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    issue_dts.text = date_str.replace("-", "")

    notes_text = inv.get("notes")
    if notes_text:
        note_el = etree.SubElement(doc, _el("ram", "IncludedNote"))
        note_content = etree.SubElement(note_el, _el("ram", "Content"))
        note_content.text = str(notes_text)[:500]

    # ===== SupplyChainTradeTransaction =====
    txn = etree.SubElement(root, _el("rsm", "SupplyChainTradeTransaction"))

    # ----- Lignes de facture (BR-16) -----
    for line in inv.get("lignes", []):
        line_item = etree.SubElement(txn, _el("ram", "IncludedSupplyChainTradeLineItem"))

        assoc_doc = etree.SubElement(line_item, _el("ram", "AssociatedDocumentLineDocument"))
        line_id = etree.SubElement(assoc_doc, _el("ram", "LineID"))
        line_id.text = str(line.get("numero", "1"))

        product = etree.SubElement(line_item, _el("ram", "SpecifiedTradeProduct"))
        product_name = etree.SubElement(product, _el("ram", "Name"))
        product_name.text = str(line.get("description", "Article"))[:256]

        agree = etree.SubElement(line_item, _el("ram", "SpecifiedLineTradeAgreement"))
        net_price = etree.SubElement(agree, _el("ram", "NetPriceProductTradePrice"))
        charge_amount = etree.SubElement(net_price, _el("ram", "ChargeAmount"))
        charge_amount.text = f"{_safe_float(line.get('prix_unitaire_ht')):.2f}"

        delivery = etree.SubElement(line_item, _el("ram", "SpecifiedLineTradeDelivery"))
        billed_qty = etree.SubElement(
            delivery, _el("ram", "BilledQuantity"),
            unitCode=str(line.get("unite", "C62"))
        )
        billed_qty.text = f"{_safe_float(line.get('quantite'), 1.0):.4f}"

        settle_line = etree.SubElement(line_item, _el("ram", "SpecifiedLineTradeSettlement"))

        line_tax = etree.SubElement(settle_line, _el("ram", "ApplicableTradeTax"))
        line_tax_type = etree.SubElement(line_tax, _el("ram", "TypeCode"))
        line_tax_type.text = "VAT"
        line_tax_cat = etree.SubElement(line_tax, _el("ram", "CategoryCode"))
        line_tax_cat.text = str(line.get("code_tva", "S"))
        line_tax_rate = etree.SubElement(line_tax, _el("ram", "RateApplicablePercent"))
        line_tax_rate.text = f"{_safe_float(line.get('taux_tva', 20.0)):.2f}"

        line_summ = etree.SubElement(settle_line, _el("ram", "SpecifiedTradeSettlementLineMonetarySummation"))
        line_total = etree.SubElement(line_summ, _el("ram", "LineTotalAmount"))
        line_total.text = f"{_safe_float(line.get('montant_net_ht')):.2f}"

    # ===== ApplicableHeaderTradeAgreement =====
    agree_h = etree.SubElement(txn, _el("ram", "ApplicableHeaderTradeAgreement"))

    buyer_ref = inv.get("reference_commande")
    if buyer_ref:
        br_el = etree.SubElement(agree_h, _el("ram", "BuyerReference"))
        br_el.text = str(buyer_ref)

    # SellerTradeParty (BG-4)
    vendeur = inv.get("vendeur", {}) or {}
    seller = etree.SubElement(agree_h, _el("ram", "SellerTradeParty"))

    seller_name = etree.SubElement(seller, _el("ram", "Name"))
    seller_name.text = vendeur.get("nom") or "Fournisseur inconnu"

    if vendeur.get("siret"):
        spec_legal = etree.SubElement(seller, _el("ram", "SpecifiedLegalOrganization"))
        legal_id = etree.SubElement(spec_legal, _el("ram", "ID"), schemeID="0002")
        legal_id.text = vendeur["siret"]

    # PostalTradeAddress vendeur (BR-08, BR-09)
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

    # BuyerTradeParty (BG-7)
    acheteur = inv.get("acheteur", {}) or {}
    buyer = etree.SubElement(agree_h, _el("ram", "BuyerTradeParty"))

    buyer_name = etree.SubElement(buyer, _el("ram", "Name"))
    buyer_name.text = acheteur.get("nom") or "Acheteur"

    if acheteur.get("siret"):
        spec_legal_b = etree.SubElement(buyer, _el("ram", "SpecifiedLegalOrganization"))
        etree.SubElement(spec_legal_b, _el("ram", "ID"), schemeID="0002").text = acheteur["siret"]

    # PostalTradeAddress acheteur (BR-10, BR-11)
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

    # ===== ApplicableHeaderTradeDelivery (BG-13) =====
    etree.SubElement(txn, _el("ram", "ApplicableHeaderTradeDelivery"))

    # ===== ApplicableHeaderTradeSettlement =====
    settle_h = etree.SubElement(txn, _el("ram", "ApplicableHeaderTradeSettlement"))

    devise = inv.get("devise") or "EUR"
    etree.SubElement(settle_h, _el("ram", "InvoiceCurrencyCode")).text = devise

    # Payment means (BG-16)
    payment_code = inv.get("code_moyen_paiement")
    if payment_code:
        payment_means = etree.SubElement(settle_h, _el("ram", "SpecifiedTradeSettlementPaymentMeans"))
        etree.SubElement(payment_means, _el("ram", "TypeCode")).text = str(payment_code)

        iban = inv.get("iban")
        if iban:
            payee_acct = etree.SubElement(payment_means, _el("ram", "PayeePartyCreditorFinancialAccount"))
            etree.SubElement(payee_acct, _el("ram", "IBANID")).text = iban.replace(" ", "")
            bic = inv.get("bic")
            if bic:
                payee_inst = etree.SubElement(payment_means, _el("ram", "PayeeSpecifiedCreditorFinancialInstitution"))
                etree.SubElement(payee_inst, _el("ram", "BICID")).text = bic.replace(" ", "")

    # Ventilation TVA (BG-23) — DOIT être avant SpecifiedTradePaymentTerms (ordre XSD)
    for vat_break in inv.get("ventilation_tva", []):
        tax_el = etree.SubElement(settle_h, _el("ram", "ApplicableTradeTax"))
        etree.SubElement(tax_el, _el("ram", "CalculatedAmount")).text = f"{_safe_float(vat_break.get('montant_tva')):.2f}"
        etree.SubElement(tax_el, _el("ram", "TypeCode")).text = "VAT"
        etree.SubElement(tax_el, _el("ram", "BasisAmount")).text = f"{_safe_float(vat_break.get('base_ht')):.2f}"
        etree.SubElement(tax_el, _el("ram", "CategoryCode")).text = str(vat_break.get("code_tva", "S"))
        etree.SubElement(tax_el, _el("ram", "RateApplicablePercent")).text = f"{_safe_float(vat_break.get('taux')):.2f}"

    # Payment terms / échéance (BT-9) — APRÈS ApplicableTradeTax
    date_ech = inv.get("date_echeance")
    if date_ech:
        payment_terms = etree.SubElement(settle_h, _el("ram", "SpecifiedTradePaymentTerms"))
        due_dt = etree.SubElement(payment_terms, _el("ram", "DueDateDateTime"))
        due_dts = etree.SubElement(due_dt, _el("udt", "DateTimeString"), format="102")
        due_dts.text = date_ech.replace("-", "")

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


# ---------------------------------------------------------------------------
# 5) Conversion PDF/A-3 + Assemblage XML -> Factur-X
# ---------------------------------------------------------------------------
def _inject_pdfa3_markers(pdf_bytes: bytes) -> bytes:
    """
    Injecte les marqueurs PDF/A-3b dans un PDF déjà Factur-X :
      - OutputIntent sRGB (si absent)
      - Patch XMP existant pour ajouter pdfaid:part=3 / pdfaid:conformance=B
    Approche : modifier le XMP existant plutôt que le remplacer,
    car la lib factur-x génère son propre XMP qu'on doit enrichir.
    """
    if b"pdfaid" in pdf_bytes and b"OutputIntent" in pdf_bytes:
        return pdf_bytes

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject, DictionaryObject, NameObject,
        TextStringObject, DecodedStreamObject, IndirectObject,
    )

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    writer.clone_reader_document_root(reader)

    # 1. OutputIntent sRGB (si absent)
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

    # 2. Patch XMP : ajouter pdfaid dans le XMP existant
    #    On récupère le XMP actuel, on injecte les balises pdfaid avant </rdf:Description>
    existing_xmp = b""
    meta_ref = writer._root_object.get("/Metadata")
    if meta_ref is not None:
        try:
            if isinstance(meta_ref, IndirectObject):
                meta_obj = meta_ref.get_object()
            else:
                meta_obj = meta_ref
            existing_xmp = meta_obj.get_data()
        except Exception:
            pass

    pdfaid_block = (
        b' xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"'
    )
    pdfaid_tags = (
        b'<pdfaid:part>3</pdfaid:part>'
        b'<pdfaid:conformance>B</pdfaid:conformance>'
    )

    if existing_xmp and b"pdfaid" not in existing_xmp:
        # Stratégie 1 : injecter avant </rdf:Description>
        marker = b"</rdf:Description>"
        if marker in existing_xmp:
            # Ajouter le namespace pdfaid à l'ouverture de rdf:Description
            patched = existing_xmp.replace(
                b'<rdf:Description rdf:about=""',
                b'<rdf:Description rdf:about=""' + pdfaid_block,
                1,
            )
            patched = patched.replace(marker, pdfaid_tags + marker, 1)
            existing_xmp = patched
        else:
            # Stratégie 2 : injecter avant </rdf:RDF>
            marker2 = b"</rdf:RDF>"
            if marker2 in existing_xmp:
                inject = (
                    b'<rdf:Description rdf:about=""'
                    + pdfaid_block + b'>'
                    + pdfaid_tags
                    + b'</rdf:Description>'
                )
                existing_xmp = existing_xmp.replace(marker2, inject + marker2, 1)

        # Réécrire le stream XMP
        xmp_stream = DecodedStreamObject()
        xmp_stream.set_data(existing_xmp)
        xmp_stream[NameObject("/Type")] = NameObject("/Metadata")
        xmp_stream[NameObject("/Subtype")] = NameObject("/XML")
        writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp_stream)
    elif not existing_xmp:
        # Pas de XMP du tout → en créer un complet
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
    """Génère le Factur-X puis injecte les marqueurs PDF/A-3b."""
    # Étape 1 : embedding XML (la lib factur-x gère EmbeddedFiles + AF + XMP partiel)
    facturx_bytes = generate_from_binary(
        pdf_bytes,
        xml_bytes,
        flavor="factur-x",
        level=FACTURX_PROFILE,
        check_xsd=True,
        xmp_compression=False,
    )
    # Étape 2 : injection PDF/A-3b (OutputIntent + pdfaid dans XMP)
    return _inject_pdfa3_markers(facturx_bytes)


# ---------------------------------------------------------------------------
# 6) Routes
# ---------------------------------------------------------------------------
@app.route("/api/process-invoice", methods=["POST"])
def process_invoice():
    """OCR -> filtrage local -> Gemini -> normalisation -> Factur-X EN16931 -> PDF/A-3."""
    if "pdf" not in request.files:
        return jsonify({"error": "Fichier PDF requis (clé 'pdf')"}), 400

    pdf_file = request.files["pdf"]
    pdf_bytes = pdf_file.read()

    email_context = ""
    if request.form.get("email_subject"):
        email_context += f"Objet: {request.form['email_subject']}\n"
    if request.form.get("email_from"):
        email_context += f"Expéditeur: {request.form['email_from']}\n"
    if request.form.get("email_body"):
        email_context += f"Corps: {request.form['email_body'][:1000]}\n"

    try:
        logger.info("Extraction texte du PDF...")
        ocr_text = extract_text_from_pdf(pdf_bytes)

        if len((ocr_text or "").strip()) < 20:
            resp = jsonify({"error": "Impossible d'extraire du texte du PDF"})
            resp.headers["X-Gemini-Used"] = "0"
            return resp, 422

        ok, why = is_invoice_candidate(ocr_text)
        if not ok:
            logger.info("Document rejeté (filtrage local) : %s", why)
            resp = jsonify({"error": "Document non-facture (filtrage local)", "reason": why})
            resp.headers["X-Gemini-Used"] = "0"
            return resp, 422

        logger.info("Appel Gemini pour extraction des données (profil EN16931)...")
        invoice_data = call_gemini(ocr_text, email_context)

        if not invoice_data.get("est_facture"):
            resp = jsonify({
                "error": "Le document n'est pas identifié comme une facture",
                "invoice_data": invoice_data,
            })
            resp.headers["X-Gemini-Used"] = "1"
            return resp, 422

        logger.info("Normalisation des données pour profil EN16931...")
        invoice_data = normalize_invoice_data(invoice_data)

        logger.info("Génération du XML Factur-X (profil EN16931)...")
        xml_bytes = generate_facturx_xml_en16931(invoice_data)

        logger.info("Injection Factur-X dans le PDF...")
        facturx_pdf_bytes = embed_facturx_in_pdf(pdf_bytes, xml_bytes)

        invoice_date = invoice_data.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
        try:
            dt = datetime.fromisoformat(invoice_date.replace("Z", ""))
        except Exception:
            dt = datetime.now()

        folder_name = f"{dt.year}-{dt.month:02d} {MOIS_FR.get(dt.month, '')}".strip()
        vendor = (invoice_data.get("vendeur", {}) or {}).get("nom_court") or "Fournisseur"
        number = invoice_data.get("numero_facture") or "SansNumero"
        filename = sanitize_filename(f"{dt.year}-{dt.month:02d}-{vendor}-{number}.pdf")

        nb_lignes = len(invoice_data.get("lignes", []))
        logger.info(
            "Factur-X EN16931 : %s | %d ligne(s) | %.2f€ TTC → %s",
            filename, nb_lignes, _safe_float(invoice_data.get("montant_ttc")), folder_name,
        )

        response = send_file(
            io.BytesIO(facturx_pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )

        response.headers["X-Gemini-Used"] = "1"
        response.headers["X-Invoice-Filename"] = filename
        response.headers["X-Invoice-Folder"] = folder_name
        response.headers["X-Invoice-Number"] = number
        response.headers["X-Invoice-Date"] = invoice_data.get("date_facture") or ""
        response.headers["X-Invoice-Vendor"] = vendor
        response.headers["X-Invoice-Amount-TTC"] = str(invoice_data.get("montant_ttc", 0))
        response.headers["X-Invoice-Lines"] = str(nb_lignes)
        response.headers["X-Invoice-Profile"] = "EN16931"
        # Base64 pour éviter le rejet Gunicorn (headers longs / caractères spéciaux)
        import base64 as _b64
        response.headers["X-Invoice-Data"] = _b64.b64encode(
            json.dumps(invoice_data, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

        return response, 200

    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)

        if status == 429:
            retry_after = resp.headers.get("Retry-After", "60") if resp else "60"
            logger.error("Gemini rate limit (429). Retry-After=%s", retry_after)
            return jsonify({"error": "Gemini rate limit", "retry_after": retry_after}), 429

        logger.error("Erreur HTTP Gemini (%s)", status)
        return jsonify({"error": "Erreur API Gemini"}), 502

    except requests.exceptions.RequestException as e:
        logger.error("Erreur réseau Gemini : %s", str(e))
        return jsonify({"error": "Erreur réseau Gemini"}), 502

    except Exception as e:
        logger.exception("Erreur inattendue")
        return jsonify({"error": str(e)}), 500


@app.route("/api/extract-metadata", methods=["POST"])
def extract_metadata():
    """OCR + Gemini -> retourne uniquement le JSON (pas de Factur-X)."""
    if "pdf" not in request.files:
        return jsonify({"error": "Fichier PDF requis"}), 400

    pdf_file = request.files["pdf"]
    pdf_bytes = pdf_file.read()
    email_context = request.form.get("email_context", "")

    try:
        ocr_text = extract_text_from_pdf(pdf_bytes)

        ok, why = is_invoice_candidate(ocr_text)
        if not ok:
            return jsonify({"error": "Document non-facture (filtrage local)", "reason": why}), 422

        invoice_data = call_gemini(ocr_text, email_context)
        invoice_data = normalize_invoice_data(invoice_data)

        invoice_data["_metadata"] = {
            "filename": build_filename(invoice_data),
            "folder": build_folder_name(invoice_data),
            "profile": "EN16931",
            "nb_lignes": len(invoice_data.get("lignes", [])),
            "ocr_chars": len(ocr_text),
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }

        return jsonify(invoice_data), 200

    except Exception as e:
        logger.exception("Erreur extraction métadonnées")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "gemini_configured": bool(GEMINI_API_KEY),
        "gemini_model": GEMINI_MODEL,
        "facturx_profile": FACTURX_PROFILE,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
