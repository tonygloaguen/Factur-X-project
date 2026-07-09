#!/usr/bin/env python3
"""test_gmail_window.py — P2 : fenêtre de collecte Gmail paramétrable.

Vérifie que ``main._ensure_window`` :
  - impose ``newer_than:{POLL_WINDOW_DAYS}d`` par défaut (30 j) ;
  - respecte une borne déjà présente dans la requête ;
  - retire toute borne en mode rattrapage (GMAIL_CATCHUP) ;
  - se désactive via GMAIL_ENFORCE_WINDOW=false.

conftest stubifie googleapiclient/langgraph/etc. → import de main possible en
environnement léger.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import main


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Restaure les globals du module autour de chaque test."""
    monkeypatch.setattr(main, "GMAIL_CATCHUP", False, raising=False)
    monkeypatch.setattr(main, "POLL_WINDOW_DAYS", 30, raising=False)
    monkeypatch.delenv("GMAIL_ENFORCE_WINDOW", raising=False)
    monkeypatch.delenv("GMAIL_ENFORCE_7D", raising=False)
    yield


def test_default_adds_30d_window():
    q = main._ensure_window("has:attachment filename:pdf -label:Factures-Traitées")
    assert "newer_than:30d" in q


def test_custom_window_days(monkeypatch):
    monkeypatch.setattr(main, "POLL_WINDOW_DAYS", 45)
    assert "newer_than:45d" in main._ensure_window("has:attachment filename:pdf")


def test_existing_bound_is_respected(monkeypatch):
    monkeypatch.setattr(main, "POLL_WINDOW_DAYS", 30)
    q = main._ensure_window("has:attachment newer_than:3d")
    assert "newer_than:3d" in q
    assert "newer_than:30d" not in q


def test_catchup_removes_time_bound(monkeypatch):
    monkeypatch.setattr(main, "GMAIL_CATCHUP", True)
    q = main._ensure_window("has:attachment filename:pdf newer_than:7d")
    assert "newer_than" not in q
    assert "has:attachment filename:pdf" in q


def test_enforce_window_false_leaves_query_untouched(monkeypatch):
    monkeypatch.setenv("GMAIL_ENFORCE_WINDOW", "false")
    q = main._ensure_window("has:attachment filename:pdf")
    assert "newer_than" not in q


def test_legacy_enforce_7d_false_still_honoured(monkeypatch):
    """Rétro-compat : l'ancienne variable GMAIL_ENFORCE_7D=false désactive aussi."""
    monkeypatch.setenv("GMAIL_ENFORCE_7D", "false")
    q = main._ensure_window("has:attachment filename:pdf")
    assert "newer_than" not in q
