#!/usr/bin/env python3
"""supplier_registry.py — Registre fournisseurs persistant + résolution émetteur.

Source de vérité de l'identité des fournisseurs pour le classement GDrive. Le
registre (JSON) est chargé au démarrage, indexé en mémoire (index inversés), et
enrichi à chaud (apprentissage + complétion) avec réécriture **atomique**.

Règle d'or d'identification de l'émetteur
-----------------------------------------
Le fournisseur = l'entité de la facture dont l'identifiant FORT (TVA intracom,
puis SIRET/SIREN) est **différent de celui de ``self``** (JMT Déco). C'est ce
qui résout le « piège Häcker » : une facture Häcker affiche à la fois
``FR41944684497`` (JMT Déco = acheteur, en en-tête) et ``DE174736262`` (Häcker =
vendeur). En retenant l'ID fort qui n'est PAS celui de ``self``, l'émetteur est
identifié sans dépendre de la mise en page.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("orchestrator")

# Emplacement du registre vivant (réécrit à chaud) et du seed (lecture seule).
_DEFAULT_REGISTRY = Path(
    os.environ.get("SUPPLIERS_REGISTRY_PATH", "/app/data/suppliers_registry.json")
)
_SEED_FILE = Path(
    os.environ.get(
        "SUPPLIERS_REGISTRY_SEED",
        str(Path(__file__).resolve().parent.parent / "suppliers_registry.seed.json"),
    )
)

# Domaines email personnels : jamais fiables comme clé d'identité d'entreprise.
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.fr", "outlook.com",
    "outlook.fr", "live.fr", "live.com", "orange.fr", "wanadoo.fr", "free.fr",
    "sfr.fr", "laposte.net", "yahoo.com", "yahoo.fr", "icloud.com", "me.com",
    "bbox.fr", "neuf.fr", "aol.com", "protonmail.com",
})

# Seuil de similarité (0-100) pour le rapprochement flou sur raison sociale/marque.
FUZZY_THRESHOLD = 90

# Préfixes de n° TVA intracommunautaire valides (ISO pays UE + quelques voisins).
# Filtre les faux positifs du type « FEUVRIER » (FE) ou « SARL » (SA) que la
# regex large capturerait sinon comme des TVA.
EU_VAT_PREFIXES: frozenset[str] = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "GB", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL",
    "PT", "RO", "SE", "SI", "SK", "CH", "NO", "XI",
})

_VAT_RE = re.compile(r"\b([A-Z]{2})\s?([0-9A-Z][0-9A-Z ]{1,12}[0-9A-Z])\b")
_SIRET_RE = re.compile(r"\b(\d[\d ]{12,16}\d)\b")  # 14 chiffres, espaces tolérés
_DIGITS_RE = re.compile(r"\d")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def luhn_ok(digits: str) -> bool:
    """Valide une chaîne de chiffres par l'algorithme de Luhn (SIREN/SIRET).

    Les SIREN (9) et SIRET (14) français satisfont Luhn ; cela écarte les faux
    positifs (numéros de téléphone, de facture…) captés par la regex.
    """
    if not digits or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_vat(vat: str) -> bool:
    """Vrai si ``vat`` normalisé a un préfixe pays connu ET contient des chiffres."""
    vat = normalize_vat(vat)
    if len(vat) < 6 or vat[:2] not in EU_VAT_PREFIXES:
        return False
    return sum(c.isdigit() for c in vat[2:]) >= 4


# ─────────────────────────────────────────────────────────────────────────────
# Slug canonique (nom de dossier) — fonction unique, réutilisée par classify.py
# ─────────────────────────────────────────────────────────────────────────────

def slugify(value: str) -> str:
    """Normalise un libellé en nom de dossier canonique.

    MAJUSCULES, accents retirés (NFKD), espaces → ``_``, tout caractère non
    alphanumérique supprimé sauf ``_`` et ``-``. Ex. ``"Häcker" → "HACKER"``.

    Args:
        value: Libellé brut (nom de société, marque, client…).

    Returns:
        Slug en MAJUSCULES, ou ``""`` si l'entrée ne contient rien d'exploitable.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_str = decomposed.encode("ascii", "ignore").decode("ascii")
    ascii_str = ascii_str.upper().strip()
    ascii_str = re.sub(r"\s+", "_", ascii_str)
    ascii_str = re.sub(r"[^A-Z0-9_-]", "", ascii_str)
    ascii_str = re.sub(r"_+", "_", ascii_str).strip("_")
    return ascii_str


