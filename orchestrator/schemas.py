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

from typing import Any, Optional

from pydantic import BaseModel, field_validator


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
