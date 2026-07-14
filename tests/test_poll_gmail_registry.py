#!/usr/bin/env python3
"""test_poll_gmail_registry.py — Régression : registry passé à poll_gmail.

Reproduit le crash « name 'registry' is not defined » (chaque email en échec) :
poll_gmail construisait l'état initial avec ``registry`` sans le recevoir en
paramètre. Ce test fait transiter un email jusqu'à ``workflow.invoke`` avec une
API Gmail simulée et vérifie que l'état porte bien ``registry`` (aucun NameError).
"""
import base64
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import main


def _gmail_stub():
    """Client Gmail simulé : 1 email avec une PJ PDF inline."""
    pdf_b64 = base64.urlsafe_b64encode(b"%PDF-1.4 test").decode()
    message = {
        "id": "msg1",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "INTERBAT - Facture N°FA132301"},
                {"name": "From", "value": "Compta Interbat <compta@interbat.fr>"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"corps").decode()}},
                {"mimeType": "application/pdf", "filename": "Facture_FA132301.pdf",
                 "body": {"data": pdf_b64}},
            ],
        },
    }

    gmail = MagicMock()
    users = gmail.users.return_value
    messages = users.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": "msg1"}]}
    messages.get.return_value.execute.return_value = message
    return gmail


def test_poll_gmail_passes_registry_into_state(monkeypatch):
    monkeypatch.setattr(main, "MAX_EMAILS_PER_CYCLE", 5, raising=False)

    services = MagicMock()
    services.gmail = _gmail_stub()

    state_db = MagicMock()
    state_db.gemini_calls_today.return_value = 0
    state_db.is_seen.return_value = False
    state_db.get_latest_by_filename.return_value = None

    workflow = MagicMock()
    sentinel_registry = object()

    # Ne doit PAS lever « name 'registry' is not defined ».
    main.poll_gmail(services, workflow, state_db, sentinel_registry)

    assert workflow.invoke.called, "le workflow aurait dû être invoqué pour l'email"
    state = workflow.invoke.call_args[0][0]
    assert state["registry"] is sentinel_registry
    assert state["message_id"] == "msg1"
    assert state["pdf_filename"] == "Facture_FA132301.pdf"
