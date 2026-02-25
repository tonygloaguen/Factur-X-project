#!/usr/bin/env python3
"""
Orchestrateur LangGraph — Workflow factures fournisseurs
========================================================
Remplace Automatisch.io par un graphe Python pur.

Workflow :
  poll_gmail → process_invoice → upload_drive → label_gmail → log_result

Anti-retraitement via SQLite : chaque email+PJ vu est enregistré.
Throttling Gemini : quota journalier + pause entre les appels.

Dépendances : voir requirements.txt
Licence : MIT
"""

import base64
import io
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from langgraph.graph import END, StateGraph

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("orchestrator")

# URL du micro-service Factur-X (réseau Docker interne)
FACTURX_URL = os.environ.get("FACTURX_SERVICE_URL", "http://facturx-service:5000")

# Intervalle de polling en secondes (défaut : 15 minutes)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))

# ID du dossier racine Google Drive
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")

# Label Gmail pour marquer les emails traités
GMAIL_LABEL_NAME = os.environ.get("GMAIL_LABEL", "Factures-Traitées")

# Requête Gmail pour trouver les emails avec factures
def _ensure_7d(q: str) -> str:
    q = (q or "").strip()
    if "newer_than:" in q:
        return q
    return (q + " newer_than:7d").strip()

GMAIL_QUERY = _ensure_7d(
    os.environ.get(
        "GMAIL_QUERY",
        "has:attachment filename:pdf -label:Factures-Traitées",
    )
)

# SQLite state DB
STATE_DB_PATH = os.environ.get("STATE_DB_PATH", "/app/data/state.db")

# Throttling Gemini
MAX_EMAILS_PER_CYCLE = int(os.environ.get("MAX_EMAILS_PER_CYCLE", "3"))
MIN_SECONDS_BETWEEN_CALLS = float(os.environ.get("MIN_SECONDS_BETWEEN_CALLS", "15"))
MAX_GEMINI_REQUESTS_PER_DAY = int(os.environ.get("MAX_GEMINI_REQUESTS_PER_DAY", "18"))

# Scopes Google (Gmail lecture/écriture + Drive fichiers)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]

# Chemins des fichiers d'authentification Google
CREDENTIALS_FILE = Path("/app/credentials.json")
TOKEN_FILE = Path("/app/token.json")

# Mois en français pour les noms de dossiers
MOIS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}


# ---------------------------------------------------------------------------
# SQLite : base de suivi anti-retraitement
# ---------------------------------------------------------------------------
class StateDB:
    """
    Base SQLite qui enregistre chaque email+PJ traité.

    Table `processed` :
      - message_id  : ID Gmail du message
      - filename    : nom du fichier PDF
      - status      : 'success', 'not_invoice', 'error_gemini', 'error_drive', etc.
      - detail      : info supplémentaire (nom fournisseur, erreur...)
      - drive_url   : lien Drive si upload réussi
      - created_at  : date/heure du traitement

    Cela évite de :
      - Rappeler Gemini sur un PDF déjà analysé (économie de quota)
      - Re-uploader un PDF déjà dans Drive
      - Re-scanner les emails non-factures à chaque cycle
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
        self._conn.commit()
        logger.info("StateDB ouverte : %s", db_path)

    def is_seen(self, message_id: str, filename: str) -> bool:
        """Vérifie si ce message+fichier a déjà été traité."""
        row = self._conn.execute(
            "SELECT 1 FROM processed WHERE message_id = ? AND filename = ?",
            (message_id, filename),
        ).fetchone()
        return row is not None

    def mark(self, message_id: str, filename: str, status: str,
             detail: str = "", drive_url: str = ""):
        """Enregistre un traitement."""
        self._conn.execute(
            """INSERT OR REPLACE INTO processed
               (message_id, filename, status, detail, drive_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (message_id, filename, status, detail, drive_url,
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def gemini_calls_today(self) -> int:
        """Compte le nombre d'appels Gemini aujourd'hui (status != 'skipped')."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT COUNT(*) FROM processed WHERE created_at LIKE ? AND status != 'skipped'",
            (today + "%",),
        ).fetchone()
        return row[0] if row else 0

    def stats(self) -> dict:
        """Retourne des statistiques globales."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM processed GROUP BY status"
        ).fetchall()
        return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# Authentification Google OAuth2
# ---------------------------------------------------------------------------
def get_google_credentials() -> Credentials:
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
                    "  1. Va sur https://console.cloud.google.com/\n"
                    "  2. APIs et services → Identifiants\n"
                    "  3. Télécharge le JSON de ton ID client OAuth\n"
                    "  4. Place-le dans orchestrator/credentials.json"
                )
                sys.exit(1)

            logger.info(
                "=== PREMIÈRE AUTORISATION GOOGLE ===\n"
                "Une URL va s'afficher ci-dessous.\n"
                "Copie-la et ouvre-la dans ton navigateur.\n"
                "Connecte-toi avec ton compte Google et autorise l'accès.\n"
                "===================================="
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=False)

        TOKEN_FILE.write_text(creds.to_json())
        logger.info("Token Google sauvegardé dans %s", TOKEN_FILE)

    return creds


