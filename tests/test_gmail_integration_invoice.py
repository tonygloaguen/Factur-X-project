# C:\Automatisch\tests\test_gmail_integration_invoice.py
#
# Dépendances :
#   pip install pytest google-auth google-auth-oauthlib google-api-python-client
#
# Exécution :
#   pytest -q -m integration -s
#
# Pré-requis :
#   - Fichier OAuth Desktop Google (Gmail API activée)
#     ex : C:\Automatisch\orchestrator\credentials.json
#   - OU variable d’environnement :
#       GMAIL_OAUTH_CLIENT_SECRET=C:\Automatisch\orchestrator\credentials.json
#
# Comportement :
#   - OAuth interactif uniquement au premier run
#   - Token persisté localement (pas de popup ensuite)
#   - Lit les 10 derniers mails Gmail
#   - Vérifie qu’au moins une facture PDF est détectée

import os
import base64
import re
from typing import List, Tuple

import pytest
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CLIENT_SECRET_FALLBACK = r"C:\Automatisch\orchestrator\credentials.json"
TOKEN_PATH = r"C:\Automatisch\orchestrator\gmail_token.json"

INVOICE_SUBJECT_RE = re.compile(r"\bfacture\b", re.IGNORECASE)
PDF_NAME_RE = re.compile(r"\.pdf$", re.IGNORECASE)
LIKELY_INVOICE_PDF_RE = re.compile(r"(facture|invoice|avoir)", re.IGNORECASE)

# ---------------------------------------------------------------------
# GMAIL AUTH
# ---------------------------------------------------------------------

def gmail_service(client_secret_path: str):
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)

# ---------------------------------------------------------------------
# MIME / ATTACHMENTS
# ---------------------------------------------------------------------

def _walk_parts(payload: dict):
    stack = [payload]
    while stack:
        part = stack.pop()
        yield part
        for sub in part.get("parts") or []:
            stack.append(sub)

def _fetch_attachment_bytes(service, msg_id: str, att_id: str) -> bytes:
    att = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=msg_id, id=att_id)
        .execute()
    )
    return base64.urlsafe_b64decode(att["data"])

def extract_pdf_attachments(service, msg: dict) -> List[Tuple[str, bytes]]:
    msg_id = msg["id"]
    payload = msg["payload"]

    found: List[Tuple[str, bytes]] = []

    for part in _walk_parts(payload):
        filename = (part.get("filename") or "").strip()
        mime_type = (part.get("mimeType") or "").strip()
        body = part.get("body") or {}
        att_id = body.get("attachmentId")

        if att_id and (mime_type == "application/pdf" or PDF_NAME_RE.search(filename)):
            data = _fetch_attachment_bytes(service, msg_id, att_id)
            found.append((filename or "facture.pdf", data))

    return found

# ---------------------------------------------------------------------
# HEADERS
# ---------------------------------------------------------------------

def get_header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []) or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""

# ---------------------------------------------------------------------
# TEST D’INTÉGRATION
# ---------------------------------------------------------------------

@pytest.mark.integration
def test_last_10_emails_contains_invoice_pdf():
    client_secret = os.environ.get(
        "GMAIL_OAUTH_CLIENT_SECRET",
        CLIENT_SECRET_FALLBACK,
    )

    assert os.path.exists(client_secret), (
        f"Fichier OAuth introuvable : {client_secret}\n"
        "Fournis credentials.json ou définis GMAIL_OAUTH_CLIENT_SECRET."
    )

    service = gmail_service(client_secret)

    resp = service.users().messages().list(
        userId="me",
        maxResults=10
        # Exemple de filtrage possible :
        # q="facture"
    ).execute()

    ids = [m["id"] for m in resp.get("messages", [])]
    assert ids, "Aucun mail trouvé dans Gmail."

    invoices_found = []

    for mid in ids:
        msg = service.users().messages().get(
            userId="me",
            id=mid,
            format="full"
        ).execute()

        subject = get_header(msg, "Subject")
        sender = get_header(msg, "From")

        pdfs = extract_pdf_attachments(service, msg)

        if pdfs and (
            INVOICE_SUBJECT_RE.search(subject)
            or any(LIKELY_INVOICE_PDF_RE.search(n) for n, _ in pdfs)
        ):
            invoices_found.append(
                {
                    "id": mid,
                    "subject": subject,
                    "from": sender,
                    "pdfs": [n for n, _ in pdfs],
                }
            )

    assert invoices_found, (
        "Aucune facture PDF détectée dans les 10 derniers mails."
    )

    print("Factures détectées :")
    for it in invoices_found:
        print(f"- {it['subject']} | {it['from']} | PDFs={it['pdfs']}")