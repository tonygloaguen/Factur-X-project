# Mission — Fiabiliser le pipeline Factur-X (orchestrateur LangGraph)

## Rôle
Tu es ingénieur Python senior, spécialiste **Factur-X / EN16931 / CII (UN/CEFACT)**. Tu interviens sur `facturx-staging` (orchestrateur LangGraph, pure Python, 9 nœuds, pas de microservice HTTP). Objectif : faire passer le **taux de succès réel** de ~35 % à >90 %, sans régresser l'existant.

## Méthode imposée (NON négociable)
> Avant tout patch : **trace l'exécution logique ligne par ligne, identifie le point de divergence exact, PUIS propose le fix.** Aucun patch spéculatif.

Procédure obligatoire :
1. **Explore d'abord.** Cartographie le repo, identifie les 9 nœuds LangGraph et le module qui construit le XML CII + celui qui embarque le PDF/A-3. Ne présume pas des noms de fichiers, lis-les.
2. Pour chaque problème ci-dessous : localise le code fautif, **cite le fichier:ligne**, explique le point de divergence, propose le patch, puis applique.
3. **Un lot = un commit** (P0 → P4). Tests verts avant de passer au lot suivant. Message de commit conventionnel (`fix(cii): ...`).
4. Ne touche pas au réseau/OAuth/Gmail au-delà de ce qui est demandé en P2.

---

