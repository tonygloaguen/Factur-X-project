#!/usr/bin/env python3
"""
test_dedup_lastoccurrence.py — Tests batch 1 : règle "dernière occurrence fait foi"
====================================================================================

Couvre :
  - is_seen : clé exacte (message_id, filename)
  - get_latest_by_filename : retourne le dernier enregistrement actif par filename
  - mark_superseded : marque une ancienne occurrence comme remplacée
  - Comportement attendu dans les cas métier clés
"""
import sys
import os
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from services import StateDB


@pytest.fixture
def db(tmp_path):
    return StateDB(str(tmp_path / "test.db"))


# ─────────────────────────────────────────────────────────────────────────────
# is_seen : garantie clé exacte (message_id, filename)
# ─────────────────────────────────────────────────────────────────────────────

def test_is_seen_exact_key(db):
    """is_seen retourne True UNIQUEMENT pour la paire exacte."""
    db.mark("msg_A", "facture.pdf", "success")
    assert db.is_seen("msg_A", "facture.pdf") is True
    # Autre email, même fichier → pas vu
    assert db.is_seen("msg_B", "facture.pdf") is False
    # Même email, autre fichier → pas vu
    assert db.is_seen("msg_A", "autre.pdf") is False


def test_is_seen_false_on_new_db(db):
    """Base vide → jamais vu."""
    assert db.is_seen("msg_X", "facture.pdf") is False


# ─────────────────────────────────────────────────────────────────────────────
# get_latest_by_filename
# ─────────────────────────────────────────────────────────────────────────────

def test_get_latest_by_filename_none_on_empty(db):
    """Aucun enregistrement pour ce fichier → None."""
    assert db.get_latest_by_filename("inconnu.pdf") is None


def test_get_latest_by_filename_returns_entry(db):
    """Retourne le bon enregistrement après mark."""
    db.mark("msg_A", "facture.pdf", "success", drive_url="https://drive/A")
    r = db.get_latest_by_filename("facture.pdf")
    assert r is not None
    assert r["message_id"] == "msg_A"
    assert r["status"] == "success"
    assert r["drive_url"] == "https://drive/A"


def test_get_latest_by_filename_different_files_isolated(db):
    """Les fichiers différents sont isolés."""
    db.mark("msg_A", "facture_jan.pdf", "success")
    db.mark("msg_B", "facture_fev.pdf", "error")
    assert db.get_latest_by_filename("facture_jan.pdf")["message_id"] == "msg_A"
    assert db.get_latest_by_filename("facture_fev.pdf")["message_id"] == "msg_B"
    assert db.get_latest_by_filename("inexistant.pdf") is None


# ─────────────────────────────────────────────────────────────────────────────
# mark_superseded
# ─────────────────────────────────────────────────────────────────────────────

def test_mark_superseded_hides_old_entry(db):
    """Après supersession, get_latest_by_filename ne retourne plus l'ancienne entrée."""
    db.mark("msg_A", "facture.pdf", "success", drive_url="https://drive/A")
    db.mark("msg_B", "facture.pdf", "success", drive_url="https://drive/B")
    db.mark_superseded("msg_A", "facture.pdf", "msg_B")

    latest = db.get_latest_by_filename("facture.pdf")
    assert latest["message_id"] == "msg_B"
    assert latest["drive_url"] == "https://drive/B"


def test_mark_superseded_idempotent(db):
    """Appeler mark_superseded deux fois ne casse pas la base."""
    db.mark("msg_A", "facture.pdf", "success")
    db.mark("msg_B", "facture.pdf", "success")
    db.mark_superseded("msg_A", "facture.pdf", "msg_B")
    db.mark_superseded("msg_A", "facture.pdf", "msg_B")  # 2ème appel sans effet
    assert db.get_latest_by_filename("facture.pdf")["message_id"] == "msg_B"


# ─────────────────────────────────────────────────────────────────────────────
# Cas métier : ancienne occurrence success, nouvelle occurrence error
# ─────────────────────────────────────────────────────────────────────────────

def test_new_occurrence_error_after_success(db):
    """
    msg_A → success, msg_B (autre email, même fichier) → error.
    La dernière occurrence (error) fait foi → get_latest retourne msg_B après supersession.
    """
    db.mark("msg_A", "facture.pdf", "success")
    # msg_B arrive : is_seen(msg_B, facture.pdf) = False → traité
    assert not db.is_seen("msg_B", "facture.pdf")
    # Traitement de msg_B → erreur
    db.mark("msg_B", "facture.pdf", "error")
    db.mark_superseded("msg_A", "facture.pdf", "msg_B")

    latest = db.get_latest_by_filename("facture.pdf")
    assert latest["message_id"] == "msg_B"
    assert latest["status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# Cas métier : ancienne occurrence success, nouvelle occurrence success
# ─────────────────────────────────────────────────────────────────────────────

def test_new_occurrence_success_after_success(db):
    """
    msg_A → success, msg_B (même fichier, autre email) → success plus récent.
    Après supersession, get_latest retourne msg_B.
    """
    db.mark("msg_A", "facture.pdf", "success", drive_url="https://drive/A")
    db.mark("msg_B", "facture.pdf", "success", drive_url="https://drive/B")
    db.mark_superseded("msg_A", "facture.pdf", "msg_B")

    latest = db.get_latest_by_filename("facture.pdf")
    assert latest["message_id"] == "msg_B"
    assert latest["drive_url"] == "https://drive/B"


# ─────────────────────────────────────────────────────────────────────────────
# Cas métier : fichier not_invoice → doit être skippé
# ─────────────────────────────────────────────────────────────────────────────

def test_not_invoice_prior_allows_skip(db):
    """
    get_latest retourne not_invoice → main.py doit skip (même PDF = même décision).
    """
    db.mark("msg_A", "releve.pdf", "not_invoice", detail="deny_hard:lcr")
    prior = db.get_latest_by_filename("releve.pdf")
    assert prior["status"] == "not_invoice"
    # La logique dans main.py vérifie prior["status"] == "not_invoice" → continue


# ─────────────────────────────────────────────────────────────────────────────
# Cas métier : même email, même fichier → skip garanti via is_seen
# ─────────────────────────────────────────────────────────────────────────────

def test_exact_same_email_skipped(db):
    """
    Même (message_id, filename) → is_seen retourne True → skip garanti.
    Protège contre le retraitement lors du polling sans labellisation.
    """
    db.mark("msg_A", "facture.pdf", "success")
    assert db.is_seen("msg_A", "facture.pdf") is True


# ─────────────────────────────────────────────────────────────────────────────
# Migration : base existante sans colonne superseded_by
# ─────────────────────────────────────────────────────────────────────────────

def test_migration_existing_db(tmp_path):
    """La migration additive n'altère pas les enregistrements existants."""
    import sqlite3
    db_path = str(tmp_path / "legacy.db")
    # Créer une base sans la colonne superseded_by (schéma d'avant batch 1)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE processed (
            message_id TEXT NOT NULL, filename TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT DEFAULT '',
            drive_url TEXT DEFAULT '', created_at TEXT NOT NULL,
            PRIMARY KEY (message_id, filename)
        )
    """)
    conn.execute(
        "INSERT INTO processed VALUES ('msg_old','f.pdf','success','','','2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    # Ouvrir avec StateDB → migration automatique
    db = StateDB(db_path)
    assert db.is_seen("msg_old", "f.pdf") is True
    r = db.get_latest_by_filename("f.pdf")
    assert r is not None
    assert r["status"] == "success"