# ---------------------------------------------------------------------------
# Services Google (Gmail + Drive)
# ---------------------------------------------------------------------------
class GoogleServices:
    """Encapsule les connexions Gmail et Drive."""

    __slots__ = ("gmail", "drive", "_label_id")

    def __init__(self, creds: Credentials):
        self.gmail = build("gmail", "v1", credentials=creds)
        self.drive = build("drive", "v3", credentials=creds)
        self._label_id: str | None = None

    def get_or_create_label(self, label_name: str) -> str:
        if self._label_id:
            return self._label_id

        results = self.gmail.users().labels().list(userId="me").execute()
        for label in results.get("labels", []):
            if label["name"] == label_name:
                self._label_id = label["id"]
                return self._label_id

        body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = self.gmail.users().labels().create(userId="me", body=body).execute()
        self._label_id = created["id"]
        logger.info("Label Gmail créé : %s (ID: %s)", label_name, self._label_id)
        return self._label_id


# ---------------------------------------------------------------------------
# État du workflow LangGraph
# ---------------------------------------------------------------------------
class InvoiceState(TypedDict):
    message_id: str
    subject: str
    sender: str
    body: str
    pdf_bytes: bytes
    pdf_filename: str

    facturx_pdf: bytes
    invoice_filename: str
    invoice_folder: str
    invoice_data: dict
    processing_error: str

    drive_file_id: str
    drive_file_url: str

    services: GoogleServices
    state_db: StateDB


# ---------------------------------------------------------------------------
# Nœud 1 : Traitement de la facture via le micro-service
# ---------------------------------------------------------------------------
def process_invoice(state: InvoiceState) -> dict:
    logger.info(
        "Traitement facture : '%s' de %s", state["subject"], state["sender"]
    )

    try:
        resp = requests.post(
            f"{FACTURX_URL}/api/process-invoice",
            files={"pdf": (state["pdf_filename"], state["pdf_bytes"], "application/pdf")},
            data={
                "email_subject": state["subject"],
                "email_from": state["sender"],
                "email_body": state["body"][:1000],
            },
            timeout=120,
        )

        if resp.status_code == 422:
            error_data = resp.json()
            reason = error_data.get("error", "Pas une facture")
            gemini_used = resp.headers.get("X-Gemini-Used", "0")
            logger.info("Document ignoré : %s (Gemini=%s)", reason, gemini_used)

            # Enregistrer dans SQLite
            db: StateDB = state["state_db"]
            status = "not_invoice" if gemini_used == "0" else "not_invoice_gemini"
            db.mark(state["message_id"], state["pdf_filename"], status, detail=reason)

            return {"processing_error": reason}

        if resp.status_code == 429:
            logger.warning("Gemini rate limit (429) — skip pour ce cycle")
            # NE PAS enregistrer dans SQLite → sera retenté au prochain cycle
            return {"processing_error": "rate_limit_429"}

        resp.raise_for_status()

        invoice_filename = resp.headers.get("X-Invoice-Filename", "facture.pdf")
        invoice_folder = resp.headers.get("X-Invoice-Folder", "")
        invoice_data_raw = resp.headers.get("X-Invoice-Data", "")

        try:
            # Le micro-service encode le JSON en base64 (évite les problèmes Gunicorn)
            if invoice_data_raw:
                invoice_data = json.loads(
                    base64.b64decode(invoice_data_raw).decode("utf-8")
                )
            else:
                invoice_data = {}
        except Exception:
            invoice_data = {}

        logger.info(
            "Factur-X EN16931 générée : %s → dossier %s", invoice_filename, invoice_folder
        )

        return {
            "facturx_pdf": resp.content,
            "invoice_filename": invoice_filename,
            "invoice_folder": invoice_folder,
            "invoice_data": invoice_data,
            "processing_error": "",
        }

    except requests.exceptions.RequestException as e:
        logger.error("Erreur appel micro-service : %s", e)
        return {"processing_error": f"Erreur micro-service : {e}"}