## Contexte système (établi, à vérifier dans le code)
- **Entrée** : polling Gmail quotidien. Requête actuelle : `has:attachment filename:pdf -label:Factures-Traitées newer_than:7d`.
- **Flux** (9 nœuds) : `extract_text` → `filter_document` (score keywords) → `call_gemini` (extraction EN16931) → `normalize_data` → construction **XML CII** → **embed Factur-X** (PDF/A-3) → renommage `{Fournisseur}_FacturX_{date}_{num}.pdf` → `label_gmail` (`Factures-Traitées` + `Fournisseurs/<X>`).
- **Profil cible** : `urn:cen.eu:en16931:2017`.
- **State DB** : SQLite, compteurs `success` / `error` / `not_invoice`.
- **Validation** : schematron officiel EN16931 (déjà branché ; c'est lui qui rejette).
- **Quota Gemini** : 18/jour, jamais atteint (≤4/18 en régime courant) → **le quota n'est PAS le problème, ne le touche pas.**

---

## Diagnostic déjà établi — 3 semaines de logs + 19 PDF Drive analysés

Fait marquant : **les 10 XML actuellement générés n'utilisent QUE `C62` (pièce), 28/28.** Le pipeline ne sait produire que du « à la pièce ». Toute facture avec une autre unité échoue. Corrige les causes ci-dessous **dans cet ordre de priorité (impact décroissant)**.

### P0 — `@unitCode is not allowed` — 252 occurrences, cause n°1
**Divergence** : `BilledQuantity/@unitCode` reçoit une unité en clair (« m2 », « UNI », « UNITE », « ml ») au lieu d'un code **UN/ECE Recommendation 20**. Le schematron rejette → 6 erreurs typiques par facture.
**Preuve terrain** : les 3 factures IN-IPSO (`FPA0002846`, `FPA0002861`, `FAC0045917`) sont en **m²** (mélaminé). `FPA0002846` = exactement les 6 erreurs du 2026-06-23 dans les logs.

**Fix attendu** : un module de normalisation d'unités déterministe, appelé dans `normalize_data` **avant** la construction du CII. Jamais laisser passer la valeur brute de Gemini.

```python
# unece_units.py — spec de référence, à adapter au code réel
import unicodedata

_UNECE: dict[str, str] = {
    "piece": "C62", "pieces": "C62", "pce": "C62", "pc": "C62", "u": "C62",
    "uni": "C62", "unite": "C62", "unites": "C62", "u.v": "C62", "": "C62",
    "ea": "C62", "each": "C62", "lot": "C62", "ens": "C62", "forfait": "C62",
    "m2": "MTK", "m²": "MTK", "metre carre": "MTK",        # ← manquant n°1 (IN-IPSO)
    "ml": "MTR", "m": "MTR", "metre": "MTR", "metre lineaire": "MTR",
    "m3": "MTQ", "kg": "KGM", "g": "GRM", "t": "TNE",
    "l": "LTR", "litre": "LTR", "h": "HUR", "heure": "HUR",
    "jour": "DAY", "paire": "PR", "boite": "XBX", "carton": "XCT",
    "palette": "XPX", "rouleau": "NRL",
}

def _norm(raw: str) -> str:
    s = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode()
    return s.lower().strip().rstrip(".")

def to_unece(raw: str, *, default: str = "C62") -> str:
    """Mappe une unité libre vers un code UN/ECE Rec 20. Fallback C62, jamais d'exception."""
    code = _UNECE.get(_norm(raw))
    if code is None:
        logger.warning("unitCode inconnu '%s' → fallback %s", raw, default)
        return default
    return code
```
**Piège métier** : « ml » chez ces fournisseurs = **mètre linéaire** (`MTR`), PAS millilitre (`MLT`). Vérifie-le explicitement.
**Critère d'acceptation** : les 3 factures IN-IPSO passent le schematron. Ajoute un test paramétré couvrant toutes les clés du mapping.

### P1 — `BR-CO-13` / `BR-S-08` — cohérence des montants (24 + N occurrences)
**Divergence** : `Total HT (BT-109) = Σ lignes nettes − remises + charges` ne boucle pas. Deux causes vues :
- Gemini renvoie des totaux arrondis/incohérents → **ne jamais faire confiance aux totaux du LLM**, recalcule-les.
- Facture **Leroy Merlin chantier** : montants **en TTC ligne à ligne** → il faut reconvertir en HT avant d'agréger.

**Fix attendu** : recalcul intégral en `Decimal` (ROUND_HALF_UP, quantize 0.01) dans `normalize_data`, qui **écrase** les agrégats de Gemini. Détecter le cas « prix TTC » et reconvertir via le taux de TVA de la ligne.

```python
from decimal import Decimal, ROUND_HALF_UP
CENT = Decimal("0.01")
def q(x: Decimal) -> Decimal: return x.quantize(CENT, rounding=ROUND_HALF_UP)

def reconcile_totals(lines, doc_charges=Decimal(0), doc_allowances=Decimal(0)):
    net = sum((q(Decimal(str(l["qty"])) * Decimal(str(l["unit_price"]))) for l in lines), Decimal(0))
    total_ht = q(net - doc_allowances + doc_charges)                # BT-109 / BR-CO-13
    vat_by_rate: dict[Decimal, Decimal] = {}
    for l in lines:
        base = q(Decimal(str(l["qty"])) * Decimal(str(l["unit_price"])))
        vat_by_rate.setdefault(Decimal(str(l["vat_rate"])), Decimal(0))
        vat_by_rate[Decimal(str(l["vat_rate"]))] += base           # BR-S-08
    total_vat = sum((q(b * r / 100) for r, b in vat_by_rate.items()), Decimal(0))
    return {"total_ht": total_ht, "total_vat": q(total_vat),
            "total_ttc": q(total_ht + total_vat),
            "vat_breakdown": {str(r): q(b) for r, b in vat_by_rate.items()}}
```

**Éco-participation** (DEA mobilier chez Discac, PMCB/DEEE chez Leroy Merlin) : traite ces montants explicitement — soit ligne de facture normale, soit charge document-level (BG-21) — jamais ignorée ni mal placée. **Escompte** (Discac 2 %) = allowance document-level.
**Nettoyage extraction** : la facture Discac contient des pseudo-balises `<client>…</client>` dans le texte source qui polluent Gemini. Ajoute un pré-nettoyage du texte avant l'appel Gemini.
**Critère d'acceptation** : Discac (`FAE2625100`) et Leroy Merlin chantier passent le schematron.

### P2 — Factures jamais captées (ni succès, ni erreur) — 5 cas
**Divergence** : ce ne sont PAS des échecs d'embedding, ce sont des trous de collecte.
- `newer_than:7d` : toute facture > J-7 au démarrage est **invisible pour toujours**. Perdus : `BAUS-2026-05-000514` (30/05) et les 3 `RE…MARTINEAU` (29/05, Häcker).
- `filename:pdf` exclut les images → `facture jardinerie.jpg` jamais vue.
- **Piège structurel** : une facture qui échoue en boucle n'est jamais labellisée `Factures-Traitées`, donc sort de la fenêtre à J+7 → **perdue silencieusement**.

**Fix attendu** :
1. Élargir la fenêtre (paramétrable, ex. `POLL_WINDOW_DAYS`, défaut 30) + option de **rattrapage** sans borne temporelle.
2. Sur échec d'embedding : appliquer un label `Factures-Erreur` (à créer) au lieu de laisser l'email « nu » → traçabilité + requête de reprise dédiée.
3. Créer un **script CLI de rattrapage** `scripts/replay.py --message-id … | --since YYYY-MM-DD` pour réinjecter des factures précises (les 5 perdues).
4. (Optionnel, branche image) : si pièce jointe image, convertir via **Tesseract** (déjà dans la stack) → PDF avant `extract_text`.

**Critère d'acceptation** : `replay.py` rejoue BAUS + les 3 Häcker ; label `Factures-Erreur` visible sur un échec simulé.

### P3 — Robustesse extraction (Pydantic)
**Divergence** : `type_facture = None` renvoyé par Gemini viole `str` (12 occurrences).
**Fix** : champ tolérant + défaut métier `380` (facture commerciale).
```python
from typing import Literal
from pydantic import field_validator
class GeminiInvoiceOutput(BaseModel):
    type_facture: Literal["380","381","384","389"] = "380"  # 380=facture, 381=avoir
    @field_validator("type_facture", mode="before")
    @classmethod
    def _default_type(cls, v): return v or "380"
```
Passe en revue les autres champs susceptibles d'arriver `None` et applique la même tolérance (défauts documentés).

### P4 — Fournisseur étranger / autoliquidation (BR-CO-26, BR-AE-02/08)
**Divergence** : Häcker (`@haecker-kuechen.de`) et cas IN-IPSO en reverse charge → VAT ID vendeur non-`FR` manquant, `CategoryCode` TVA mal positionné.
**Fix** :
- Prompt Gemini : extraire explicitement le **VAT ID vendeur** (préfixe pays + numéro) et le SIRET si présent.
- Si régime autoliquidation (`AE`/`K`) : imposer BT-31 vendeur **et** BT-48 acheteur (le tien, en config/env), sinon **router en `Factures-Erreur` explicite** plutôt que produire un XML invalide.

---

## Contraintes de code (conventions projet)
- Python **3.11+**, typage **mypy strict**, docstrings **Google**.
- Perf-first : pas de recalcul redondant, `Decimal` uniquement pour la monnaie.
- Secrets/config **exclusivement via variables d'environnement** (fenêtre de polling, VAT acheteur, etc.).
- Pas de dépendance nouvelle sans justification ; réutilise l'existant.
- Logs cohérents avec le style actuel (préfixes `[ n/9 ]`, emojis de statut conservés).

## Garde-fous anti-régression (bloquants)
1. **Golden set** : les 10 factures actuellement valides (GPDIS, INTERBAT, LMC, Leroy Merlin ALU, JMT ×2, Menuiserie JH, Eduardo, Raison Home) DOIVENT rester conformes au schematron après chaque lot. Crée un test qui régénère leur CII et le valide.
2. Ajoute les 5 factures « à débloquer » (3 IN-IPSO, Discac, Leroy Merlin chantier) comme **cas de non-régression cibles** : rouge avant, vert après.
3. Aucune modification du quota Gemini ni de la logique OAuth (hors label P2).
4. Chaque lot : `mypy`, tests unitaires, validation schematron → verts avant commit.

## Livrables attendus
1. Patches par lot (P0→P4), un commit chacun, avec `fichier:ligne` du point de divergence dans le message.
2. `unece_units.py` + tests paramétrés.
3. `scripts/replay.py` (rattrapage).
4. Suite de tests golden + cibles (fixtures = les 15 PDF).
5. Un `RAPPORT_CORRECTIONS.md` : pour chaque P, la divergence tracée, le fix, le résultat schematron (avant/après).

## Point de départ
Commence par cartographier le repo et me confirmer : (a) le fichier qui construit le CII, (b) le fichier qui embarque le PDF/A-3, (c) où est appelé le schematron. Puis attaque **P0**. Ne passe à P1 que lorsque les 3 IN-IPSO sont vertes ET que le golden set n'a pas régressé.
