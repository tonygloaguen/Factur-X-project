#!/usr/bin/env python3
"""scripts/reclasse_existant.py — Migration one-shot du classement GDrive.

Applique la nouvelle arborescence ``mois/contremarque/fournisseur`` aux PDF
Factur-X déjà présents sur Drive. **Dry-run par défaut** : sans ``--apply``, le
script ne fait que produire un plan ``ancien_chemin → nouveau_chemin`` (et
l'écrit dans ``RAPPORT_CLASSEMENT.md``). Avec ``--apply``, il crée les dossiers
manquants et **déplace** les fichiers (jamais de suppression).

L'identité de l'émetteur est relue depuis le XML Factur-X EMBARQUÉ dans chaque
PDF (TVA vendeur/acheteur) — la règle d'or ID fort ≠ self s'applique donc même
si l'ancien nom de dossier était erroné (piège Häcker).

Idempotence : un fichier déjà correctement rangé n'est pas touché.
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_ORCH = Path(__file__).resolve().parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCH))

from classify import Classification, classify  # noqa: E402
from supplier_registry import SupplierRegistry  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", force=True)
logger = logging.getLogger("orchestrator")


@dataclass
class Move:
    """Un déplacement planifié (ou un skip idempotent)."""

    file_id: str
    name: str
    old_path: str
    new_path: str
    new_name: str
    reason: str = ""

    @property
    def is_noop(self) -> bool:
        return self.old_path == self.new_path and self.name == self.new_name


# ─────────────────────────────────────────────────────────────────────────────
# Décision pure (testable sans Drive)
# ─────────────────────────────────────────────────────────────────────────────

def decide_move(file_id: str, current_name: str, current_folder: str,
                plan: Classification) -> Move:
    """Construit le :class:`Move` d'un fichier à partir de son plan de classement.

    Args:
        file_id: Identifiant Drive du fichier.
        current_name: Nom de fichier actuel.
        current_folder: Chemin de dossier actuel (relatif à la racine Drive).
        plan: Classement cible calculé par ``classify``.

    Returns:
        Un :class:`Move` (``is_noop`` True si le fichier est déjà bien rangé).
    """
    return Move(
        file_id=file_id, name=current_name, old_path=current_folder,
        new_path=plan.folder_path, new_name=plan.filename, reason=plan.route,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Extraction des données facture depuis le PDF Factur-X embarqué
# ─────────────────────────────────────────────────────────────────────────────

def inv_from_facturx_pdf(pdf_bytes: bytes) -> tuple[dict, str]:
    """Reconstruit un ``inv`` minimal depuis le XML Factur-X embarqué.

    Returns:
        ``(inv, xml_text)`` — ``inv`` alimente ``classify`` (vendeur/acheteur/
        date/numéro) et ``xml_text`` sert de source d'identifiants/contremarque
        (BuyerReference). ``({}, "")`` si le PDF n'embarque pas de XML.
    """
    import facturx
    from lxml import etree

    try:
        _, xml_bytes = facturx.get_xml_from_pdf(pdf_bytes, check_xsd=False, check_schematron=False)
    except Exception:  # noqa: BLE001
        xml_bytes = None
    if not xml_bytes:
        return {}, ""

    root = etree.fromstring(xml_bytes)
    ns = {
        "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
        "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
        "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
    }

    def _text(path: str) -> str:
        el = root.find(path, ns)
        return el.text.strip() if el is not None and el.text else ""

    def _party(tag: str) -> dict:
        base = f".//ram:{tag}"
        name = _text(base + "/ram:Name")
        vat = _text(base + "/ram:SpecifiedTaxRegistration/ram:ID")
        siret = _text(base + "/ram:SpecifiedLegalOrganization/ram:ID")
        return {"nom": name, "tva_intra": vat, "siret": siret}

    inv = {
        "vendeur": _party("SellerTradeParty"),
        "acheteur": _party("BuyerTradeParty"),
        "numero_facture": _text(".//rsm:ExchangedDocument/ram:ID"),
        "date_facture": _iso_date(_text(".//ram:IssueDateTime/udt:DateTimeString")),
        "type_facture": _text(".//rsm:ExchangedDocument/ram:TypeCode") or "380",
        "reference_commande": _text(".//ram:BuyerReference"),
    }
    return inv, xml_bytes.decode("utf-8", errors="replace")


def _iso_date(yyyymmdd: str) -> str:
    d = "".join(c for c in yyyymmdd if c.isdigit())
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else ""


# ─────────────────────────────────────────────────────────────────────────────
# Parcours Drive + application
# ─────────────────────────────────────────────────────────────────────────────

def _walk_pdfs(services, root_id: str) -> list[tuple[str, str, str, str]]:
    """Retourne (file_id, name, folder_path, parent_id) de chaque PDF sous root."""
    out: list[tuple[str, str, str, str]] = []

    def _recurse(folder_id: str, path: str) -> None:
        page_token = None
        while True:
            resp = services.drive.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces="drive", fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    _recurse(f["id"], f"{path}/{f['name']}" if path else f["name"])
                elif f["name"].lower().endswith(".pdf"):
                    out.append((f["id"], f["name"], path, folder_id))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    _recurse(root_id, "")
    return out


def _download(services, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    req = services.drive.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _ensure_folder(services, name: str, parent_id: str) -> str:
    q = (f"name = '{name}' and '{parent_id}' in parents "
         f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    files = services.drive.files().list(q=q, spaces="drive", fields="files(id)").execute().get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return services.drive.files().create(body=meta, fields="id").execute()["id"]


def compute_moves(services, registry: SupplierRegistry, root_id: str) -> list[Move]:
    """Calcule la liste des déplacements pour tous les PDF sous ``root_id``."""
    moves: list[Move] = []
    for file_id, name, folder_path, _parent in _walk_pdfs(services, root_id):
        try:
            inv, xml_text = inv_from_facturx_pdf(_download(services, file_id))
            if not inv:
                logger.warning("Sans XML Factur-X, ignoré : %s/%s", folder_path, name)
                continue
            plan = classify(inv, xml_text, registry)
            moves.append(decide_move(file_id, name, folder_path, plan))
        except Exception as exc:  # noqa: BLE001 — un échec ne bloque pas la migration
            logger.error("Analyse échouée (%s/%s) : %s", folder_path, name, exc)
    return moves


def apply_move(services, root_id: str, move: Move) -> None:
    """Crée l'arborescence cible et déplace le fichier (jamais de suppression)."""
    parent_id = root_id
    for part in move.new_path.split("/"):
        parent_id = _ensure_folder(services, part, parent_id)
    meta = services.drive.files().get(fileId=move.file_id, fields="parents").execute()
    old_parents = ",".join(meta.get("parents", []))
    services.drive.files().update(
        fileId=move.file_id, addParents=parent_id, removeParents=old_parents,
        body=({"name": move.new_name} if move.new_name != move.name else {}),
        fields="id, parents",
    ).execute()
    logger.info("Déplacé : %s/%s → %s/%s", move.old_path, move.name, move.new_path, move.new_name)


