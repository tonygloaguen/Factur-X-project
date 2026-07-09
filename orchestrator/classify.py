#!/usr/bin/env python3
"""classify.py — Calcul du triplet de rangement GDrive (mois / contremarque / fournisseur).

Arborescence cible :
    <AAAA-MM Mois>/<CONTREMARQUE>/<FOURNISSEUR>/<fichier>.pdf
Exception franchiseur (émetteur = Raison Home, par ID fort) :
    <AAAA-MM Mois>/Communication/<fichier>.pdf
Incertitude (contremarque introuvable) :
    <AAAA-MM Mois>/_A_CLASSER/<fichier>.pdf

L'identité du fournisseur (émetteur) provient du registre (``supplier_registry``)
— jamais d'une heuristique sur le nom d'en-tête (piège Häcker). Le classement ne
lève jamais : toute incertitude retombe sur ``_A_CLASSER`` avec un log.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Optional

from supplier_registry import (
    Identifiers, SupplierRegistry, extract_identifiers,
    identifiers_from_invoice, slugify,
)

logger = logging.getLogger("orchestrator")

# Noms de mois FR — même format que build_folder_name (« 2026-07 Juillet »).
_MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril", 5: "Mai", 6: "Juin",
    7: "Juillet", 8: "Août", 9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}

FALLBACK_FOLDER = "_A_CLASSER"
COMMUNICATION_FOLDER = "Communication"

# Libellés de contremarque, par ordre de priorité (premier non vide retenu).
_CONTREMARQUE_LABELS = [
    "contremarque", "c/m", "cm", "référence client", "reference client",
    "réf. commande client", "ref commande client", "réf commande client",
    "v/ref", "v/réf", "vref", "référence", "reference",
]

# Jamais une contremarque valide (enseigne/entité self).
_CONTREMARQUE_BLACKLIST = {"RAISON_HOME", "JMT_DECO", "JMT", "RAISON", "HOME"}

_CHANTIER_HINTS = ("chantier", "travaux", "adresse de livraison", "lieu d'intervention",
                   "lieu de livraison", "livraison")


def _strip_accents_lower(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


# ─────────────────────────────────────────────────────────────────────────────
# Résultat
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Classification:
    """Plan de rangement calculé pour une facture."""

    month: str                              # « 2026-07 Juillet »
    fournisseur: str                        # nom canonique registre (dossier niv. 3)
    contremarque: str                       # dossier niv. 2 (ou _A_CLASSER / Communication)
    filename: str                           # nom de fichier final
    route: str                              # 'normal' | 'communication' | 'a_classer'
    is_self_sale: bool = False
    resolution_method: str = ""
    alternates: list[str] = field(default_factory=list)  # contremarques secondaires loguées
    warnings: list[str] = field(default_factory=list)

    @property
    def path_parts(self) -> list[str]:
        """Segments de dossier (hors nom de fichier), racine Drive non incluse."""
        if self.route == "communication":
            return [self.month, COMMUNICATION_FOLDER]
        if self.route == "a_classer":
            return [self.month, FALLBACK_FOLDER]
        return [self.month, self.contremarque, self.fournisseur]

    @property
    def folder_path(self) -> str:
        return "/".join(self.path_parts)


# ─────────────────────────────────────────────────────────────────────────────
# Mois
# ─────────────────────────────────────────────────────────────────────────────

def month_folder(inv: dict) -> str:
    """Reproduit le format mensuel existant : « AAAA-MM Mois »."""
    date_str = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        dt = datetime.now()
    return f"{dt.year}-{dt.month:02d} {_MOIS_FR.get(dt.month, '')}".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Avoir / nom de fichier
# ─────────────────────────────────────────────────────────────────────────────

def is_credit_note(inv: dict, ocr_text: str = "") -> bool:
    """Détecte un avoir : type 381, mention « AVOIR », ou montant total négatif."""
    if str(inv.get("type_facture") or "") == "381":
        return True
    if "avoir" in _strip_accents_lower(ocr_text[:400]):
        return True
    try:
        if float(inv.get("montant_ttc") or 0) < 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def build_classified_filename(inv: dict, fournisseur: str, *, credit: bool) -> str:
    """Nom de fichier : ``{FOURNISSEUR}_FacturX_{date}_{num}.pdf`` (préfixe AVOIR_ si avoir)."""
    date = inv.get("date_facture") or datetime.now().strftime("%Y-%m-%d")
    numero = str(inv.get("numero_facture") or "").strip()
    suffix = f"_{numero}" if numero else ""
    base = f"{fournisseur}_FacturX_{date}{suffix}.pdf"
    name = ("AVOIR_" + base) if credit else base
    # Nettoyage léger des caractères de chemin (le slug fournisseur est déjà sûr).
    return re.sub(r"[\\/:*?\"<>|]+", "_", name)


# ─────────────────────────────────────────────────────────────────────────────
# Contremarque
# ─────────────────────────────────────────────────────────────────────────────

# Mots qui terminent une valeur de contremarque (bruit de fin de ligne).
_CONTREMARQUE_STOP = {
    "client", "chez", "tva", "siret", "siren", "tel", "ref", "reference",
    "commande", "adresse", "livraison", "chantier", "facture", "n", "no",
}


def _family_name(raw: str) -> str:
    """Extrait le nom de famille d'un libellé (convention « Prénom NOM »).

    « Aurélie SCHWEITZER » → « SCHWEITZER » ; « FEUVRIER » → « FEUVRIER ».
    Les tokens en MAJUSCULES priment ; sinon on retient le dernier token.
    """
    tokens = [t for t in re.split(r"\s+", raw.strip()) if t]
    if not tokens:
        return ""
    caps = [t for t in tokens if t.isalpha() and t.isupper() and len(t) >= 2]
    if caps:
        return " ".join(caps)
    if len(tokens) >= 2:
        return tokens[-1]
    return tokens[0]


def _clean_contremarque(value: str) -> str:
    """Normalise une valeur de contremarque en slug de nom de famille, ou ''."""
    slug = slugify(_family_name(value))
    if not slug or slug in _CONTREMARQUE_BLACKLIST:
        return ""
    return slug


def _name_value_after_label(after: str) -> str:
    """Extrait la valeur de nom qui suit un libellé, en s'arrêtant au bruit.

    Collecte les tokens de type nom propre (initiale majuscule / tout en
    majuscules) et s'arrête au premier mot en minuscules, mot-outil ou token
    contenant un chiffre. Limite à 3 tokens.
    """
    after = re.sub(r"^[\s:.\-–]+", "", after).strip()
    collected: list[str] = []
    for tok in re.split(r"\s+", after):
        if not tok or len(collected) >= 3:
            break
        low = _strip_accents_lower(tok).strip(".,;")
        if low in _CONTREMARQUE_STOP or any(c.isdigit() for c in tok):
            break
        if tok[:1].isupper():
            collected.append(tok)
        else:
            break
    return " ".join(collected)


def _contremarque_from_labels(ocr_text: str) -> str:
    """Cherche un libellé de contremarque explicite dans le texte."""
    if not ocr_text:
        return ""
    lines = ocr_text.splitlines()
    lowered = [_strip_accents_lower(ln) for ln in lines]
    for label in _CONTREMARQUE_LABELS:
        lab = _strip_accents_lower(label)
        for i, ln in enumerate(lowered):
            idx = ln.find(lab)
            if idx == -1:
                continue
            # Valeur = nom propre qui suit le label (arrêt au bruit de fin de ligne).
            value = _name_value_after_label(lines[i][idx + len(label):])
            cm = _clean_contremarque(value)
            if cm:
                return cm
    return ""


def _contremarque_from_lines(inv: dict) -> tuple[str, list[str]]:
    """Contremarque dominante quand plusieurs chantiers apparaissent en lignes.

    Cas Menuiserie JH : lignes « SAV GRAIRE » / « SAV MARTINEAU » → route sur la
    contremarque au montant cumulé le plus élevé ; retourne aussi les secondaires.
    """
    totals: dict[str, float] = {}
    pat = re.compile(r"\b(?:SAV|CHANTIER|C/?M|REF)\s+([A-ZÉÈÀÂÊÎÔÛ][A-Za-zÉÈÀÂÊÎÔÛ\-]{2,})", re.IGNORECASE)
    for line in inv.get("lignes") or []:
        desc = str(line.get("description") or "")
        m = pat.search(desc)
        if not m:
            continue
        cm = _clean_contremarque(m.group(1))
        if not cm:
            continue
        try:
            amount = abs(float(line.get("montant_net_ht") or 0))
        except (TypeError, ValueError):
            amount = 0.0
        totals[cm] = totals.get(cm, 0.0) + amount
    if not totals:
        return "", []
    ranked = sorted(totals, key=lambda k: totals[k], reverse=True)
    return ranked[0], ranked[1:]


def _contremarque_from_chantier_address(ocr_text: str) -> str:
    """Repli : nom associé à l'adresse de chantier / livraison."""
    if not ocr_text:
        return ""
    lines = ocr_text.splitlines()
    lowered = [_strip_accents_lower(ln) for ln in lines]
    for i, ln in enumerate(lowered):
        if any(h in ln for h in _CHANTIER_HINTS):
            # Chercher un nom propre sur la même ligne ou la suivante.
            for cand in (lines[i], lines[i + 1] if i + 1 < len(lines) else ""):
                m = re.search(r"\b([A-ZÉÈÀÂÊÎÔÛ][A-Za-zÉÈÀÂÊÎÔÛ'\-]{2,}(?:\s+[A-ZÉÈÀÂÊÎÔÛ][A-Za-zÉÈÀÂÊÎÔÛ'\-]+)?)", cand)
                if m:
                    cm = _clean_contremarque(m.group(1))
                    if cm:
                        return cm
    return ""