# ─────────────────────────────────────────────────────────────────────────────
# Extraction et normalisation des identifiants
# ─────────────────────────────────────────────────────────────────────────────

def normalize_vat(raw: str) -> str:
    """Normalise un n° TVA intracom : MAJUSCULES sans espaces (``FR 123`` → ``FR123``)."""
    return re.sub(r"\s+", "", (raw or "").upper())


def normalize_digits(raw: str) -> str:
    """Ne conserve que les chiffres (pour SIRET/SIREN)."""
    return "".join(_DIGITS_RE.findall(raw or ""))


@dataclass
class Identifiers:
    """Identifiants extraits d'une facture (déjà normalisés)."""

    vats: set[str] = field(default_factory=set)
    sirets: set[str] = field(default_factory=set)
    sirens: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    names: list[str] = field(default_factory=list)


def extract_identifiers(text: str) -> Identifiers:
    """Extrait tous les identifiants forts et faibles d'un texte de facture.

    Args:
        text: Texte source (OCR) ou bloc émetteur.

    Returns:
        Un :class:`Identifiers` avec TVA, SIRET, SIREN et domaines email.
    """
    ids = Identifiers()
    if not text:
        return ids

    for country, body in _VAT_RE.findall(text):
        vat = normalize_vat(country + body)
        if is_valid_vat(vat):
            ids.vats.add(vat)

    for match in _SIRET_RE.findall(text):
        digits = normalize_digits(match)
        if len(digits) == 14 and luhn_ok(digits):
            ids.sirets.add(digits)
            ids.sirens.add(digits[:9])  # le SIREN est le préfixe du SIRET

    # SIREN isolés (9 chiffres, Luhn valide) non déjà couverts par un SIRET.
    for match in re.findall(r"\b(\d[\d ]{7,10}\d)\b", text):
        digits = normalize_digits(match)
        if len(digits) == 9 and luhn_ok(digits):
            ids.sirens.add(digits)

    for domain in _EMAIL_RE.findall(text):
        ids.domains.add(domain.lower())

    return ids


def identifiers_from_invoice(inv: dict) -> Identifiers:
    """Construit les identifiants à partir des données Gemini normalisées.

    Combine les champs structurés vendeur/acheteur (TVA, SIRET, domaines email)
    afin d'alimenter la résolution d'émetteur. Les deux parties sont incluses :
    la règle d'or se charge d'écarter ``self``.
    """
    ids = Identifiers()
    for party_key in ("vendeur", "acheteur"):
        party = inv.get(party_key) or {}
        vat = normalize_vat(str(party.get("tva_intra") or ""))
        if vat:
            ids.vats.add(vat)
        siret = normalize_digits(str(party.get("siret") or ""))
        if len(siret) == 14:
            ids.sirets.add(siret)
            ids.sirens.add(siret[:9])
        siren = normalize_digits(str(party.get("siren") or ""))
        if len(siren) == 9:
            ids.sirens.add(siren)
        for field_name in ("email", "email_domain", "domaine"):
            val = str(party.get(field_name) or "")
            m = _EMAIL_RE.search(val)
            if m:
                ids.domains.add(m.group(1).lower())
            elif "." in val and "@" not in val:
                ids.domains.add(val.strip().lower())
        name = str(party.get("nom") or party.get("nom_court") or "").strip()
        if name:
            ids.names.append(name)
    return ids


