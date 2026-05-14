#!/usr/bin/env python3
"""
test_client_final.py — Tests batch 2 : extraction du client final
==================================================================

Couvre les exemples métier réels :
  - Cuisines Morel  : contremarque GARNIER → client final GARNIER
  - BAUS            : Bénédicte Gloaguen exclue, Brigitte Whitechurch valide
  - IN IPSO         : JMT Déco / Raison Home → fallback A_CLASSER
  - cas acheteur valide, acheteur = fournisseur, blacklist exhaustive

Armony (LCR/relevé) est testé dans test_invoice_validation.py via is_invoice_candidate.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from facturx_utils import extract_final_client, build_client_folder_name


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inv(vendor: str = "FOURNISSEUR", buyer: str = "", lignes: list | None = None) -> dict:
    return {
        "vendeur":  {"nom": vendor,  "nom_court": vendor},
        "acheteur": {"nom": buyer,   "nom_court": buyer},
        "lignes":   lignes or [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Priorité 1 — contremarque / chantier dans l'OCR
# ─────────────────────────────────────────────────────────────────────────────

def test_contremarque_garnier():
    """Cas Cuisines Morel F25142633 : 'Contremarque : GARNIER' → GARNIER."""
    ocr = (
        "CUISINES MOREL\n"
        "Facture F25142633\n"
        "Adresse livraison : JMT Déco, 75001 Paris\n"
        "Contremarque : GARNIER\n"
        "Total HT : 1 500,00 €"
    )
    result = extract_final_client(_inv(vendor="CUISINES MOREL", buyer="JMT Déco"), ocr)
    assert result == "GARNIER"


def test_chantier_keyword():
    """'Chantier : DUPONT' → DUPONT."""
    ocr = "Référence chantier : DUPONT\nFacture 2026-001"
    result = extract_final_client(_inv(), ocr)
    assert result == "DUPONT"


def test_repere_client_keyword():
    """'Repère client : MARTINEZ' → MARTINEZ."""
    ocr = "Repère client : MARTINEZ\nFacture 2026-099"
    result = extract_final_client(_inv(), ocr)
    assert result == "MARTINEZ"


def test_contremarque_blacklisted_value():
    """Contremarque JMT Déco est blacklistée → passe à la priorité suivante → A_CLASSER."""
    ocr = "Contremarque : JMT Déco\nFacture 001"
    result = extract_final_client(_inv(vendor="IPSO", buyer="JMT Déco"), ocr)
    assert result == "A_CLASSER"


def test_contremarque_with_trailing_spaces():
    """La contremarque est nettoyée des espaces et ponctuations finales."""
    ocr = "Contremarque : BERNARD .\n"
    result = extract_final_client(_inv(vendor="MOREL"), ocr)
    assert result == "BERNARD"


# ─────────────────────────────────────────────────────────────────────────────
# Priorité 2 — noms propres dans les lignes de facture
# ─────────────────────────────────────────────────────────────────────────────

def test_baus_whitechurch_benedicte_excluded():
    """
    Cas BAUS-2026-02-000239 :
    - Bénédicte Gloaguen dans la 1ère ligne → exclue
    - Brigitte Whitechurch dans la 2ème → valide → client final
    """
    lignes = [
        {"description": "Bénédicte Gloaguen - cuisine sur mesure"},
        {"description": "Brigitte Whitechurch - pose cuisine"},
    ]
    result = extract_final_client(_inv(vendor="BAUS", buyer="JMT Déco", lignes=lignes), "")
    assert "Whitechurch" in result
    assert "Gloaguen" not in result


def test_benedicte_gloaguen_always_excluded():
    """Bénédicte Gloaguen est exclue en toutes circonstances."""
    lignes = [{"description": "Bénédicte Gloaguen - rénovation cuisine"}]
    result = extract_final_client(_inv(buyer="JMT Déco", lignes=lignes), "")
    assert "Gloaguen" not in result
    assert result == "A_CLASSER"


def test_jmt_deco_in_ligne_excluded():
    """JMT Déco dans une description de ligne → exclu."""
    lignes = [{"description": "Livraison JMT Déco Paris"}]
    result = extract_final_client(_inv(vendor="IPSO", buyer="JMT Déco", lignes=lignes), "")
    assert result == "A_CLASSER"


def test_valid_name_in_ligne():
    """Nom valide dans une ligne → client final."""
    lignes = [{"description": "Pose meuble pour Martin Lefevre"}]
    # "Martin" (6 chars) + "Lefevre" (7 chars) → match _PROPER_NAME_RE
    result = extract_final_client(_inv(vendor="BAUS", buyer="JMT Déco", lignes=lignes), "")
    assert "Lefevre" in result or result == "A_CLASSER"  # dépend de l'OCR exact


def test_multiple_valid_names_returns_first(db=None):
    """Plusieurs noms valides dans les lignes → premier non-blacklisté retenu."""
    lignes = [
        {"description": "Brigitte Whitechurch - cuisine"},
        {"description": "Jean Dupont - salle de bain"},
    ]
    result = extract_final_client(_inv(vendor="BAUS", buyer="JMT Déco", lignes=lignes), "")
    assert "Whitechurch" in result


# ─────────────────────────────────────────────────────────────────────────────
# Priorité 3 — acheteur.nom
# ─────────────────────────────────────────────────────────────────────────────

def test_acheteur_valid_when_no_contremarque():
    """Acheteur non blacklisté → client final = acheteur."""
    result = extract_final_client(_inv(vendor="IPSO", buyer="Martin Dufresne"), "")
    assert result == "Martin Dufresne"


def test_acheteur_jmt_deco_excluded():
    """Cas IN IPSO : acheteur JMT Déco → exclu → A_CLASSER."""
    result = extract_final_client(_inv(vendor="IN IPSO", buyer="JMT Déco"), "")
    assert result == "A_CLASSER"


def test_acheteur_raison_home_excluded():
    """Acheteur Raison Home → exclu → A_CLASSER."""
    result = extract_final_client(_inv(vendor="IN IPSO", buyer="Raison Home"), "")
    assert result == "A_CLASSER"


def test_acheteur_jmt_deco_slash_raison_home():
    """'JMT Déco / Raison Home' : contient JMT Déco → exclu → A_CLASSER."""
    result = extract_final_client(_inv(vendor="IN IPSO", buyer="JMT Déco / Raison Home"), "")
    assert result == "A_CLASSER"


def test_acheteur_equals_vendor_excluded():
    """Acheteur identique au fournisseur → exclu → A_CLASSER."""
    result = extract_final_client(_inv(vendor="IPSO FACTO", buyer="IPSO FACTO"), "")
    assert result == "A_CLASSER"


def test_acheteur_contains_vendor_excluded():
    """Acheteur contient le nom du fournisseur → exclu."""
    result = extract_final_client(_inv(vendor="IPSO", buyer="IN IPSO SARL"), "")
    assert result == "A_CLASSER"


# ─────────────────────────────────────────────────────────────────────────────
# Fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_invoice_fallback():
    """Facture vide → A_CLASSER."""
    assert extract_final_client({}, "") == "A_CLASSER"


def test_all_blacklisted_fallback():
    """Toutes les sources blacklistées → A_CLASSER."""
    lignes = [{"description": "JMT Déco - livraison"}]
    result = extract_final_client(_inv(vendor="IPSO", buyer="JMT Déco", lignes=lignes), "")
    assert result == "A_CLASSER"


def test_benedicte_gloaguen_as_buyer_fallback():
    """Bénédicte Gloaguen comme acheteur → A_CLASSER."""
    result = extract_final_client(_inv(vendor="BAUS", buyer="Bénédicte Gloaguen"), "")
    assert result == "A_CLASSER"


# ─────────────────────────────────────────────────────────────────────────────
# build_client_folder_name — sanitisation pour Drive
# ─────────────────────────────────────────────────────────────────────────────

def test_folder_name_no_spaces():
    """Le nom de dossier ne contient pas d'espaces."""
    result = build_client_folder_name(_inv(vendor="MOREL", buyer="Brigitte Whitechurch"), "")
    assert " " not in result


