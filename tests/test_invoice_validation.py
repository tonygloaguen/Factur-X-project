#!/usr/bin/env python3
"""
test_invoice_validation.py — Tests d'intégration de la validation stricte Pydantic
====================================================================================

Couvre :
  1. ``InvoiceExtracted.from_invoice_data()`` — validations unitaires
  2. ``_validate_invoice_strict()``            — helper nodes.py
  3. ``route_after_gemini()``                  — routage vers manual_review
  4. ``node_call_gemini()``                    — intégration avec mock Gemini

Usage::

    pytest tests/test_invoice_validation.py -v
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from pydantic import ValidationError
from schemas import InvoiceExtracted


# ── Fixtures partagées -------------------------------------------------------

def _valid_data() -> dict[str, Any]:
    """Dict invoice_data minimal valide (toutes validations passent)."""
    return {
        "est_facture": True,
        "numero_facture": "F-2026-001",
        "date_facture": "2026-01-15",
        "montant_ht": 100.0,
        "montant_tva": 20.0,
        "montant_ttc": 120.0,
        "montant_du": 120.0,
        "vendeur": {
            "nom": "MSA FRANCE SAS",
            "nom_court": "MSA FRANCE",
            "siret": "12345678901234",
            "adresse_ligne1": "1 rue du Test",
        },
        "ventilation_tva": [
            {"code_tva": "S", "taux": 20.0, "base_ht": 100.0, "montant_tva": 20.0}
        ],
        "iban": "FR7630006000011234567890189",
        "lignes": [],
    }


def _make_node_state(invoice_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Crée un state minimal pour node_call_gemini."""
    base = _valid_data()
    if invoice_override:
        base.update(invoice_override)
    return {
        "message_id": "test-msg-001",
        "subject": "Facture TEST-001",
        "sender": "fournisseur@example.com",
        "body": "Veuillez trouver ci-joint notre facture.",
        "ocr_text": "FACTURE F-2026-001 MSA FRANCE 100 EUR HT TVA 20 EUR TTC 120 EUR",
        "invoice_data": base,
    }


# ── Tests InvoiceExtracted valide -------------------------------------------

class TestInvoiceExtractedValid:
    """Cas nominaux : from_invoice_data réussit sans lever de ValidationError."""

    def test_nominal_all_fields(self) -> None:
        inv = InvoiceExtracted.from_invoice_data(_valid_data())
        assert inv.montant_ht == 100.0
        assert inv.montant_ttc == 120.0
        assert inv.tva_rate == 20.0
        assert inv.date_facture == "2026-01-15"
        assert inv.numero_facture == "F-2026-001"
        assert inv.fournisseur == "MSA FRANCE"
        assert inv.iban == "FR7630006000011234567890189"
        assert inv.siret == "12345678901234"
        assert inv.adresse == "1 rue du Test"

    def test_tva_zero_percent(self) -> None:
        """Factures exonérées TVA 0% : TTC == HT est autorisé."""
        data = _valid_data()
        data["montant_tva"] = 0.0
        data["montant_ttc"] = 100.0
        data["ventilation_tva"] = [
            {"code_tva": "Z", "taux": 0.0, "base_ht": 100.0, "montant_tva": 0.0}
        ]
        inv = InvoiceExtracted.from_invoice_data(data)
        assert inv.tva_rate == 0.0
        assert inv.montant_ttc == inv.montant_ht

    def test_tva_5_5_percent(self) -> None:
        data = _valid_data()
        data["ventilation_tva"] = [
            {"taux": 5.5, "base_ht": 100.0, "montant_tva": 5.5}
        ]
        data["montant_tva"] = 5.5
        data["montant_ttc"] = 105.5
        inv = InvoiceExtracted.from_invoice_data(data)
        assert inv.tva_rate == 5.5

    def test_tva_10_percent(self) -> None:
        data = _valid_data()
        data["ventilation_tva"] = [
            {"taux": 10.0, "base_ht": 100.0, "montant_tva": 10.0}
        ]
        data["montant_tva"] = 10.0
        data["montant_ttc"] = 110.0
        inv = InvoiceExtracted.from_invoice_data(data)
        assert inv.tva_rate == 10.0

    def test_optional_fields_none(self) -> None:
        """Champs optionnels absents : pas d'erreur."""
        data = _valid_data()
        data.pop("iban", None)
        data["vendeur"]["siret"] = None
        data["vendeur"]["adresse_ligne1"] = None
        inv = InvoiceExtracted.from_invoice_data(data)
        assert inv.iban is None
        assert inv.siret is None
        assert inv.adresse is None

    def test_dominant_tva_rate_picked(self) -> None:
        """Facture multi-taux : le taux dominant (plus grande base HT) est choisi."""
        data = _valid_data()
        data["ventilation_tva"] = [
            {"taux": 10.0, "base_ht": 30.0, "montant_tva": 3.0},
            {"taux": 20.0, "base_ht": 70.0, "montant_tva": 14.0},
        ]
        data["montant_ht"] = 100.0
        data["montant_tva"] = 17.0
        data["montant_ttc"] = 117.0
        inv = InvoiceExtracted.from_invoice_data(data)
        assert inv.tva_rate == 20.0  # base_ht=70 > base_ht=30

    def test_tva_computed_from_totals_fallback(self) -> None:
        """Sans ventilation_tva, le taux est calculé depuis les totaux."""
        data = _valid_data()
        data["ventilation_tva"] = []  # vide → fallback calcul
        # 20/100 * 100 = 20.0%
        inv = InvoiceExtracted.from_invoice_data(data)
        assert inv.tva_rate == 20.0

    def test_numero_whitespace_stripped(self) -> None:
        data = _valid_data()
        data["numero_facture"] = "  F-2026-001  "
        inv = InvoiceExtracted.from_invoice_data(data)
        assert inv.numero_facture == "F-2026-001"


