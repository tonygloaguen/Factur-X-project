#!/usr/bin/env python3
"""test_upload_classement.py — Câblage classement dans node_upload_drive.

Vérifie, avec un Drive simulé, que le nœud crée l'arborescence 3 niveaux
mois/contremarque/fournisseur, gère l'exception Communication, ignore les
doublons (idempotence) et retombe sur l'ancien schéma si le registre manque.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import nodes
from supplier_registry import SupplierRegistry

SEED = Path(__file__).resolve().parent.parent / "suppliers_registry.seed.json"


class FakeDrive:
    """Drive minimal en mémoire : dossiers imbriqués + fichiers, requêtes `name=`."""

    def __init__(self):
        self.nodes = {"ROOT": {"name": "ROOT", "parent": None, "is_folder": True}}
        self.counter = 0
        self.created_files = []

    # API fluent : files().list(...).execute() / files().create(...).execute()
    def files(self):
        return self

    def list(self, q=None, spaces=None, fields=None):
        self._q = q
        return self

    def create(self, body=None, media_body=None, fields=None):
        self._body = body
        self._media = media_body
        return self

    def execute(self):
        if hasattr(self, "_q") and self._q is not None:
            q, self._q = self._q, None
            name = q.split("name = '")[1].split("'")[0]
            parent = q.split("'")[3] if " in parents" in q else None
            want_folder = "!=" not in q  # 'mimeType != folder' → on cherche un fichier
            matches = [
                {"id": nid, "name": n["name"], "webViewLink": f"http://d/{nid}"}
                for nid, n in self.nodes.items()
                if n["name"] == name and n["parent"] == parent and n["is_folder"] == want_folder
            ]
            return {"files": matches}
        # create
        body, self._body = self._body, None
        self.counter += 1
        nid = f"id{self.counter}"
        is_folder = body.get("mimeType") == "application/vnd.google-apps.folder"
        self.nodes[nid] = {"name": body["name"], "parent": body["parents"][0], "is_folder": is_folder}
        if not is_folder:
            self.created_files.append(body["name"])
        return {"id": nid, "webViewLink": f"http://d/{nid}"}


@pytest.fixture(autouse=True)
def _drive_root(monkeypatch):
    monkeypatch.setattr(nodes, "DRIVE_FOLDER_ID", "ROOT")


def _state(registry, inv, ocr=""):
    services = MagicMock()
    services.drive = FakeDrive()
    return {
        "processing_error": "", "services": services, "registry": registry,
        "invoice_data": inv, "ocr_text": ocr, "facturx_pdf": b"%PDF-1.4",
        "invoice_folder": "2026-07 Juillet", "client_final": "LEGACY",
        "invoice_filename": "legacy.pdf", "message_id": "m1",
    }


@pytest.fixture
def registry(tmp_path):
    return SupplierRegistry.load(path=tmp_path / "reg.json", seed=SEED)


def _folder_chain(drive, file_id):
    """Reconstruit le chemin de dossiers d'un fichier."""
    parts = []
    node = drive.nodes[file_id]
    parent = node["parent"]
    while parent and parent != "ROOT":
        p = drive.nodes[parent]
        parts.append(p["name"])
        parent = p["parent"]
    return "/".join(reversed(parts))


def test_hacker_creates_three_level_path(registry):
    inv = {
        "date_facture": "2026-07-09", "numero_facture": "126181723",
        "vendeur": {"nom": "SARL JMT deco", "tva_intra": "FR41944684497"},
        "acheteur": {"nom": "Hacker", "tva_intra": "DE174736262"},
        "montant_ttc": 1200.0,
    }
    st = _state(registry, inv, "Contremarque: FEUVRIER\nHacker DE174736262 JMT FR41944684497")
    out = nodes.node_upload_drive(st)
    drive = st["services"].drive
    assert _folder_chain(drive, out["drive_file_id"]) == "2026-07 Juillet/FEUVRIER/HACKER"
    assert out["invoice_filename"] == "HACKER_FacturX_2026-07-09_126181723.pdf"


def test_communication_exception(registry):
    inv = {
        "date_facture": "2026-07-01", "numero_facture": "C1",
        "vendeur": {"nom": "Raison Home", "tva_intra": "FR88428155956"},
        "acheteur": {"nom": "JMT", "tva_intra": "FR41944684497"}, "montant_ttc": 10.0,
    }
    st = _state(registry, inv, "Raison Home FR88428155956 JMT FR41944684497")
    out = nodes.node_upload_drive(st)
    assert _folder_chain(st["services"].drive, out["drive_file_id"]) == "2026-07 Juillet/Communication"


def test_duplicate_is_skipped(registry):
    inv = {
        "date_facture": "2026-07-09", "numero_facture": "126181723",
        "vendeur": {"nom": "SARL JMT deco", "tva_intra": "FR41944684497"},
        "acheteur": {"nom": "Hacker", "tva_intra": "DE174736262"}, "montant_ttc": 1200.0,
    }
    ocr = "Contremarque: FEUVRIER\nHacker DE174736262 JMT FR41944684497"
    st = _state(registry, inv, ocr)
    nodes.node_upload_drive(st)
    drive = st["services"].drive
    n_files = len(drive.created_files)
    # Deuxième passage (même registre, nouveau state/drive partagé) : simulate re-run
    st2 = _state(registry, inv, ocr)
    st2["services"].drive = drive  # même Drive → le fichier existe déjà
    out2 = nodes.node_upload_drive(st2)
    assert len(drive.created_files) == n_files  # aucun nouveau fichier
    assert out2["drive_file_id"]  # renvoie l'existant


def test_legacy_fallback_without_registry():
    inv = {"date_facture": "2026-07-09", "numero_facture": "X", "vendeur": {"nom": "Y"}}
    st = _state(None, inv)
    st["registry"] = None
    out = nodes.node_upload_drive(st)
    drive = st["services"].drive
    assert _folder_chain(drive, out["drive_file_id"]) == "2026-07 Juillet/LEGACY"
    assert drive.created_files == ["legacy.pdf"]