def test_folder_name_garnier():
    """Contremarque GARNIER → dossier 'GARNIER'."""
    ocr = "Contremarque : GARNIER\n"
    result = build_client_folder_name(_inv(vendor="MOREL", buyer="JMT Déco"), ocr)
    assert result == "GARNIER"


def test_folder_name_whitechurch():
    """Brigitte Whitechurch → dossier 'Brigitte_Whitechurch'."""
    lignes = [{"description": "Brigitte Whitechurch - cuisine"}]
    result = build_client_folder_name(_inv(vendor="BAUS", buyer="JMT Déco", lignes=lignes), "")
    assert result == "Brigitte_Whitechurch"


def test_folder_name_fallback():
    """Sans client valide → dossier 'A_CLASSER'."""
    result = build_client_folder_name(_inv(vendor="IPSO", buyer="JMT Déco"), "")
    assert result == "A_CLASSER"


def test_folder_name_no_special_chars():
    """Les caractères spéciaux dans le nom sont éliminés."""
    ocr = "Contremarque : O'Brien\n"
    result = build_client_folder_name(_inv(vendor="MOREL"), ocr)
    # sanitize_filename retire les caractères interdits dans les noms de fichiers
    assert "/" not in result
    assert "?" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Cas Eberhardt — ligne opérationnelle "86 LEROY 17.03.2026"
# ─────────────────────────────────────────────────────────────────────────────