# ── Tests InvoiceExtracted invalide -----------------------------------------

class TestInvoiceExtractedInvalid:
    """Cas d'erreur : from_invoice_data doit lever ValidationError."""

    def test_montant_ttc_less_than_ht(self) -> None:
        data = _valid_data()
        data["montant_ttc"] = 80.0  # < montant_ht=100
        with pytest.raises(ValidationError) as exc_info:
            InvoiceExtracted.from_invoice_data(data)
        msgs = " ".join(e["msg"] for e in exc_info.value.errors())
        assert "ttc" in msgs.lower() or "ht" in msgs.lower() or "incohérent" in msgs

    def test_date_wrong_format(self) -> None:
        data = _valid_data()
        data["date_facture"] = "15/01/2026"  # format FR au lieu de YYYY-MM-DD
        with pytest.raises(ValidationError) as exc_info:
            InvoiceExtracted.from_invoice_data(data)
        msgs = " ".join(e["msg"] for e in exc_info.value.errors())
        assert "YYYY-MM-DD" in msgs or "parseable" in msgs

    def test_date_nonsense_string(self) -> None:
        data = _valid_data()
        data["date_facture"] = "pas-une-date"
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_date_empty(self) -> None:
        data = _valid_data()
        data["date_facture"] = None
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_tva_rate_invalid(self) -> None:
        """Taux 7% inexistant en France → rejet."""
        data = _valid_data()
        data["ventilation_tva"] = [
            {"taux": 7.0, "base_ht": 100.0, "montant_tva": 7.0}
        ]
        data["montant_tva"] = 7.0
        data["montant_ttc"] = 107.0
        with pytest.raises(ValidationError) as exc_info:
            InvoiceExtracted.from_invoice_data(data)
        msgs = " ".join(e["msg"] for e in exc_info.value.errors())
        assert "7.0" in msgs or "non autorisé" in msgs

    def test_numero_facture_empty_string(self) -> None:
        data = _valid_data()
        data["numero_facture"] = ""
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_numero_facture_none(self) -> None:
        data = _valid_data()
        data["numero_facture"] = None
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_fournisseur_empty(self) -> None:
        data = _valid_data()
        data["vendeur"]["nom_court"] = ""
        data["vendeur"]["nom"] = ""
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_fournisseur_none_vendeur(self) -> None:
        data = _valid_data()
        data["vendeur"] = {}  # plus de nom ni nom_court
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_montant_ht_zero(self) -> None:
        data = _valid_data()
        data["montant_ht"] = 0.0
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_montant_ttc_zero(self) -> None:
        data = _valid_data()
        data["montant_ttc"] = 0.0
        with pytest.raises(ValidationError):
            InvoiceExtracted.from_invoice_data(data)

    def test_multiple_errors_all_reported(self) -> None:
        """Plusieurs champs invalides → toutes les erreurs sont signalées."""
        data = _valid_data()
        data["montant_ttc"] = 50.0      # < HT
        data["numero_facture"] = ""     # vide
        data["date_facture"] = "31-12"  # mauvais format
        with pytest.raises(ValidationError) as exc_info:
            InvoiceExtracted.from_invoice_data(data)
        assert exc_info.value.error_count() >= 2


# ── Tests _validate_invoice_strict ------------------------------------------