def write_report(moves: list[Move], registry: SupplierRegistry, path: Path) -> None:
    """Écrit RAPPORT_CLASSEMENT.md (plan, entités apprises, entrées to_review)."""
    to_move = [m for m in moves if not m.is_noop]
    noop = [m for m in moves if m.is_noop]
    learned = [c for c, e in registry.entities() if "auto_learned" in (e.get("flags") or [])]
    review = [c for c, e in registry.entities() if "to_review" in (e.get("flags") or [])]

    lines = ["# RAPPORT_CLASSEMENT — Migration du rangement GDrive", ""]
    lines.append(f"- Fichiers analysés : **{len(moves)}**")
    lines.append(f"- À déplacer : **{len(to_move)}**  |  déjà bien rangés : **{len(noop)}**")
    lines.append("")
    lines.append("## Plan de rangement (ancien → nouveau)")
    lines.append("")
    if to_move:
        lines.append("| Fichier | Ancien chemin | Nouveau chemin | Nouveau nom |")
        lines.append("|---------|---------------|----------------|-------------|")
        for m in to_move:
            lines.append(f"| {m.name} | `{m.old_path or '/'}` | `{m.new_path}` | {m.new_name} |")
    else:
        lines.append("_Aucun déplacement nécessaire._")
    lines.append("")
    lines.append("## Entités apprises automatiquement")
    lines.append("")
    lines.append(", ".join(learned) if learned else "_Aucune._")
    lines.append("")
    lines.append("## Entrées à revoir (`to_review`)")
    lines.append("")
    lines.append(", ".join(review) if review else "_Aucune._")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Rapport écrit : %s", path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Applique réellement les déplacements (défaut : dry-run).")
    parser.add_argument("--root-id", default=None,
                        help="ID du dossier Drive racine (défaut : env DRIVE_FOLDER_ID).")
    parser.add_argument("--report", default="RAPPORT_CLASSEMENT.md", help="Chemin du rapport.")
    args = parser.parse_args()

    import os
    root_id = args.root_id or os.environ.get("DRIVE_FOLDER_ID", "")
    if not root_id:
        parser.error("DRIVE_FOLDER_ID (ou --root-id) requis.")

    from services import GoogleServices, get_google_credentials

    services = GoogleServices(get_google_credentials())
    registry = SupplierRegistry.load()

    moves = compute_moves(services, registry, root_id)
    write_report(moves, registry, Path(args.report))

    to_move = [m for m in moves if not m.is_noop]
    if not args.apply:
        logger.info("DRY-RUN : %d déplacement(s) planifié(s). Relancer avec --apply pour exécuter.",
                    len(to_move))
        for m in to_move:
            logger.info("  %s/%s → %s/%s", m.old_path, m.name, m.new_path, m.new_name)
        return 0

    for m in to_move:
        try:
            apply_move(services, root_id, m)
        except Exception as exc:  # noqa: BLE001
            logger.error("Déplacement échoué (%s) : %s", m.name, exc)
    logger.info("Migration terminée : %d déplacement(s) appliqué(s).", len(to_move))
    return 0


if __name__ == "__main__":
    sys.exit(main())