def resolve_contremarque(
    inv: dict, ocr_text: str, *, is_self_sale: bool
) -> tuple[str, list[str]]:
    """Détermine la contremarque (client final du chantier) et ses alternatives.

    Pour une facture de vente (émetteur = self), la contremarque est le
    destinataire (client particulier). Sinon : libellés explicites, puis
    contremarque dominante en lignes, puis nom près de l'adresse de chantier.
    """
    if is_self_sale:
        acheteur = inv.get("acheteur") or {}
        cm = _clean_contremarque(acheteur.get("nom") or acheteur.get("nom_court") or "")
        return cm, []

    cm = _contremarque_from_labels(ocr_text)
    if cm:
        return cm, []

    dominant, alternates = _contremarque_from_lines(inv)
    if dominant:
        return dominant, alternates

    cm = _contremarque_from_chantier_address(ocr_text)
    return cm, []


# ─────────────────────────────────────────────────────────────────────────────
# Classification complète
# ─────────────────────────────────────────────────────────────────────────────

def classify(
    inv: dict,
    ocr_text: str,
    registry: SupplierRegistry,
    *,
    learner: Optional[Callable[[Identifiers, str], Optional[str]]] = None,
) -> Classification:
    """Calcule le plan de rangement complet d'une facture.

    Args:
        inv: Données facture normalisées (vendeur/acheteur/lignes/…).
        ocr_text: Texte OCR source (contremarque, identifiants complémentaires).
        registry: Registre fournisseurs (source de vérité de l'émetteur).
        learner: Callback optionnel appelé si l'émetteur est inconnu ; doit
            retourner un nom canonique (ou None). En production, interroge Gemini
            et enrichit le registre.

    Returns:
        Un :class:`Classification` (jamais d'exception ; incertitude → _A_CLASSER).
    """
    month = month_folder(inv)
    credit = is_credit_note(inv, ocr_text)

    # Identifiants = champs structurés + texte OCR (la règle d'or écarte self).
    ids = identifiers_from_invoice(inv)
    text_ids = extract_identifiers(ocr_text)
    ids.vats |= text_ids.vats
    ids.sirets |= text_ids.sirets
    ids.sirens |= text_ids.sirens
    ids.domains |= text_ids.domains
    ids.names += text_ids.names

    res = registry.resolve_emitter(ids)
    warnings: list[str] = []

    # Émetteur inconnu → apprentissage (si un learner est fourni).
    if res.canonical is None and learner is not None:
        learned = learner(ids, ocr_text)
        if learned:
            # Le registre a été enrichi : ré-résoudre pour récupérer l'entrée.
            res = registry.resolve_emitter(ids)
            if res.canonical is None:
                # Ré-résolution infructueuse (aucun ID fort commun) : retenir le
                # canonical appris tel quel.
                res = replace(res, canonical=learned, method="learned",
                              entry=registry.get(learned))

    # Exception franchiseur : émetteur = Raison Home (par ID fort) → Communication.
    entry = res.entry or (registry.get(res.canonical) if res.canonical else None)
    if entry and entry.get("role") == "franchisor":
        filename = build_classified_filename(inv, COMMUNICATION_FOLDER, credit=credit)
        return Classification(
            month=month, fournisseur=COMMUNICATION_FOLDER, contremarque=COMMUNICATION_FOLDER,
            filename=filename, route="communication", resolution_method=res.method,
        )

    # Émetteur non identifié (même après apprentissage) → _A_CLASSER : on ne
    # devine JAMAIS un fournisseur depuis l'en-tête (piège Häcker), garde-fou #5.
    if not res.canonical:
        vendeur = inv.get("vendeur") or {}
        header = slugify(vendeur.get("nom_court") or vendeur.get("nom") or "") or "FOURNISSEUR_INCONNU"
        warnings.append("émetteur non identifié")
        logger.warning("Classement : émetteur non identifié → %s (%s)", FALLBACK_FOLDER, header)
        return Classification(
            month=month, fournisseur=header, contremarque=FALLBACK_FOLDER,
            filename=build_classified_filename(inv, header, credit=credit),
            route="a_classer", resolution_method=res.method, warnings=warnings,
        )

    fournisseur = res.canonical
    filename = build_classified_filename(inv, fournisseur, credit=credit)

    # Contremarque (dossier niveau 2).
    contremarque, alternates = resolve_contremarque(inv, ocr_text, is_self_sale=res.is_self)
    if alternates:
        logger.info("Classement : contremarques secondaires ignorées : %s", ", ".join(alternates))

    if not contremarque:
        warnings.append("contremarque introuvable")
        logger.warning("Classement : contremarque introuvable → %s (%s)", FALLBACK_FOLDER, filename)
        return Classification(
            month=month, fournisseur=fournisseur, contremarque=FALLBACK_FOLDER,
            filename=filename, route="a_classer", is_self_sale=res.is_self,
            resolution_method=res.method, alternates=alternates, warnings=warnings,
        )

    return Classification(
        month=month, fournisseur=fournisseur, contremarque=contremarque,
        filename=filename, route="normal", is_self_sale=res.is_self,
        resolution_method=res.method, alternates=alternates, warnings=warnings,
    )