# ─────────────────────────────────────────────────────────────────────────────
# Résultat de résolution
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Resolution:
    """Résultat de la résolution d'émetteur."""

    canonical: Optional[str]           # nom canonique (= dossier) ou None
    method: str                        # 'vat' | 'siret' | 'siren' | 'domain' | 'fuzzy' | 'self' | 'unresolved'
    entry: Optional[dict] = None       # entrée registre correspondante
    is_self: bool = False              # True si l'émetteur est self (facture de vente)

    @property
    def resolved(self) -> bool:
        return self.canonical is not None


# ─────────────────────────────────────────────────────────────────────────────
# Registre
# ─────────────────────────────────────────────────────────────────────────────

class SupplierRegistry:
    """Registre fournisseurs avec index inversés et résolution d'émetteur."""

    def __init__(self, data: dict, path: Optional[Path] = None) -> None:
        """Initialise le registre à partir d'un dict déjà chargé.

        Args:
            data: Contenu du registre (clé = nom canonique ; ``_meta`` optionnel).
            path: Chemin de persistance (pour la réécriture atomique).
        """
        self._path = path
        self._data = data
        self._meta = data.get("_meta", {})
        self.self_canonical: str = self._meta.get("self", "JMT_DECO")
        self._reindex()

    # ── Chargement / persistance ──────────────────────────────────────────────

    @classmethod
    def load(
        cls, path: Optional[Path] = None, seed: Optional[Path] = None
    ) -> "SupplierRegistry":
        """Charge le registre vivant, en l'amorçant depuis le seed si absent.

        Args:
            path: Chemin du registre vivant (défaut : env SUPPLIERS_REGISTRY_PATH).
            seed: Chemin du seed en lecture seule (défaut : suppliers_registry.seed.json).

        Returns:
            Une instance de :class:`SupplierRegistry`.
        """
        path = Path(path) if path else _DEFAULT_REGISTRY
        seed = Path(seed) if seed else _SEED_FILE

        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info("Registre fournisseurs chargé : %s (%d entités)", path, _count(data))
        elif seed.exists():
            data = json.loads(seed.read_text(encoding="utf-8"))
            logger.info("Registre amorcé depuis le seed : %s (%d entités)", seed, _count(data))
            reg = cls(data, path=path)
            reg._atomic_write()  # matérialise le registre vivant
            return reg
        else:
            logger.warning("Aucun registre ni seed trouvé (%s / %s) — registre vide", path, seed)
            data = {"_meta": {"self": "JMT_DECO", "version": 1}}
        return cls(data, path=path)

    def _atomic_write(self) -> None:
        """Réécrit le registre de façon atomique (fichier temp + os.replace)."""
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── Index inversés ────────────────────────────────────────────────────────

    def _reindex(self) -> None:
        """(Re)construit les index inversés vat/siret/siren/domaine → canonical."""
        self.by_vat: dict[str, str] = {}
        self.by_siret: dict[str, str] = {}
        self.by_siren: dict[str, str] = {}
        self.by_domain: dict[str, str] = {}
        for canonical, entry in self.entities():
            for vat in entry.get("vat", []) or []:
                self.by_vat[normalize_vat(vat)] = canonical
            for siret in entry.get("siret", []) or []:
                digits = normalize_digits(siret)
                if len(digits) == 14:
                    self.by_siret[digits] = canonical
                    self.by_siren.setdefault(digits[:9], canonical)
            for siren in entry.get("siren", []) or []:
                digits = normalize_digits(siren)
                if len(digits) == 9:
                    self.by_siren[digits] = canonical
            for domain in entry.get("email_domains", []) or []:
                d = domain.lower().strip()
                if d and d not in PERSONAL_EMAIL_DOMAINS:
                    self.by_domain[d] = canonical

    def entities(self):
        """Itère sur (canonical, entry) en ignorant la clé ``_meta``."""
        for key, entry in self._data.items():
            if key == "_meta":
                continue
            yield key, entry

    def get(self, canonical: str) -> Optional[dict]:
        return self._data.get(canonical)

    @property
    def self_vats(self) -> set[str]:
        entry = self._data.get(self.self_canonical, {})
        return {normalize_vat(v) for v in entry.get("vat", []) or []}

    @property
    def self_sirets(self) -> set[str]:
        entry = self._data.get(self.self_canonical, {})
        return {normalize_digits(s) for s in entry.get("siret", []) or []}

    @property
    def self_sirens(self) -> set[str]:
        entry = self._data.get(self.self_canonical, {})
        sirens = {normalize_digits(s) for s in entry.get("siren", []) or []}
        sirens |= {s[:9] for s in self.self_sirets}
        return sirens

    # ── Résolution d'émetteur (règle d'or) ────────────────────────────────────

    def resolve_emitter(self, ids: Identifiers) -> Resolution:
        """Identifie l'émetteur (fournisseur) d'une facture selon la règle d'or.

        Ordre déterministe → flou, arrêt au premier concluant :
        TVA ≠ self → SIRET/SIREN ≠ self → domaine (hors perso) → fuzzy nom.
        Si aucun identifiant non-``self`` n'existe mais que ``self`` est présent,
        l'émetteur EST ``self`` (facture de vente).

        Args:
            ids: Identifiants extraits de la facture.

        Returns:
            Une :class:`Resolution` (``canonical`` = None si à apprendre).
        """
        non_self_vats = {v for v in ids.vats if v not in self.self_vats}
        non_self_sirets = {s for s in ids.sirets if s not in self.self_sirets}
        non_self_sirens = {s for s in ids.sirens if s not in self.self_sirens}
        non_self_domains = {
            d for d in ids.domains
            if d not in PERSONAL_EMAIL_DOMAINS
            and self.by_domain.get(d) != self.self_canonical
        }

        # 1) TVA forte ≠ self
        for vat in sorted(non_self_vats):
            if vat in self.by_vat:
                canonical = self.by_vat[vat]
                return Resolution(canonical, "vat", self.get(canonical),
                                  is_self=(canonical == self.self_canonical))

        # 2) SIRET puis SIREN ≠ self
        for siret in sorted(non_self_sirets):
            if siret in self.by_siret:
                canonical = self.by_siret[siret]
                return Resolution(canonical, "siret", self.get(canonical),
                                  is_self=(canonical == self.self_canonical))
        for siren in sorted(non_self_sirens):
            if siren in self.by_siren:
                canonical = self.by_siren[siren]
                return Resolution(canonical, "siren", self.get(canonical),
                                  is_self=(canonical == self.self_canonical))

        # 3) Facture de vente : aucun tiers identifié mais self présent → émetteur = self
        self_present = bool(ids.vats & self.self_vats) or bool(ids.sirets & self.self_sirets)
        has_third_party_strong = bool(non_self_vats or non_self_sirets or non_self_sirens)
        if self_present and not has_third_party_strong and not non_self_domains:
            return Resolution(self.self_canonical, "self",
                              self.get(self.self_canonical), is_self=True)

        # 4) Domaine email (hors perso) ≠ self
        for domain in sorted(non_self_domains):
            if domain in self.by_domain:
                canonical = self.by_domain[domain]
                return Resolution(canonical, "domain", self.get(canonical),
                                  is_self=(canonical == self.self_canonical))

        # 5) Fuzzy sur legal_name / brand (seuil élevé)
        fuzzy = self._fuzzy_match(ids.names)
        if fuzzy:
            return Resolution(fuzzy, "fuzzy", self.get(fuzzy),
                              is_self=(fuzzy == self.self_canonical))

        # 6) Rien de concluant → à apprendre
        return Resolution(None, "unresolved")

    def _fuzzy_match(self, names: list[str]) -> Optional[str]:
        """Rapprochement flou d'un nom sur legal_name/brand (difflib, seuil élevé)."""
        best_canonical: Optional[str] = None
        best_score = 0.0
        for name in names:
            slug_name = slugify(name)
            if not slug_name:
                continue
            for canonical, entry in self.entities():
                candidates = [canonical, slugify(entry.get("legal_name", "")),
                              slugify(entry.get("brand", ""))]
                for cand in candidates:
                    if not cand:
                        continue
                    score = difflib.SequenceMatcher(None, slug_name, cand).ratio() * 100
                    if score > best_score:
                        best_score = score
                        best_canonical = canonical
        return best_canonical if best_score >= FUZZY_THRESHOLD else None

    # ── Apprentissage / complétion ────────────────────────────────────────────

    def complete_entry(
        self, canonical: str, *, vat: Optional[str] = None,
        siret: Optional[str] = None, siren: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> bool:
        """Complète une entité connue avec un identifiant fort nouveau (in place).

        Évite les doublons : au lieu de créer une nouvelle entité, on enrichit
        l'existante (« apprendre au fur et à mesure les coordonnées complètes »).

        Returns:
            True si au moins un identifiant a été ajouté (registre réécrit).
        """
        entry = self._data.get(canonical)
        if entry is None:
            return False
        changed = False
        if vat:
            v = normalize_vat(vat)
            if v and v not in {normalize_vat(x) for x in entry.get("vat", []) or []}:
                entry.setdefault("vat", []).append(v)
                changed = True
        if siret:
            s = normalize_digits(siret)
            if len(s) == 14 and s not in {normalize_digits(x) for x in entry.get("siret", []) or []}:
                entry.setdefault("siret", []).append(s)
                changed = True
        if siren:
            s = normalize_digits(siren)
            if len(s) == 9 and s not in {normalize_digits(x) for x in entry.get("siren", []) or []}:
                entry.setdefault("siren", []).append(s)
                changed = True
        if domain:
            d = domain.lower().strip()
            if d and d not in PERSONAL_EMAIL_DOMAINS and d not in (entry.get("email_domains", []) or []):
                entry.setdefault("email_domains", []).append(d)
                changed = True
        if changed:
            logger.info("Registre : entité '%s' complétée (nouvel identifiant fort)", canonical)
            self._reindex()
            self._atomic_write()
        return changed

    def learn(self, learned: dict) -> str:
        """Ajoute une nouvelle entité apprise (Gemini) et retourne son canonical.

        Args:
            learned: Dict ``{legal_name, brand, vat, siret, siren, email_domains,
                country}`` issu de l'extraction du bloc émetteur.

        Returns:
            Le nom canonique généré (slug MAJUSCULE, unique).
        """
        base = slugify(learned.get("brand") or learned.get("legal_name") or "FOURNISSEUR_INCONNU")
        base = base or "FOURNISSEUR_INCONNU"
        canonical = base
        i = 2
        while canonical in self._data:
            canonical = f"{base}_{i}"
            i += 1

        def _as_list(v) -> list[str]:
            if not v:
                return []
            return v if isinstance(v, list) else [v]

        entry = {
            "role": "supplier",
            "legal_name": learned.get("legal_name") or canonical,
            "brand": learned.get("brand") or "",
            "vat": [normalize_vat(v) for v in _as_list(learned.get("vat")) if v],
            "siret": [normalize_digits(s) for s in _as_list(learned.get("siret")) if normalize_digits(s)],
            "siren": [normalize_digits(s) for s in _as_list(learned.get("siren")) if normalize_digits(s)],
            "email_domains": [
                d.lower() for d in _as_list(learned.get("email_domains"))
                if d and d.lower() not in PERSONAL_EMAIL_DOMAINS
            ],
            "country": learned.get("country") or "FR",
            "flags": ["auto_learned", "to_review"],
            "note": "Entité apprise automatiquement — à revoir.",
        }
        self._data[canonical] = entry
        logger.info(
            "Registre : nouvelle entité apprise '%s' (flags auto_learned, to_review) — revue humaine requise",
            canonical,
        )
        self._reindex()
        self._atomic_write()
        return canonical

    def add_flag(self, canonical: str, flag: str) -> bool:
        """Ajoute un flag à une entité (ex. 'intracommunity') si absent."""
        entry = self._data.get(canonical)
        if entry is None:
            return False
        flags = entry.setdefault("flags", [])
        if flag not in flags:
            flags.append(flag)
            self._atomic_write()
            return True
        return False


def _count(data: dict) -> int:
    return sum(1 for k in data if k != "_meta")
