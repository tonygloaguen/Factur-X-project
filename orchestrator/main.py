#!/usr/bin/env python3
"""
main.py — Point d'entrée : boucle de polling Gmail
====================================================

Ce fichier est intentionnellement court et simple.
Il initialise les services, compile le graphe, et lance la boucle infinie.

Séparation des responsabilités :
  main.py     → orchestration de haut niveau (boucle, config, init)
  graph.py    → topologie du graphe LangGraph (nœuds + arêtes)
  nodes.py    → logique de chaque nœud (9 nœuds + 2 routeurs)
  facturx.py  → fonctions métier pures (OCR, Gemini, XML, PDF)
  services.py → services Google + SQLite (indépendants de LangGraph)
  state.py    → définition de l'état partagé (TypedDict)

Workflow LangGraph — Vision globale :
  Pour chaque email avec PJ PDF :
    workflow.invoke(état_initial) → LangGraph exécute les 9 nœuds dans l'ordre
"""

import base64
import logging
import os
import re
import sys
import time
from pathlib import Path

from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError

from graph import build_graph
from services import get_google_credentials, GoogleServices, StateDB
from state import InvoiceState
from supplier_registry import SupplierRegistry
from facturx_utils import is_pdf_attachment

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,  # Réinitialise la configuration de logging (utile si rechargé dans un notebook
)
logger = logging.getLogger("orchestrator")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (variables d'environnement)
# ─────────────────────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "900"))
GMAIL_LABEL_NAME = os.environ.get("GMAIL_LABEL", "Factures-Traitées")
STATE_DB_PATH = os.environ.get("STATE_DB_PATH", "/app/data/state.db")
TOKEN_FILE = Path("/app/token.json")
HEARTBEAT_FILE = Path("/tmp/heartbeat")  # Lu par le HEALTHCHECK Docker

MAX_EMAILS_PER_CYCLE = int(os.environ.get("MAX_EMAILS_PER_CYCLE", "3"))
MIN_SECONDS_BETWEEN_CALLS = float(os.environ.get("MIN_SECONDS_BETWEEN_CALLS", "15"))
MAX_GEMINI_REQUESTS_PER_DAY = int(os.environ.get("MAX_GEMINI_REQUESTS_PER_DAY", "18"))
GMAIL_MAX_RESULTS = int(os.environ.get("GMAIL_MAX_RESULTS", "10"))

# Fenêtre de collecte Gmail (jours). Trop courte (7 j), toute facture arrivée
# avant J-7 au premier passage devenait invisible POUR TOUJOURS (BAUS-2026-05,
# les 3 RE…MARTINEAU/Häcker). Défaut élargi à 30 j, paramétrable.
POLL_WINDOW_DAYS = int(os.environ.get("POLL_WINDOW_DAYS", "30"))
# Mode rattrapage : GMAIL_CATCHUP=true retire toute borne temporelle (remonte
# tout l'historique) pour réinjecter des factures anciennes perdues.
GMAIL_CATCHUP = os.environ.get("GMAIL_CATCHUP", "false").lower() == "true"


def _ensure_window(q: str) -> str:
    """Applique la fenêtre temporelle de collecte à la requête Gmail.

    - GMAIL_CATCHUP=true : aucune borne (rattrapage complet de l'historique).
    - sinon : garantit un ``newer_than:{POLL_WINDOW_DAYS}d`` s'il est absent.
    - GMAIL_ENFORCE_WINDOW=false (ou l'ancien GMAIL_ENFORCE_7D=false) : désactive
      totalement l'ajout de borne (l'appelant maîtrise sa requête).
    """
    q = (q or "").strip()
    if GMAIL_CATCHUP:
        # Retirer une éventuelle borne pour remonter tout l'historique.
        return re.sub(r"\s*newer_than:\S+", "", q).strip()
    enforce = os.environ.get(
        "GMAIL_ENFORCE_WINDOW", os.environ.get("GMAIL_ENFORCE_7D", "true")
    ).lower() == "true"
    if not enforce:
        return q
    return q if "newer_than:" in q else f"{q} newer_than:{POLL_WINDOW_DAYS}d".strip()

GMAIL_QUERY = _ensure_window(
    os.environ.get(
        "GMAIL_QUERY",
        "has:attachment filename:pdf -label:Factures-Traitées",
    )
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers : extraction corps + PJ depuis Gmail API
# ─────────────────────────────────────────────────────────────────────────────

def _extract_body(payload: dict) -> str:
    """Extrait récursivement le texte brut du message Gmail."""
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
    """Retourne la liste des (nom_fichier, contenu_bytes) pour les PJ PDF.

    Gère trois cas :
      1. Attachement classique : body.attachmentId → récupéré via l'API Gmail
      2. Données inline : body.data sans attachmentId (PDFs embarqués directement)
      3. Parties imbriquées : multipart/* ou message/rfc822 (emails transférés)
    """
    attachments = []
    for part in payload.get("parts", []):
        filename = part.get("filename", "")
        mime_type = part.get("mimeType", "")

        if is_pdf_attachment(part):
            body = part.get("body", {})
            att_id = body.get("attachmentId")
            if att_id:
                # Cas 1 : attachement classique (> ~2 Ko — récupéré via l'API)
                att = (
                    services.gmail.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=att_id)
                    .execute()
                )
                data = base64.urlsafe_b64decode(att["data"])
                attachments.append((filename or "facture.pdf", data))
            elif body.get("data"):
                # Cas 2 : données inline (PDF embarqué directement dans le payload)
                # Certains clients/relais email intègrent les PDFs directement
                # sans passer par le mécanisme d'attachementId Gmail.
                data = base64.urlsafe_b64decode(body["data"])
                attachments.append((filename or "facture.pdf", data))

        if part.get("parts"):
            # Cas 3 : récursion dans les parties imbriquées
            # (multipart/mixed, multipart/alternative, message/rfc822…)
            attachments.extend(_find_pdf_attachments(part, message_id, services))

    return attachments


# ─────────────────────────────────────────────────────────────────────────────
# Boucle de polling Gmail
# ─────────────────────────────────────────────────────────────────────────────

def poll_gmail(services: GoogleServices, workflow, state_db: StateDB):
    """
    Un cycle de polling :
      1. Vérifie le quota Gemini journalier
      2. Liste les emails Gmail selon GMAIL_QUERY
      3. Pour chaque email avec PJ PDF non déjà traité :
           → Construit l'état initial
           → Invoque le workflow LangGraph : workflow.invoke(état_initial)
           → Pause entre chaque PDF (throttling Gemini)

    CONCEPT LANGGRAPH : workflow.invoke()
    --------------------------------------
    workflow.invoke(état_initial) exécute le graphe en entier de façon SYNCHRONE.
    LangGraph :
      1. Démarre au nœud d'entrée (extract_text)
      2. Appelle chaque nœud avec l'état courant
      3. Merge le dict retourné dans l'état
      4. Suit les arêtes (directes ou conditionnelles) jusqu'à END
      5. Retourne l'état final

    L'état final n'est pas utilisé ici car log_result a déjà tout persisté dans SQLite.
    """
    try:
        # Vérifier le quota Gemini journalier AVANT de commencer
        calls_today = state_db.gemini_calls_today()
        if calls_today >= MAX_GEMINI_REQUESTS_PER_DAY:
            logger.warning(
                "Quota Gemini journalier atteint (%d/%d). Prochain cycle dans %ds.",
                calls_today, MAX_GEMINI_REQUESTS_PER_DAY, POLL_INTERVAL,
            )
            return

        # Lister les emails correspondant à la requête Gmail
        results = (
            services.gmail.users()
            .messages()
            .list(userId="me", q=GMAIL_QUERY, maxResults=GMAIL_MAX_RESULTS)
            .execute()
        )
        messages = results.get("messages", [])

        if not messages:
            logger.info("Aucun nouvel email avec facture détecté")
            return

        logger.info("%d email(s) trouvé(s) par Gmail", len(messages))
        processed_count = 0
        # Gmail retourne les emails newest-first. Le 1er email rencontré pour un
        # filename est donc le plus récent → c'est le seul à traiter dans ce cycle.
        # Les emails plus anciens avec le même filename sont ignorés ici.
        # mark_superseded reste en place comme filet de sécurité secondaire.
        cycle_filenames: set[str] = set()

        for msg_info in messages:
            # Limite par cycle (évite de tout traiter d'un coup)
            if processed_count >= MAX_EMAILS_PER_CYCLE:
                logger.info(
                    "Limite par cycle atteinte (%d/%d). Suite au prochain cycle.",
                    processed_count, MAX_EMAILS_PER_CYCLE,
                )
                break

            # Re-vérifier le quota (peut avoir changé pendant le cycle)
            if state_db.gemini_calls_today() >= MAX_GEMINI_REQUESTS_PER_DAY:
                logger.warning("Quota Gemini journalier atteint pendant le cycle.")
                break

            msg_id = msg_info["id"]

            try:
                # Récupérer l'email complet
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
                    logger.info("Email '%s' : pas de PJ PDF, ignoré", subject[:50])
                    continue

                for att_filename, att_bytes in attachments:
                    # ── Guard intra-cycle ──────────────────────────────────────
                    if att_filename in cycle_filenames:
                        logger.info(
                            "⏭️  Doublon intra-cycle '%s' — email plus ancien ignoré",
                            att_filename,
                        )
                        continue
                    cycle_filenames.add(att_filename)

                    # ── Étape 1 : skip si (message_id, filename) exactement déjà traité ──
                    # Protège contre le retraitement du même email lors d'un polling ultérieur
                    # (cas normal : l'email n'a pas encore été labellisé).
                    if state_db.is_seen(msg_id, att_filename):
                        logger.info(
                            "⏭️  Déjà traité (même email+fichier) : '%s' / %s — skip",
                            subject[:50], att_filename,
                        )
                        continue

                    # ── Étape 2 : règle "dernière occurrence fait foi" ─────────
                    # Un même nom de fichier peut arriver dans plusieurs emails distincts
                    # (ex : fournisseur qui renvoi la facture corrigée).
                    # Règle : la dernière occurrence reçue fait foi → on retraite.
                    # Exception : si l'occurrence précédente était not_invoice,
                    # le même PDF reste non-facture (décision stable).
                    prior = state_db.get_latest_by_filename(att_filename)
                    if prior and prior["status"] == "not_invoice":
                        logger.info(
                            "⏭️  Fichier '%s' déjà classé not_invoice (msg: %s…) — skip",
                            att_filename, prior["message_id"][:12],
                        )
                        continue

                    prior_msg_id = prior["message_id"] if prior else ""
                    if prior_msg_id:
                        logger.info(
                            "↩️  Nouvelle occurrence de '%s' "
                            "(précédent: statut=%s, msg=%s…) — retraitement",
                            att_filename, prior["status"], prior_msg_id[:12],
                        )

                    logger.info("━" * 60)
                    logger.info("Nouvel email : '%s' de %s", subject[:60], sender[:50])
                    logger.info("Pièce jointe : %s (%d Ko)", att_filename, len(att_bytes) // 1024)

                    # ── Construire l'état initial ──────────────────────────────
                    # Tous les champs optionnels ont des valeurs par défaut.
                    # Les services sont des singletons partagés (injection de dépendances).
                    initial_state: InvoiceState = {
                        # Entrée email
                        "message_id": msg_id,
                        "subject": subject,
                        "sender": sender,
                        "body": body,
                        "pdf_bytes": att_bytes,
                        "pdf_filename": att_filename,
                        # Champs produits par les nœuds (vides au départ)
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
                        # Déduplication par nom de fichier (batch 1)
                        "prior_message_id": prior_msg_id,
                        # Client final pour le classement Drive (batch 2)
                        "client_final": "",
                        # Services (singletons injectés)
                        "services": services,
                        "state_db": state_db,
                        "registry": registry,
                    }

                    # ── Invoquer le graphe LangGraph ───────────────────────────
                    # LangGraph exécute les 9 nœuds dans l'ordre défini dans graph.py.
                    # Chaque nœud lit l'état courant et retourne un dict partiel.
                    # LangGraph merge ce dict avant d'appeler le nœud suivant.
                    workflow.invoke(initial_state)
                    processed_count += 1

                    # Pause entre chaque PDF (throttling Gemini : max 1 appel/15s)
                    time.sleep(MIN_SECONDS_BETWEEN_CALLS)

            except HttpError as e:
                if e.resp.status == 429:
                    logger.warning(
                        "Rate limit API Gmail (429) — cycle interrompu, reprise dans %ds",
                        POLL_INTERVAL,
                    )
                    break
                logger.error("Erreur traitement email %s : %s", msg_id, e)
            except Exception as e:
                logger.error("Erreur traitement email %s : %s", msg_id, e)

        # Bilan de fin de cycle
        stats = state_db.stats()
        logger.info(
            "Bilan cycle : %s | Gemini aujourd'hui : %d/%d",
            " | ".join(f"{k}={v}" for k, v in sorted(stats.items())),
            state_db.gemini_calls_today(),
            MAX_GEMINI_REQUESTS_PER_DAY,
        )

    except Exception as e:
        logger.error("Erreur polling Gmail : %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 60)
    logger.info("Orchestrateur LangGraph Factur-X — Pure Python")
    logger.info("=" * 60)
    logger.info("Architecture : 1 graphe LangGraph, 9 nœuds, 0 microservice HTTP")
    logger.info("Polling Gmail : toutes les %ds (%d min)", POLL_INTERVAL, POLL_INTERVAL // 60)
    logger.info("Quota Gemini : max %d/jour, %ds entre appels, %d/cycle",
                MAX_GEMINI_REQUESTS_PER_DAY, int(MIN_SECONDS_BETWEEN_CALLS), MAX_EMAILS_PER_CYCLE)
    logger.info("Requête Gmail : %s", GMAIL_QUERY)
    logger.info("State DB : %s", STATE_DB_PATH)
    logger.info("=" * 60)

    # Ouvrir la base SQLite
    state_db = StateDB(STATE_DB_PATH)
    stats = state_db.stats()
    if stats:
        logger.info("Historique existant : %s", " | ".join(f"{k}={v}" for k, v in sorted(stats.items())))

    # Charger le registre fournisseurs (identité émetteur + classement Drive).
    registry = SupplierRegistry.load()

    # Authentification Google OAuth2 (avec retry illimité : une coupure réseau
    # prolongée, ex. Wi-Fi coupé une partie de la nuit, ne doit pas tuer le
    # processus — il doit patienter et reprendre seul dès que ça revient).
    logger.info("Connexion à Google...")
    attempt = 0
    while True:
        attempt += 1
        try:
            creds = get_google_credentials()
            services = GoogleServices(creds)
            break
        except Exception as e:
            sleep_s = min(600, 10 * 2 ** (attempt - 1))  # backoff exponentiel, plafonné à 10 min
            logger.warning(
                "Connexion Google échouée (tentative %d) : %s — nouvelle tentative dans %ds...",
                attempt, e, sleep_s,
            )
            time.sleep(sleep_s)
    logger.info("Connexion Google : OK (après %d tentative(s))", attempt)

    # Compiler le graphe LangGraph
    # build_graph() construit le StateGraph et le compile en CompiledStateGraph
    workflow = build_graph()
    logger.info("Graphe LangGraph compilé : 9 nœuds, 2 arêtes conditionnelles")

    # Boucle de polling infinie
    logger.info("Démarrage de la boucle de polling...")
    while True:
        try:
            # Rafraîchir le token Google si expiré
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                TOKEN_FILE.write_text(creds.to_json())

            poll_gmail(services, workflow, state_db)

        except Exception as e:
            logger.error("Erreur dans la boucle principale : %s", e)

        finally:
            # Mise à jour du heartbeat après chaque cycle (succès ou erreur transitoire)
            # Le HEALTHCHECK Docker vérifie que ce fichier a moins de 30 min
            HEARTBEAT_FILE.write_text(str(time.time()))

        logger.info("Prochaine vérification dans %d secondes...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
