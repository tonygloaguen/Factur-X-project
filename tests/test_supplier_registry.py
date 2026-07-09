#!/usr/bin/env python3
"""test_supplier_registry.py — Registre fournisseurs & résolution d'émetteur.

Cœur : le **piège Häcker** — une facture affichant « SARL JMT déco » en en-tête
(l'acheteur) mais émise par Häcker doit résoudre vers HACKER via l'ID fort ≠ self.
Logique pure (stdlib) → tourne dans tous les environnements.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from supplier_registry import (
    SupplierRegistry, extract_identifiers, identifiers_from_invoice,
    is_valid_vat, luhn_ok, normalize_vat, slugify,
)

SEED = Path(__file__).resolve().parent.parent / "suppliers_registry.seed.json"


@pytest.fixture
def registry(tmp_path):
    return SupplierRegistry.load(path=tmp_path / "reg.json", seed=SEED)


# ── slugify (fonction unique testée) ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Häcker", "HACKER"), ("Aurélie SCHWEITZER", "AURELIE_SCHWEITZER"),
    ("IN-IPSO", "IN-IPSO"), ("JMT Déco", "JMT_DECO"), ("  a/b:c  ", "ABC"),
    ("RAISON HOME", "RAISON_HOME"), ("", ""),
])
def test_slugify(raw, expected):
    assert slugify(raw) == expected


# ── Validation des identifiants ──────────────────────────────────────────────

def test_luhn_valid_siren():
    assert luhn_ok("978827517") is True
    assert luhn_ok("123456789") is False


def test_is_valid_vat_filters_words():
    assert is_valid_vat("FR41944684497") is True
    assert is_valid_vat("DE174736262") is True
    assert is_valid_vat("FEUVRIER") is False       # préfixe FE inconnu
    assert is_valid_vat("SARLJMT") is False         # SA + pas assez de chiffres


def test_extract_ignores_word_false_positives():
    ids = extract_identifiers("SARL JMT deco Contremarque FEUVRIER TVA FR41944684497")
    assert ids.vats == {"FR41944684497"}


# ── LE PIÈGE HÄCKER ──────────────────────────────────────────────────────────

def test_hacker_trap_resolves_to_hacker(registry):
    """En-tête = 'SARL JMT déco' (acheteur), émetteur réel = Häcker (DE)."""
    text = (
        "SARL JMT deco / Raison Home / 78114 Magny / TVA: FR41944684497\n"
        "Facture Hacker Kuchen GmbH  USt-IdNr: DE174736262\n"
        "Livraison intracommunautaire exoneree  Contremarque: FEUVRIER  N 126181723"
    )
    r = registry.resolve_emitter(extract_identifiers(text))
    assert r.canonical == "HACKER"
    assert r.method == "vat"
    assert r.is_self is False


def test_hacker_trap_via_structured_fields(registry):
    """Même piège via les champs structurés Gemini (vendeur/acheteur)."""
    inv = {
        "vendeur": {"nom": "SARL JMT deco", "tva_intra": "FR41944684497"},  # en-tête trompeur
        "acheteur": {"nom": "Hacker", "tva_intra": "DE174736262"},
    }
    # Peu importe quel champ Gemini a rempli : la règle d'or écarte self.
    r = registry.resolve_emitter(identifiers_from_invoice(inv))
    assert r.canonical == "HACKER"


# ── Vente JMT (émetteur = self) ──────────────────────────────────────────────

def test_jmt_sale_resolves_to_self(registry):
    text = "Societe JMT Deco TVA FR41944684497 Facture acompte Client Aurelie SCHWEITZER tel 01 23 45 67 89"
    r = registry.resolve_emitter(extract_identifiers(text))
    assert r.canonical == "JMT_DECO"
    assert r.is_self is True


# ── Autres méthodes de résolution ────────────────────────────────────────────

def test_resolve_by_domain_excludes_personal(registry):
    r = registry.resolve_emitter(extract_identifiers("contact@lmcstore.com  JMT FR41944684497"))
    assert r.canonical == "LMC" and r.method == "domain"


def test_resolve_by_siren_ignores_personal_email(registry):
    r = registry.resolve_emitter(
        extract_identifiers("Menuiserie JH SIREN 978827517 johann.hardy78@gmail.com JMT FR41944684497"))
    assert r.canonical == "MENUISERIE_JH" and r.method == "siren"


def test_unknown_supplier_is_unresolved(registry):
    r = registry.resolve_emitter(
        extract_identifiers("Nouveau Fournisseur SAS TVA FR90123456789 client JMT FR41944684497"))
    assert r.canonical is None and r.method == "unresolved"


# ── Apprentissage & complétion ───────────────────────────────────────────────

def test_learn_new_entity_persists_atomically(registry, tmp_path):
    canonical = registry.learn({
        "legal_name": "Nouvelle Menuiserie SARL", "brand": "NouvMenu",
        "vat": "FR90123456789", "email_domains": ["nouvmenu.fr"], "country": "FR",
    })
    assert canonical == "NOUVMENU"
    entry = registry.get(canonical)
    assert "auto_learned" in entry["flags"] and "to_review" in entry["flags"]
    # Réécrit sur disque → un nouveau registre le retrouve.
    reloaded = SupplierRegistry.load(path=tmp_path / "reg.json", seed=SEED)
    assert reloaded.get("NOUVMENU") is not None
    # Désormais résolu par TVA.
    r = reloaded.resolve_emitter(extract_identifiers("TVA FR90123456789 client JMT FR41944684497"))
    assert r.canonical == "NOUVMENU"


def test_complete_existing_entity_no_duplicate(registry, tmp_path):
    """LMC n'a qu'un domaine dans le seed : on complète avec sa TVA (pas de doublon)."""
    before = len(list(registry.entities()))
    assert registry.complete_entry("LMC", vat="FR12345678901") is True
    after = len(list(registry.entities()))
    assert after == before  # aucune nouvelle entité
    assert "FR12345678901" in registry.get("LMC")["vat"]
    r = registry.resolve_emitter(extract_identifiers("TVA FR12345678901 client JMT FR41944684497"))
    assert r.canonical == "LMC" and r.method == "vat"


def test_learn_generates_unique_canonical(registry):
    c1 = registry.learn({"brand": "DUPLI", "vat": "FR11111111111"})
    c2 = registry.learn({"brand": "DUPLI", "vat": "FR22222222222"})
    assert c1 == "DUPLI" and c2 == "DUPLI_2"
