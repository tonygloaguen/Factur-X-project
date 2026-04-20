#!/usr/bin/env python3
"""
services.py — Services partagés : Google OAuth2 + SQLite
=========================================================

Ces classes sont instanciées UNE SEULE FOIS au démarrage de l'application,
puis injectées dans l'état LangGraph à chaque invocation du workflow.

Ce module est indépendant de LangGraph : il ne connaît pas InvoiceState.
C'est intentionnel — les services métier ne doivent pas dépendre du framework.
"""

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger("orchestrator")

# Scopes OAuth2 requis pour Gmail (lecture/modification), Drive (upload)
# et Sheets (mise à jour de la matrice de suivi fournisseurs)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]

CREDENTIALS_FILE = Path("/app/credentials.json")
TOKEN_FILE = Path("/app/token.json")


# ─────────────────────────────────────────────────────────────────────────────
# Authentification Google OAuth2
# ─────────────────────────────────────────────────────────────────────────────

def get_google_credentials() -> Credentials:
    """
    Charge ou demande les credentials Google OAuth2.

    Logique :
      1. Si token.json existe → charger et vérifier la validité
      2. Si expiré + refresh_token → renouveler silencieusement
      3. Sinon → lancer le flow OAuth2 interactif (seulement à la 1ère config)
    """
    creds = None

    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception as e:
            logger.warning("Token existant invalide : %s", e)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Rafraîchissement du token Google...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                logger.error(
                    "Fichier credentials.json introuvable !\n"
                    "Télécharge-le depuis Google Cloud Console :\n"
                    "  1. https://console.cloud.google.com/\n"
                    "  2. APIs et services → Identifiants\n"
                    "  3. Télécharge le JSON de ton ID client OAuth2\n"
                    "  4. Place-le dans orchestrator/credentials.json"
                )
                sys.exit(1)

            logger.info(
                "=== PREMIÈRE AUTORISATION GOOGLE ===\n"
                "Une URL va s'afficher. Ouvre-la dans ton navigateur.\n"
                "Connecte-toi et autorise l'accès Gmail + Drive.\n"
                "===================================="
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=False)

        TOKEN_FILE.write_text(creds.to_json())
        logger.info("Token Google sauvegardé dans %s", TOKEN_FILE)

    return creds


# ─────────────────────────────────────────────────────────────────────────────
# Clients Google (Gmail + Drive)
# ─────────────────────────────────────────────────────────────────────────────