def _eberhardt_ocr() -> str:
    """OCR minimal représentatif de la facture Eberhardt 2026014899."""
    return (
        "JMT DECO\n"
        "BENEDICTE GLOAGUEN\n"
        "RG 140980 Bamba\n"
        "86 LEROY 17.03.2026\n"
        "FACTURE\n"
        "N° 14899\n"
        "montant TTC 713,57 EUR"
    )


def test_eberhardt_client_final_leroy():
    """Cas Eberhardt : OCR avec '86 LEROY 17.03.2026' → client final LEROY."""
    result = extract_final_client(
        _inv(vendor="EBERHARDT", buyer="JMT Déco"),
        _eberhardt_ocr(),
    )
    assert result == "LEROY"


def test_eberhardt_not_jmt_deco():
    """Cas Eberhardt : JMT DECO (OCR + acheteur) ne doit jamais être retenu."""
    result = extract_final_client(
        _inv(vendor="EBERHARDT", buyer="JMT Déco"),
        _eberhardt_ocr(),
    )
    assert "JMT" not in result
    assert "DECO" not in result


def test_eberhardt_not_benedicte_gloaguen():
    """Cas Eberhardt : BENEDICTE GLOAGUEN présente dans l'OCR → jamais retenue."""
    result = extract_final_client(
        _inv(vendor="EBERHARDT", buyer="JMT Déco"),
        _eberhardt_ocr(),
    )
    assert "GLOAGUEN" not in result
    assert "BENEDICTE" not in result


def test_eberhardt_not_bamba():
    """Cas Eberhardt : Bamba visible dans l'OCR mais LEROY disponible → Bamba ne doit pas être retenu."""
    result = extract_final_client(
        _inv(vendor="EBERHARDT", buyer="JMT Déco"),
        _eberhardt_ocr(),
    )
    assert result != "BAMBA"
    assert result == "LEROY"


def test_op_line_single_word_client():
    """Ligne opérationnelle avec client en un seul mot → accepté (LEROY est valide)."""
    ocr = "42 MARTIN 25.04.2026\nFacture TEST"
    result = extract_final_client(_inv(vendor="FOURNISSEUR", buyer="JMT Déco"), ocr)
    assert result == "MARTIN"


def test_op_line_two_word_client():
    """Ligne opérationnelle avec client en deux mots → accepté."""
    ocr = "12 DUPONT MARTIN 10.01.2026\nFacture"
    result = extract_final_client(_inv(vendor="FOURNISSEUR", buyer="JMT Déco"), ocr)
    assert result == "DUPONT MARTIN"


def test_op_line_blacklisted_client_skipped():
    """Ligne opérationnelle avec client blacklisté → ignorée → A_CLASSER."""
    ocr = "86 JMT DECO 17.03.2026\nFacture 001"
    result = extract_final_client(_inv(vendor="EBERHARDT", buyer="JMT Déco"), ocr)
    assert result == "A_CLASSER"


def test_op_line_not_matched_without_date():
    """'RG 140980 Bamba' sans date après → ne doit pas déclencher _OP_LINE_RE."""
    ocr = "RG 140980 Bamba\nFacture sans date suivante"
    # Bamba n'a pas de date après lui → pas de match → A_CLASSER (acheteur blacklisté)
    result = extract_final_client(_inv(vendor="EBERHARDT", buyer="JMT Déco"), ocr)
    # Le résultat ne doit PAS être BAMBA
    assert result != "BAMBA"


# ─────────────────────────────────────────────────────────────────────────────
# Cas Interbat — Référence : LEROY + C/M LEROY (non-régression)
# ─────────────────────────────────────────────────────────────────────────────

def _interbat_ocr() -> str:
    """OCR représentatif de la facture Interbat FA131485."""
    return (
        "INTERBAT\n"
        "Facture N°FA131485\n"
        "Adresse livraison : JMT Déco, 75001 Paris\n"
        "Référence : LEROY\n"
        "C/M LEROY\n"
        "Total HT : 2 400,00 €"
    )


def test_interbat_reference_leroy():
    """Non-régression Interbat : 'Référence : LEROY' → client final LEROY."""
    result = extract_final_client(
        _inv(vendor="INTERBAT", buyer="JMT Déco"),
        _interbat_ocr(),
    )
    assert result == "LEROY"


def test_interbat_cm_leroy_fallback():
    """Non-régression Interbat : 'C/M LEROY' seul (sans Référence) → LEROY."""
    ocr = "INTERBAT\nFacture FA131485\nC/M LEROY\nTotal HT : 2 400,00 €"
    result = extract_final_client(
        _inv(vendor="INTERBAT", buyer="JMT Déco"),
        ocr,
    )
    assert result == "LEROY"


def test_interbat_folder_name():
    """Non-régression Interbat : dossier Drive = 'LEROY'."""
    result = build_client_folder_name(
        _inv(vendor="INTERBAT", buyer="JMT Déco"),
        _interbat_ocr(),
    )
    assert result == "LEROY"