# ---------------------------------------------------------------------------
# Nœud 2 : Upload sur Google Drive
# ---------------------------------------------------------------------------
def upload_drive(state: InvoiceState) -> dict:
    if state.get("processing_error"):
        return {}

    services: GoogleServices = state["services"]
    folder_name = state["invoice_folder"]
    filename = state["invoice_filename"]

    if not DRIVE_FOLDER_ID:
        logger.error("DRIVE_FOLDER_ID non configuré dans .env !")
        return {"processing_error": "DRIVE_FOLDER_ID manquant"}

    try:
        query = (
            f"name = '{folder_name}' "
            f"and '{DRIVE_FOLDER_ID}' in parents "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        results = (
            services.drive.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )
        folders = results.get("files", [])

        if folders:
            subfolder_id = folders[0]["id"]
            logger.info("Dossier existant trouvé : %s", folder_name)
        else:
            folder_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [DRIVE_FOLDER_ID],
            }
            folder = (
                services.drive.files()
                .create(body=folder_metadata, fields="id")
                .execute()
            )
            subfolder_id = folder["id"]
            logger.info("Dossier créé : %s (ID: %s)", folder_name, subfolder_id)

        file_metadata = {
            "name": filename,
            "parents": [subfolder_id],
        }
        media = MediaIoBaseUpload(
            io.BytesIO(state["facturx_pdf"]),
            mimetype="application/pdf",
            resumable=True,
        )
        uploaded = (
            services.drive.files()
            .create(body=file_metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )

        file_id = uploaded["id"]
        file_url = uploaded.get("webViewLink", "")
        logger.info("Fichier uploadé : %s → %s", filename, file_url)

        return {"drive_file_id": file_id, "drive_file_url": file_url}

    except Exception as e:
        logger.error("Erreur upload Drive : %s", e)
        return {"processing_error": f"Erreur Drive : {e}"}


# ---------------------------------------------------------------------------
# Nœud 3 : Labelliser l'email Gmail
# ---------------------------------------------------------------------------
def label_gmail(state: InvoiceState) -> dict:
    if state.get("processing_error"):
        return {}

    services: GoogleServices = state["services"]

    try:
        label_id = services.get_or_create_label(GMAIL_LABEL_NAME)

        services.gmail.users().messages().modify(
            userId="me",
            id=state["message_id"],
            body={"addLabelIds": [label_id]},
        ).execute()

        logger.info("Label '%s' ajouté à l'email %s", GMAIL_LABEL_NAME, state["message_id"])

    except Exception as e:
        logger.error("Erreur label Gmail : %s", e)

    return {}


# ---------------------------------------------------------------------------
# Nœud 4 : Log du résultat + enregistrement SQLite
# ---------------------------------------------------------------------------
def log_result(state: InvoiceState) -> dict:
    db: StateDB = state["state_db"]

    if state.get("processing_error"):
        error = state["processing_error"]
        logger.warning(
            "❌ Échec : [%s] %s — %s",
            state.get("sender", "?"),
            state.get("subject", "?"),
            error,
        )
        # Rate limit → ne pas enregistrer (sera retenté)
        # not_invoice → déjà enregistré dans process_invoice
        if "rate_limit" not in error and not error.startswith("Document non-facture"):
            db.mark(
                state["message_id"], state["pdf_filename"],
                "error", detail=error[:200],
            )
    else:
        inv = state.get("invoice_data", {})
        vendor = inv.get("vendeur", {}).get("nom_court", "?")
        numero = inv.get("numero_facture", "?")
        ttc = inv.get("montant_ttc", "?")
        url = state.get("drive_file_url", "?")
        logger.info("✅ Succès : %s | %s | %s € TTC → %s", vendor, numero, ttc, url)

        db.mark(
            state["message_id"], state["pdf_filename"],
            "success",
            detail=f"{vendor} | {numero} | {ttc}€",
            drive_url=url,
        )

    return {}


# ---------------------------------------------------------------------------
# Routage conditionnel
# ---------------------------------------------------------------------------
def should_continue(state: InvoiceState) -> str:
    if state.get("processing_error"):
        return "log_result"
    return "upload_drive"


# ---------------------------------------------------------------------------
# Construction du graphe LangGraph
# ---------------------------------------------------------------------------
def build_graph() -> StateGraph:
    graph = StateGraph(InvoiceState)

    graph.add_node("process_invoice", process_invoice)
    graph.add_node("upload_drive", upload_drive)
    graph.add_node("label_gmail", label_gmail)
    graph.add_node("log_result", log_result)

    graph.set_entry_point("process_invoice")

    graph.add_conditional_edges(
        "process_invoice",
        should_continue,
        {"upload_drive": "upload_drive", "log_result": "log_result"},
    )

    graph.add_edge("upload_drive", "label_gmail")
    graph.add_edge("label_gmail", "log_result")
    graph.add_edge("log_result", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Polling Gmail avec anti-retraitement SQLite + throttling Gemini
# ---------------------------------------------------------------------------
def poll_gmail(services: GoogleServices, workflow, state_db: StateDB):
    try:
        # Vérifier le quota Gemini journalier
        calls_today = state_db.gemini_calls_today()
        if calls_today >= MAX_GEMINI_REQUESTS_PER_DAY:
            logger.warning(
                "Quota Gemini journalier atteint (%d/%d). Prochain cycle dans %ds.",
                calls_today, MAX_GEMINI_REQUESTS_PER_DAY, POLL_INTERVAL,
            )
            return

        results = (
            services.gmail.users()
            .messages()
            .list(userId="me", q=GMAIL_QUERY, maxResults=10)
            .execute()
        )
        messages = results.get("messages", [])

        if not messages:
            logger.info("Aucun nouvel email avec facture détecté")
            return

        logger.info("%d email(s) trouvé(s) par Gmail", len(messages))

        processed_count = 0

        for msg_info in messages:
            # Limite par cycle
            if processed_count >= MAX_EMAILS_PER_CYCLE:
                logger.info(
                    "Limite par cycle atteinte (%d/%d). Suite au prochain cycle.",
                    processed_count, MAX_EMAILS_PER_CYCLE,
                )
                break

            # Re-vérifier le quota Gemini
            if state_db.gemini_calls_today() >= MAX_GEMINI_REQUESTS_PER_DAY:
                logger.warning("Quota Gemini journalier atteint pendant le cycle.")
                break

            msg_id = msg_info["id"]

            try:
                msg = (
                    services.gmail.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )

                headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
                subject = headers.get("Subject", "(sans objet)")
                sender = headers.get("From", "")

                body = _extract_body(msg["payload"])

                attachments = _find_pdf_attachments(msg["payload"], msg_id, services)

                if not attachments:
                    logger.info("Email '%s' : pas de PJ PDF, ignoré", subject)
                    continue

                for att_filename, att_bytes in attachments:
                    # ====== ANTI-RETRAITEMENT SQLite ======
                    if state_db.is_seen(msg_id, att_filename):
                        logger.info(
                            "⏭️  Déjà traité : '%s' / %s — skip",
                            subject[:50], att_filename,
                        )
                        continue

                    logger.info("━" * 60)
                    logger.info("Nouvel email : '%s' de %s", subject, sender)
                    logger.info("Pièce jointe : %s (%d Ko)", att_filename, len(att_bytes) // 1024)

                    initial_state: InvoiceState = {
                        "message_id": msg_id,
                        "subject": subject,
                        "sender": sender,
                        "body": body,
                        "pdf_bytes": att_bytes,
                        "pdf_filename": att_filename,
                        "facturx_pdf": b"",
                        "invoice_filename": "",
                        "invoice_folder": "",
                        "invoice_data": {},
                        "processing_error": "",
                        "drive_file_id": "",
                        "drive_file_url": "",
                        "services": services,
                        "state_db": state_db,
                    }

                    workflow.invoke(initial_state)
                    processed_count += 1

                    # Pause entre chaque PDF (throttling Gemini)
                    time.sleep(MIN_SECONDS_BETWEEN_CALLS)

            except Exception as e:
                logger.error("Erreur traitement email %s : %s", msg_id, e)

        # Log des stats en fin de cycle
        stats = state_db.stats()
        logger.info(
            "Stats DB : %s | Gemini aujourd'hui : %d/%d",
            " | ".join(f"{k}={v}" for k, v in sorted(stats.items())),
            state_db.gemini_calls_today(),
            MAX_GEMINI_REQUESTS_PER_DAY,
        )

    except Exception as e:
        logger.error("Erreur polling Gmail : %s", e)


# ---------------------------------------------------------------------------
# Helpers : extraction du corps et des pièces jointes
# ---------------------------------------------------------------------------
def _extract_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    return ""


def _find_pdf_attachments(
    payload: dict, message_id: str, services: GoogleServices
) -> list[tuple[str, bytes]]:
    attachments = []

    for part in payload.get("parts", []):
        filename = part.get("filename", "")
        mime_type = part.get("mimeType", "")

        if filename.lower().endswith(".pdf") or mime_type == "application/pdf":
            att_id = part.get("body", {}).get("attachmentId")
            if att_id:
                att = (
                    services.gmail.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=att_id)
                    .execute()
                )
                data = base64.urlsafe_b64decode(att["data"])
                attachments.append((filename or "facture.pdf", data))

        if part.get("parts"):
            attachments.extend(
                _find_pdf_attachments(part, message_id, services)
            )

    return attachments


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------
def main():
    logger.info("=" * 60)
    logger.info("Orchestrateur LangGraph — Factures fournisseurs")
    logger.info("=" * 60)
    logger.info("Micro-service Factur-X : %s", FACTURX_URL)
    logger.info("Polling Gmail toutes les %d secondes (%d min)", POLL_INTERVAL, POLL_INTERVAL // 60)
    logger.info("Dossier Drive : %s", DRIVE_FOLDER_ID or "⚠️  NON CONFIGURÉ")
    logger.info("Label Gmail : %s", GMAIL_LABEL_NAME)
    logger.info("Requête Gmail : %s", GMAIL_QUERY)
    logger.info("Throttling : max %d emails/cycle, %ds entre appels, %d Gemini/jour",
                MAX_EMAILS_PER_CYCLE, int(MIN_SECONDS_BETWEEN_CALLS), MAX_GEMINI_REQUESTS_PER_DAY)
    logger.info("State DB : %s", STATE_DB_PATH)
    logger.info("=" * 60)

    # Vérifier que le micro-service est accessible
    try:
        resp = requests.get(f"{FACTURX_URL}/health", timeout=10)
        resp.raise_for_status()
        logger.info("Micro-service Factur-X : OK (%s)", resp.json())
    except Exception as e:
        logger.error("Micro-service Factur-X inaccessible : %s", e)
        logger.error("Vérifiez que facturx-service est démarré")
        sys.exit(1)

    # Ouvrir la base SQLite
    state_db = StateDB(STATE_DB_PATH)
    stats = state_db.stats()
    if stats:
        logger.info("Historique existant : %s", " | ".join(f"{k}={v}" for k, v in sorted(stats.items())))

    # Authentification Google
    logger.info("Connexion à Google...")
    creds = get_google_credentials()
    services = GoogleServices(creds)
    logger.info("Connexion Google OK")

    # Construire le graphe LangGraph
    workflow = build_graph()
    logger.info("Graphe LangGraph compilé (4 nœuds)")

    # Boucle de polling
    logger.info("Démarrage de la boucle de polling...")
    while True:
        try:
            if creds.expired:
                creds.refresh(Request())
                TOKEN_FILE.write_text(creds.to_json())

            poll_gmail(services, workflow, state_db)

        except Exception as e:
            logger.error("Erreur dans la boucle principale : %s", e)

        logger.info("Prochaine vérification dans %d secondes...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