class GoogleServices:
    """
    Encapsule les clients Gmail et Drive.

    Instancié une seule fois au démarrage, partagé pour tous les cycles de polling.
    Les labels Gmail sont mis en cache dans un dict (évite un appel API list par email).
    """

    __slots__ = ("gmail", "drive", "sheets", "_label_cache")

    def __init__(self, creds: Credentials):
        self.gmail = build("gmail", "v1", credentials=creds)
        self.drive = build("drive", "v3", credentials=creds)
        self.sheets = build("sheets", "v4", credentials=creds)
        self._label_cache: dict[str, str] = {}  # {label_name: label_id}

    def get_or_create_label(self, label_name: str) -> str:
        """Retourne l'ID du label Gmail (le crée s'il n'existe pas encore).

        La recherche est case-insensitive pour éviter les doublons de casse
        (ex : 'INTERBAT' matche le label existant 'interbat').
        Le cache est partagé pour tous les appels dans le même processus.
        """
        label_name_lower = label_name.lower()

        # Fast path : check cache (case-insensitive)
        for cached_name, cached_id in self._label_cache.items():
            if cached_name.lower() == label_name_lower:
                return cached_id

        # Refresh full label list into cache
        results = self.gmail.users().labels().list(userId="me").execute()
        for lbl in results.get("labels", []):
            self._label_cache[lbl["name"]] = lbl["id"]

        # Check again after refresh (case-insensitive)
        for cached_name, cached_id in self._label_cache.items():
            if cached_name.lower() == label_name_lower:
                return cached_id

        # Label doesn't exist — create it
        # Gmail imbrique automatiquement "Fournisseurs/Nom" sous "Fournisseurs"
        body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = self.gmail.users().labels().create(userId="me", body=body).execute()
        self._label_cache[created["name"]] = created["id"]
        logger.info("Label Gmail créé : %s (ID: %s)", created["name"], created["id"])
        return created["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Base SQLite : anti-replay + suivi des quotas Gemini
# ─────────────────────────────────────────────────────────────────────────────

class StateDB:
    """
    Base SQLite qui trace chaque paire (email, PDF) traitée.

    Table `processed` :
      (message_id, filename) → PRIMARY KEY (garantit l'anti-replay exact)
      status        : 'success' | 'not_invoice' | 'error' | 'skipped'
      detail        : info supplémentaire (fournisseur, raison de rejet…)
      drive_url     : lien partageable Drive si upload réussi
      created_at    : timestamp UTC (comptage quota Gemini/jour)
      superseded_by : message_id de l'occurrence suivante si ce traitement
                      a été remplacé par une version plus récente du même fichier

    Règle "dernière occurrence fait foi" :
      Quand le même nom de fichier arrive dans plusieurs emails,
      chaque nouvelle occurrence traitée avec succès marque la précédente
      comme superseded_by = nouveau_message_id.
      get_latest_by_filename() ne retourne que l'entrée active (superseded_by IS NULL).

    Choix SQLite vs Redis/Postgres :
      → Pas de serveur séparé à gérer
      → Mode WAL pour la concurrence (safe en multi-thread)
      → Survit aux redémarrages du container (volume persistant)
      → Légèreté : <1 MB pour des années de logs de factures
    """

    __slots__ = ("_conn",)

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                message_id  TEXT NOT NULL,
                filename    TEXT NOT NULL,
                status      TEXT NOT NULL,
                detail      TEXT DEFAULT '',
                drive_url   TEXT DEFAULT '',
                created_at  TEXT NOT NULL,
                PRIMARY KEY (message_id, filename)
            )
        """)
        # Migration additive (batch 1) — idempotente, ne casse pas les anciens enregistrements.
        # ALTER TABLE échoue silencieusement si la colonne existe déjà.
        try:
            self._conn.execute(
                "ALTER TABLE processed ADD COLUMN superseded_by TEXT DEFAULT NULL"
            )
        except Exception:
            pass  # Colonne déjà présente — migration déjà appliquée
        # Index sur filename seul pour get_latest_by_filename() en O(log n)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_filename ON processed(filename)"
        )
        self._conn.commit()
        logger.info("StateDB ouverte : %s", db_path)

    def is_seen(self, message_id: str, filename: str) -> bool:
        """True si ce (message_id, filename) exact a déjà été traité.

        Ne regarde QUE la paire (message_id, filename) — pas d'autres emails.
        Pour vérifier si un NOM DE FICHIER a déjà été traité (quel que soit l'email),
        utiliser get_latest_by_filename().
        """
        row = self._conn.execute(
            "SELECT 1 FROM processed WHERE message_id = ? AND filename = ?",
            (message_id, filename),
        ).fetchone()
        return row is not None

    def get_latest_by_filename(self, filename: str) -> Optional[dict]:
        """Retourne le dernier enregistrement actif pour ce nom de fichier.

        "Actif" = superseded_by IS NULL (non remplacé par une occurrence ultérieure).
        Retourne None si le fichier n'a jamais été traité.

        Utilisé dans main.py pour implémenter la règle :
          "si un même nom de fichier arrive dans plusieurs emails,
           seule la dernière occurrence fait foi"
          → not_invoice précédent : skip (même PDF = même décision attendue)
          → success/error précédent : retraiter (nouvelle occurrence peut primer)
        """
        row = self._conn.execute(
            """SELECT message_id, filename, status, detail, drive_url, created_at
               FROM processed
               WHERE filename = ? AND superseded_by IS NULL
               ORDER BY created_at DESC
               LIMIT 1""",
            (filename,),
        ).fetchone()
        if row is None:
            return None
        return {
            "message_id": row[0],
            "filename":   row[1],
            "status":     row[2],
            "detail":     row[3],
            "drive_url":  row[4],
            "created_at": row[5],
        }

    def mark_superseded(self, old_message_id: str, filename: str, new_message_id: str) -> None:
        """Marque une occurrence précédente comme remplacée par la nouvelle.

        Appelé dans node_log_result après un succès sur un fichier déjà connu.
        Après cet appel, get_latest_by_filename() retourne la nouvelle occurrence.

        Note : on ne supprime rien — l'historique complet est conservé pour audit.
        """
        self._conn.execute(
            """UPDATE processed SET superseded_by = ?
               WHERE message_id = ? AND filename = ? AND superseded_by IS NULL""",
            (new_message_id, old_message_id, filename),
        )
        self._conn.commit()

    def mark(self, message_id: str, filename: str, status: str,
             detail: str = "", drive_url: str = ""):
        """Enregistre (ou remplace) un traitement dans SQLite."""
        self._conn.execute(
            """INSERT OR REPLACE INTO processed
               (message_id, filename, status, detail, drive_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (message_id, filename, status, detail, drive_url,
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def gemini_calls_today(self) -> int:
        """Compte les traitements qui ont consommé du quota Gemini aujourd'hui."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT COUNT(*) FROM processed "
            "WHERE created_at LIKE ? AND status NOT IN ('not_invoice', 'skipped')",
            (today + "%",),
        ).fetchone()
        return row[0] if row else 0

    def stats(self) -> dict:
        """Retourne le décompte par statut (pour les logs de fin de cycle)."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM processed GROUP BY status"
        ).fetchall()
        return {row[0]: row[1] for row in rows}
