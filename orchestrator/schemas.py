#!/usr/bin/env python3
"""
schemas.py — Validation Pydantic des sorties LLM (Gemini)
==========================================================

Objectif : détecter les hallucinations de type AVANT que les données
ne propagent dans la chaîne de traitement (XML, Drive, Sheets).

Exemples de problèmes capturés :
  - montant_ttc: "cent euros"  → coercé à 0.0 (pas de crash XML)
  - est_facture: "oui"         → coercé à True
  - lignes: null               → coercé à []

Pattern :
  call_gemini() → GeminiInvoiceOutput.model_validate() → normalize_invoice_data()

Les champs extra (vendeur, acheteur, ventilation_tva, etc.) sont conservés
tels quels grâce à model_config extra="allow".
"""

from __future__ import annotations

from datetime import datetime as _dt
from typing import Any, Optional

from pydantic import BaseModel, field_validator, model_validator


class LigneFacture(BaseModel):
    """Validation et coercition d'une ligne de facture."""

    model_config = {"extra": "allow"}

    numero: str = "1"
    description: str = "Article"
    quantite: float = 1.0
    unite: str = "C62"
    prix_unitaire_ht: float = 0.0
    montant_net_ht: float = 0.0
    taux_tva: float = 20.0
    code_tva: str = "S"

    @field_validator("quantite", "prix_unitaire_ht", "montant_net_ht", "taux_tva", mode="before")
    @classmethod
    def _coerce_float(cls, v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    @field_validator("numero", "description", "unite", "code_tva", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return str(v) if v is not None else ""


class GeminiInvoiceOutput(BaseModel):
    """
    Schéma de validation des sorties Gemini avant traitement EN16931.

    Champs déclarés : ceux qui peuvent causer des crashs si mal typés.
    Champs extra (vendeur, acheteur, ventilation_tva...) : conservés tels quels.
    """

    model_config = {"extra": "allow"}

    est_facture: bool = False
    numero_facture: Optional[str] = None
    date_facture: Optional[str] = None
    date_echeance: Optional[str] = None
    type_facture: str = "380"
    devise: str = "EUR"
    montant_ht: float = 0.0
    montant_tva: float = 0.0
    montant_ttc: float = 0.0
    montant_du: float = 0.0
    montant_total_lignes_net: float = 0.0
    lignes: list = []

    @field_validator(
        "montant_ht", "montant_tva", "montant_ttc",
        "montant_du", "montant_total_lignes_net",
        mode="before",
    )
    @classmethod
    def _coerce_float(cls, v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    @field_validator("est_facture", mode="before")
    @classmethod
    def _coerce_bool(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes", "oui")
        return bool(v)

    @field_validator("lignes", mode="before")
    @classmethod
    def _coerce_lignes(cls, v: Any) -> list:
        return v if isinstance(v, list) else []


# ─────────────────────────────────────────────────────────────────────────────
# Validation STRICTE — après coercition GeminiInvoiceOutput
# ─────────────────────────────────────────────────────────────────────────────

#: Taux de TVA légaux en France (%).
_ALLOWED_TVA_RATES: frozenset[float] = frozenset({0.0, 5.5, 10.0, 20.0})


class InvoiceExtracted(BaseModel):
    """Validation stricte des données facture extraites par Gemini.

    Contrairement à ``GeminiInvoiceOutput`` (coercition permissive sans rejet),
    ce modèle lève une ``ValidationError`` si un champ obligatoire est absent,
    vide, incohérent ou hors des valeurs autorisées.

    Il est utilisé APRÈS la coercition ``GeminiInvoiceOutput`` pour décider
    si la facture peut être traitée automatiquement ou doit être renvoyée
    en révision manuelle (nœud ``manual_review``).

    Attributes:
        montant_ht: Montant hors-taxe (> 0).
        montant_ttc: Montant toutes taxes comprises (>= montant_ht).
        tva_rate: Taux de TVA dominant en % (valeurs FR: 0, 5.5, 10, 20).
        date_facture: Date au format ``YYYY-MM-DD``.
        numero_facture: Numéro de facture non vide.
        fournisseur: Nom court du fournisseur non vide.
        iban: IBAN (optionnel).
        siret: SIRET 14 chiffres (optionnel).
        adresse: Adresse ligne 1 du fournisseur (optionnel).
    """

    model_config = {"extra": "allow"}

    # ── Champs obligatoires ──────────────────────────────────────────────────
    montant_ht: float
    montant_ttc: float
    tva_rate: float
    date_facture: str
    numero_facture: str
    fournisseur: str

    # ── Champs optionnels ────────────────────────────────────────────────────
    iban: Optional[str] = None
    siret: Optional[str] = None
    adresse: Optional[str] = None

    # ── Validators champ par champ ───────────────────────────────────────────

    @field_validator("montant_ht", "montant_ttc")
    @classmethod
    def _positive_amount(cls, v: float, info: Any) -> float:
        """Vérifie que les montants sont strictement positifs."""
        if v <= 0:
            field_name = info.field_name if hasattr(info, "field_name") else "montant"
            raise ValueError(f"{field_name}={v} doit être > 0 (vérifier l'extraction OCR)")
        return v

    @field_validator("tva_rate")
    @classmethod
    def _valid_tva_rate(cls, v: float) -> float:
        """Vérifie que le taux TVA est une valeur française autorisée."""
        rounded = round(v, 1)
        if rounded not in _ALLOWED_TVA_RATES:
            raise ValueError(
                f"tva_rate={v}% non autorisé — "
                f"valeurs FR acceptées : {sorted(_ALLOWED_TVA_RATES)}"
            )
        return rounded

    @field_validator("date_facture")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        """Vérifie que la date est parseable au format YYYY-MM-DD."""
        if not v or not v.strip():
            raise ValueError("date_facture est vide")
        try:
            _dt.strptime(v.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"date_facture '{v}' non parseable — format attendu : YYYY-MM-DD"
            ) from exc
        return v.strip()

    @field_validator("numero_facture")
    @classmethod
    def _valid_numero(cls, v: str) -> str:
        """Vérifie que le numéro de facture est non vide."""
        if not v or not v.strip():
            raise ValueError("numero_facture est obligatoire et ne peut pas être vide")
        return v.strip()

    @field_validator("fournisseur")
    @classmethod
    def _valid_fournisseur(cls, v: str) -> str:
        """Vérifie que le nom fournisseur est non vide."""
        if not v or not v.strip():
            raise ValueError("fournisseur est obligatoire et ne peut pas être vide")
        return v.strip()

    # ── Validator cross-champ ────────────────────────────────────────────────

    @model_validator(mode="after")
    def _ttc_gte_ht(self) -> InvoiceExtracted:
        """Vérifie la cohérence montant_ttc >= montant_ht.

        Autorise l'égalité pour les factures en TVA à 0% (exonérées).
        """
        if self.montant_ttc < self.montant_ht:
            raise ValueError(
                f"montant_ttc ({self.montant_ttc}€) < montant_ht ({self.montant_ht}€) "
                f"— incohérent (TVA ne peut pas être négative)"
            )
        return self

    # ── Factory depuis invoice_data ------------------------------------------

    @classmethod
    def from_invoice_data(cls, data: dict) -> InvoiceExtracted:
        """Construit un ``InvoiceExtracted`` depuis le dict ``invoice_data`` Gemini.

        Args:
            data: Dict brut retourné par Gemini (après coercition GeminiInvoiceOutput).

        Returns:
            Instance validée.

        Raises:
            pydantic.ValidationError: Si les données ne passent pas la validation.
        """
        vendeur: dict = data.get("vendeur") or {}
        ventil: list = data.get("ventilation_tva") or []

        # Taux TVA dominant : ligne avec la plus grande base HT
        tva_rate: float = 0.0
        if ventil and isinstance(ventil[0], dict):
            dominant = max(
                (v for v in ventil if isinstance(v, dict)),
                key=lambda v: float(v.get("base_ht", 0.0) or 0.0),
                default=ventil[0],
            )
            tva_rate = float(dominant.get("taux", 0.0) or 0.0)
        else:
            # Fallback calculé depuis les totaux
            ht = float(data.get("montant_ht") or 0.0)
            tva = float(data.get("montant_tva") or 0.0)
            if ht > 0:
                tva_rate = round(tva / ht * 100, 1)

        return cls(
            montant_ht=float(data.get("montant_ht") or 0.0),
            montant_ttc=float(data.get("montant_ttc") or 0.0),
            tva_rate=tva_rate,
            date_facture=str(data.get("date_facture") or ""),
            numero_facture=str(data.get("numero_facture") or ""),
            fournisseur=str(
                vendeur.get("nom_court") or vendeur.get("nom") or ""
            ),
            iban=data.get("iban"),
            siret=vendeur.get("siret"),
            adresse=vendeur.get("adresse_ligne1"),
        )
