#!/usr/bin/env python3
"""
test_gemini_transient_errors.py — Tests batch 1 : erreurs transitentes Gemini
==============================================================================

Couvre :
  - 503 (et 502/504) dans call_gemini : retry avec backoff, puis HTTPError
  - 503 → succès après retry
  - node_call_gemini : retourne erreur_transient:503 (pas gemini_used)
  - node_log_result : ne marque PAS SQLite pour erreur_transient
  - Distinction erreur_transient vs erreur permanente
"""
import sys
import os
import json as json_mod
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mock_resp(status: int, body: dict | None = None, text: str = "error") -> MagicMock:
    """Construit un mock requests.Response."""
    import requests
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = {}
    if body is not None:
        r.json.return_value = body
    r.raise_for_status.side_effect = requests.exceptions.HTTPError(response=r)
    return r


def _gemini_ok_payload(data: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json_mod.dumps(data)}]}}]}


# ─────────────────────────────────────────────────────────────────────────────
# Tests call_gemini (facturx_utils)
# ─────────────────────────────────────────────────────────────────────────────

class TestCallGemini503:

    def test_503_retries_then_raises(self):
        """Après max_attempts tentatives en 503, raise HTTPError avec status 503."""
        import requests
        import facturx_utils
        from facturx_utils import call_gemini

        mock_503 = _mock_resp(503)
        with patch("requests.post", return_value=mock_503) as p, \
             patch("time.sleep"), \
             patch.object(facturx_utils, "GEMINI_API_KEY", "test-key"), \
             patch.dict(os.environ, {"GEMINI_MAX_ATTEMPTS": "3"}):
            with pytest.raises(requests.exceptions.HTTPError) as exc_info:
                call_gemini("texte facture", "")
        assert p.call_count == 3
        assert exc_info.value.response.status_code == 503

    def test_502_retries_then_raises(self):
        """502 traité comme 503 (erreur transiente)."""
        import requests
        import facturx_utils
        from facturx_utils import call_gemini

        mock_502 = _mock_resp(502)
        with patch("requests.post", return_value=mock_502), \
             patch("time.sleep"), \
             patch.object(facturx_utils, "GEMINI_API_KEY", "test-key"), \
             patch.dict(os.environ, {"GEMINI_MAX_ATTEMPTS": "2"}):
            with pytest.raises(requests.exceptions.HTTPError) as exc_info:
                call_gemini("texte", "")
        assert exc_info.value.response.status_code == 502

    def test_503_then_success(self):
        """1 erreur 503, puis succès au 2ème essai."""
        import facturx_utils
        from facturx_utils import call_gemini

        payload_ok = _gemini_ok_payload({"est_facture": False, "numero_facture": "TEST-001"})
        mock_503 = _mock_resp(503)
        mock_ok = _mock_resp(200, body=payload_ok)

        with patch("requests.post", side_effect=[mock_503, mock_ok]), \
             patch("time.sleep"), \
             patch.object(facturx_utils, "GEMINI_API_KEY", "test-key"), \
             patch.dict(os.environ, {"GEMINI_MAX_ATTEMPTS": "3"}):
            result = call_gemini("texte", "")
        assert result["est_facture"] is False

    def test_429_still_raises_after_retries(self):
        """429 reste traité (déjà existant) : raise après max_attempts."""
        import requests
        import facturx_utils
        from facturx_utils import call_gemini

        mock_429 = _mock_resp(429)
        mock_429.headers = {"Retry-After": "1"}
        with patch("requests.post", return_value=mock_429), \
             patch("time.sleep"), \
             patch.object(facturx_utils, "GEMINI_API_KEY", "test-key"), \
             patch.dict(os.environ, {"GEMINI_MAX_ATTEMPTS": "2"}):
            with pytest.raises(requests.exceptions.HTTPError) as exc_info:
                call_gemini("texte", "")
        assert exc_info.value.response.status_code == 429

    def test_400_raises_immediately(self):
        """400 (erreur permanente) raise immédiatement sans retry."""
        import requests
        import facturx_utils
        from facturx_utils import call_gemini

        mock_400 = _mock_resp(400)
        with patch("requests.post", return_value=mock_400) as p, \
             patch("time.sleep"), \
             patch.object(facturx_utils, "GEMINI_API_KEY", "test-key"), \
             patch.dict(os.environ, {"GEMINI_MAX_ATTEMPTS": "4"}):
            with pytest.raises(requests.exceptions.HTTPError):
                call_gemini("texte", "")
        assert p.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Tests node_call_gemini (nodes.py)
# ─────────────────────────────────────────────────────────────────────────────

