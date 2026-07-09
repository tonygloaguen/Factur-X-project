#!/usr/bin/env python3
"""test_error_label.py — P2 : label Factures-Erreur sur échec dur.

Vérifie que ``nodes._apply_error_label`` applique bien le label d'erreur à
l'email (best-effort) et ne lève jamais, même si l'API Gmail échoue.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import nodes


def _fake_services():
    services = MagicMock()
    services.get_or_create_label.return_value = "Label_123"
    return services


def test_error_label_applied_on_hard_error():
    services = _fake_services()
    state = {"services": services, "message_id": "msg_abc"}
    nodes._apply_error_label(state)

    services.get_or_create_label.assert_called_once_with(nodes.GMAIL_ERROR_LABEL)
    modify = services.gmail.users().messages().modify
    _, kwargs = modify.call_args
    assert kwargs["id"] == "msg_abc"
    assert kwargs["body"] == {"addLabelIds": ["Label_123"]}


def test_error_label_is_best_effort_on_api_failure():
    services = _fake_services()
    services.gmail.users().messages().modify.side_effect = RuntimeError("Gmail down")
    # Ne doit PAS lever.
    nodes._apply_error_label({"services": services, "message_id": "msg_x"})


def test_error_label_noop_without_services_or_id():
    # Absence de services ou de message_id → pas d'appel, pas d'exception.
    nodes._apply_error_label({"message_id": "msg_x"})
    nodes._apply_error_label({"services": _fake_services()})