class TestValidateInvoiceStrict:
    """Tests du helper ``_validate_invoice_strict`` exposé par nodes.py."""

    def test_valid_data_returns_empty_list(self) -> None:
        from nodes import _validate_invoice_strict
        assert _validate_invoice_strict(_valid_data()) == []

    def test_invalid_data_returns_messages(self) -> None:
        from nodes import _validate_invoice_strict
        data = _valid_data()
        data["montant_ttc"] = 50.0
        errors = _validate_invoice_strict(data)
        assert len(errors) >= 1
        assert all(isinstance(e, str) for e in errors)

    def test_multiple_errors_all_in_list(self) -> None:
        from nodes import _validate_invoice_strict
        data = _valid_data()
        data["montant_ttc"] = 50.0
        data["numero_facture"] = ""
        data["date_facture"] = "bad"
        errors = _validate_invoice_strict(data)
        assert len(errors) >= 2

    def test_error_messages_contain_field_name(self) -> None:
        from nodes import _validate_invoice_strict
        data = _valid_data()
        data["numero_facture"] = ""
        errors = _validate_invoice_strict(data)
        combined = " ".join(errors).lower()
        assert "numero" in combined or "obligatoire" in combined


# ── Tests route_after_gemini -------------------------------------------------

class TestRouteAfterGemini:
    """Tests de la fonction de routage ``route_after_gemini`` dans nodes.py."""

    def test_no_error_routes_to_normalize(self) -> None:
        from nodes import route_after_gemini
        assert route_after_gemini({"invoice_data": {}, "gemini_used": True}) == "normalize_data"

    def test_validation_ko_routes_to_manual_review(self) -> None:
        from nodes import route_after_gemini
        state = {
            "processing_error": "validation_ko:montant_ht: doit être > 0",
            "gemini_used": True,
        }
        assert route_after_gemini(state) == "manual_review"

    def test_validation_ko_prefix_required(self) -> None:
        """Un processing_error sans préfixe validation_ko va vers log_result."""
        from nodes import route_after_gemini
        state = {"processing_error": "erreur_json_permanent:...", "gemini_used": True}
        assert route_after_gemini(state) == "log_result"

    def test_rate_limit_routes_to_log_result(self) -> None:
        from nodes import route_after_gemini
        assert route_after_gemini({"processing_error": "rate_limit_429"}) == "log_result"

    def test_not_invoice_routes_to_log_result(self) -> None:
        from nodes import route_after_gemini
        state = {"processing_error": "not_invoice_gemini:est_facture=false"}
        assert route_after_gemini(state) == "log_result"

    def test_empty_string_error_routes_to_normalize(self) -> None:
        from nodes import route_after_gemini
        assert route_after_gemini({"processing_error": ""}) == "normalize_data"


# ── Tests node_call_gemini intégration --------------------------------------

class TestNodeCallGeminiValidation:
    """Tests d'intégration de la validation dans node_call_gemini (mock Gemini)."""

    def test_valid_invoice_no_processing_error(self) -> None:
        from nodes import node_call_gemini
        with patch("nodes.call_gemini", return_value=_valid_data()):
            result = node_call_gemini(_make_node_state())
        assert not result.get("processing_error")
        assert result.get("gemini_used") is True
        assert "invoice_data" in result

    def test_invalid_invoice_sets_validation_ko(self) -> None:
        from nodes import node_call_gemini
        bad = _valid_data()
        bad["montant_ttc"] = 50.0  # < montant_ht → incohérent
        with patch("nodes.call_gemini", return_value=bad):
            result = node_call_gemini(_make_node_state())
        err = result.get("processing_error", "")
        assert err.startswith("validation_ko:"), f"Attendu validation_ko:…, reçu: {err!r}"
        assert result.get("gemini_used") is True

    def test_validation_ko_preserves_invoice_data(self) -> None:
        """Les données brutes sont toujours retournées même en cas de rejet."""
        from nodes import node_call_gemini
        bad = _valid_data()
        bad["numero_facture"] = ""
        with patch("nodes.call_gemini", return_value=bad):
            result = node_call_gemini(_make_node_state())
        assert "invoice_data" in result
        assert result["invoice_data"].get("montant_ht") == 100.0

    def test_not_invoice_bypasses_strict_validation(self) -> None:
        """est_facture=False → rejet avant validation stricte (pas de validation_ko)."""
        from nodes import node_call_gemini
        not_invoice = _valid_data()
        not_invoice["est_facture"] = False
        with patch("nodes.call_gemini", return_value=not_invoice):
            result = node_call_gemini(_make_node_state())
        err = result.get("processing_error", "")
        assert "not_invoice" in err
        assert not err.startswith("validation_ko:")

    def test_gemini_json_error_not_validation_ko(self) -> None:
        """Erreur JSON Gemini → erreur_json_permanent, pas validation_ko."""
        from nodes import node_call_gemini
        from facturx_utils import GeminiJsonDecodeError
        with patch("nodes.call_gemini", side_effect=GeminiJsonDecodeError("JSON invalide")):
            result = node_call_gemini(_make_node_state())
        err = result.get("processing_error", "")
        assert "erreur_json_permanent" in err
        assert not err.startswith("validation_ko:")