class TestNodeCallGemini503:

    def _make_state(self):
        return {
            "ocr_text": "texte facture test",
            "subject": "Facture test",
            "sender": "test@test.com",
            "body": "",
        }

    def test_503_returns_erreur_transient(self):
        """node_call_gemini retourne erreur_transient:503 sur HTTPError 503."""
        import requests
        from nodes import node_call_gemini

        http_503 = requests.exceptions.HTTPError(response=_mock_resp(503))
        with patch("nodes.call_gemini", side_effect=http_503):
            result = node_call_gemini(self._make_state())

        assert result.get("processing_error", "").startswith("erreur_transient:")
        # gemini_used doit être absent ou False (quota non consommé côté SQLite)
        assert not result.get("gemini_used", False)

    def test_502_returns_erreur_transient(self):
        """502 traité comme 503."""
        import requests
        from nodes import node_call_gemini

        http_502 = requests.exceptions.HTTPError(response=_mock_resp(502))
        with patch("nodes.call_gemini", side_effect=http_502):
            result = node_call_gemini(self._make_state())

        assert result.get("processing_error", "").startswith("erreur_transient:")

    def test_429_returns_rate_limit(self):
        """429 retourne rate_limit_429 (comportement inchangé)."""
        import requests
        from nodes import node_call_gemini

        http_429 = requests.exceptions.HTTPError(response=_mock_resp(429))
        with patch("nodes.call_gemini", side_effect=http_429):
            result = node_call_gemini(self._make_state())

        assert result.get("processing_error") == "rate_limit_429"
        assert not result.get("gemini_used", False)

    def test_400_returns_erreur_http_gemini(self):
        """400 retourne erreur_http_gemini:400 (erreur permanente)."""
        import requests
        from nodes import node_call_gemini

        http_400 = requests.exceptions.HTTPError(response=_mock_resp(400))
        with patch("nodes.call_gemini", side_effect=http_400):
            result = node_call_gemini(self._make_state())

        assert "erreur_http_gemini:400" in result.get("processing_error", "")
        # gemini_used=True : erreur permanente → comptabilisée dans le quota
        assert result.get("gemini_used") is True


# ─────────────────────────────────────────────────────────────────────────────
# Tests node_log_result : erreur_transient ne marque pas SQLite
# ─────────────────────────────────────────────────────────────────────────────

class TestNodeLogResultTransient:

    def _base_state(self, db, error: str) -> dict:
        return {
            "message_id": "msg_X",
            "pdf_filename": "facture.pdf",
            "subject": "Test",
            "sender": "test@example.com",
            "processing_error": error,
            "state_db": db,
            "invoice_data": {},
            "drive_file_url": "",
            "gemini_used": False,
            "prior_message_id": "",
            "client_final": "",
        }

    def test_erreur_transient_503_not_marked_sqlite(self, tmp_path):
        """erreur_transient:503 → NE PAS marquer dans SQLite."""
        from services import StateDB
        from nodes import node_log_result

        db = StateDB(str(tmp_path / "test.db"))
        node_log_result(self._base_state(db, "erreur_transient:503"))
        assert not db.is_seen("msg_X", "facture.pdf")

    def test_erreur_transient_502_not_marked_sqlite(self, tmp_path):
        """erreur_transient:502 → NE PAS marquer dans SQLite."""
        from services import StateDB
        from nodes import node_log_result

        db = StateDB(str(tmp_path / "test.db"))
        node_log_result(self._base_state(db, "erreur_transient:502"))
        assert not db.is_seen("msg_X", "facture.pdf")

    def test_rate_limit_429_not_marked_sqlite(self, tmp_path):
        """rate_limit_429 → NE PAS marquer dans SQLite (comportement inchangé)."""
        from services import StateDB
        from nodes import node_log_result

        db = StateDB(str(tmp_path / "test.db"))
        node_log_result(self._base_state(db, "rate_limit_429"))
        assert not db.is_seen("msg_X", "facture.pdf")

    def test_permanent_error_is_marked_sqlite(self, tmp_path):
        """Erreur permanente (ex: erreur_json_permanent) → marquée dans SQLite."""
        from services import StateDB
        from nodes import node_log_result

        db = StateDB(str(tmp_path / "test.db"))
        node_log_result(self._base_state(db, "erreur_json_permanent:json decode error"))
        assert db.is_seen("msg_X", "facture.pdf")

    def test_not_invoice_is_marked_sqlite(self, tmp_path):
        """not_invoice → marqué dans SQLite (skip futur garanti)."""
        from services import StateDB
        from nodes import node_log_result

        db = StateDB(str(tmp_path / "test.db"))
        node_log_result(self._base_state(db, "not_invoice:deny_hard:lcr"))
        assert db.is_seen("msg_X", "facture.pdf")
