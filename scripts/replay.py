#!/usr/bin/env python3
"""scripts/replay.py — Réinjection manuelle de factures (rattrapage P2).

Rejoue des emails précis dans le pipeline LangGraph Factur-X, hors de la boucle
de polling et de sa fenêtre temporelle. Sert à récupérer les factures perdues
par le trou de collecte ``newer_than`` (ex. BAUS-2026-05-000514, les 3 Häcker
« RE…MARTINEAU ») ou à retraiter un email en échec.

Usage :
    # Par identifiant(s) de message Gmail (répétable)
    python scripts/replay.py --message-id 19ed20efd606bd38 --message-id 18abcd...

    # Par date (tous les emails avec PJ PDF depuis une date, SANS borne 7/30 j)
    python scripts/replay.py --since 2026-05-01

    # Forcer le retraitement même si déjà marqué (error/success) en base
    python scripts/replay.py --since 2026-05-01 --force

    # Requête Gmail personnalisée (prioritaire sur --since)
    python scripts/replay.py --query "from:noreply@haecker-kuechen.de has:attachment"

Les identifiants de --message-id et les résultats de --since/--query sont
cumulés puis rejoués une seule fois chacun.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Rendre le paquet orchestrator importable (scripts/ est hors du package).
_ORCH = Path(__file__).resolve().parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCH))

from graph import build_graph  # noqa: E402
from main import _extract_body, _find_pdf_attachments  # noqa: E402  (helpers testés)
from services import GoogleServices, StateDB, get_google_credentials  # noqa: E402
from state import InvoiceState  # noqa: E402
from supplier_registry import SupplierRegistry  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger("orchestrator")

STATE_DB_PATH = os.environ.get("STATE_DB_PATH", "/app/data/state.db")


def _resolve_message_ids(
    services: GoogleServices, args: argparse.Namespace
) -> list[str]:
    """Construit la liste ordonnée et dédupliquée des message_id à rejouer."""
    ids: list[str] = list(args.message_id or [])

    query = None
    if args.query:
        query = args.query
    elif args.since:
        # Gmail attend une date au format YYYY/MM/DD pour `after:`. On ne met
        # AUCUNE borne haute ni filtre de label : rattrapage complet voulu.
        after = args.since.replace("-", "/")
        query = f"has:attachment filename:pdf after:{after}"

    if query:
        logger.info("Requête Gmail de rattrapage : %s", query)
        page_token = None
        while True:
            resp = (
                services.gmail.users()
                .messages()
                .list(userId="me", q=query, maxResults=100, pageToken=page_token)
                .execute()
            )
            for m in resp.get("messages", []):
                ids.append(m["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # Dédupliquer en conservant l'ordre.
    seen: set[str] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    return unique


def _replay_one(
    services: GoogleServices,
    workflow,
    state_db: StateDB,
    registry: SupplierRegistry,
    msg_id: str,
    *,
    force: bool,
) -> int:
    """Rejoue un email : retourne le nombre de PJ PDF traitées."""
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
        logger.info("Email '%s' (%s) : aucune PJ PDF — ignoré", subject[:50], msg_id)
        return 0

    processed = 0
    for att_filename, att_bytes in attachments:
        if not force and state_db.is_seen(msg_id, att_filename):
            logger.info(
                "⏭️  Déjà traité (%s / %s) — utiliser --force pour rejouer",
                subject[:40], att_filename,
            )
            continue

        prior = state_db.get_latest_by_filename(att_filename)
        prior_msg_id = prior["message_id"] if prior else ""

        logger.info("━" * 60)
        logger.info("Rejeu : '%s' de %s", subject[:60], sender[:50])
        logger.info("Pièce jointe : %s (%d Ko)", att_filename, len(att_bytes) // 1024)

        initial_state: InvoiceState = {
            "message_id": msg_id,
            "subject": subject,
            "sender": sender,
            "body": body,
            "pdf_bytes": att_bytes,
            "pdf_filename": att_filename,
            "ocr_text": "",
            "invoice_data": {},
            "gemini_used": False,
            "xml_bytes": b"",
            "facturx_pdf": b"",
            "invoice_filename": "",
            "invoice_folder": "",
            "drive_file_id": "",
            "drive_file_url": "",
            "processing_error": "",
            "prior_message_id": prior_msg_id,
            "client_final": "",
            "services": services,
            "state_db": state_db,
            "registry": registry,
        }
        workflow.invoke(initial_state)
        processed += 1

    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--message-id", action="append", metavar="ID",
        help="Identifiant de message Gmail à rejouer (répétable).",
    )
    parser.add_argument(
        "--since", metavar="YYYY-MM-DD",
        help="Rejouer tous les emails avec PJ PDF reçus depuis cette date (sans borne haute).",
    )
    parser.add_argument(
        "--query", metavar="GMAIL_QUERY",
        help="Requête Gmail brute (prioritaire sur --since).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Retraiter même si (message, fichier) est déjà marqué en base.",
    )
    args = parser.parse_args()

    if not (args.message_id or args.since or args.query):
        parser.error("Fournir au moins --message-id, --since ou --query.")

    logger.info("Connexion à Google...")
    creds = get_google_credentials()
    services = GoogleServices(creds)
    state_db = StateDB(STATE_DB_PATH)
    registry = SupplierRegistry.load()
    workflow = build_graph()
    logger.info("Connexion Google : OK — pipeline prêt")

    message_ids = _resolve_message_ids(services, args)
    if not message_ids:
        logger.warning("Aucun email à rejouer pour les critères fournis.")
        return 0

    logger.info("%d email(s) à rejouer%s", len(message_ids), " (--force)" if args.force else "")
    total = 0
    for msg_id in message_ids:
        try:
            total += _replay_one(services, workflow, state_db, registry, msg_id, force=args.force)
        except Exception as exc:  # noqa: BLE001 — on continue le rattrapage
            logger.error("Rejeu échoué pour %s : %s", msg_id, exc)

    logger.info("Rattrapage terminé : %d PJ PDF traitée(s) sur %d email(s)", total, len(message_ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
