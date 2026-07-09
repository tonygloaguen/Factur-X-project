# RAPPORT_CLASSEMENT — Rangement GDrive (mois / contremarque / fournisseur)

Mission : passer d'un rangement à un niveau (mois) à **3 niveaux**
`mois / contremarque / fournisseur` + exception `Communication`, avec une
identification **certaine** de l'émetteur (registre + identifiants forts), pas
une heuristique sur le nom d'en-tête.

## Points d'ancrage confirmés (point de départ)

- **(a) Écriture Drive / construction du chemin** : `orchestrator/nodes.py`
  → `node_upload_drive`. Avant : `ROOT / <mois> / <client_final> / fichier`
  (2 niveaux, `client_final` heuristique). Après : `ROOT / <mois> /
  <contremarque> / <fournisseur> /` via `classify`.
- **(b) Extraction vendeur/acheteur** : sortie Gemini → `normalize_invoice_data`
  → `inv["vendeur"]` / `inv["acheteur"]`. Le nom de fournisseur était pris
  **heuristiquement** sur `vendeur.nom_court` (`build_filename`) — d'où le piège
  Häcker (l'en-tête affiche « SARL JMT déco », l'acheteur).

## Règle d'or d'identification (résout le piège Häcker)

> Le fournisseur = l'entité de la facture dont l'**identifiant fort** (TVA
> intracom, puis SIRET/SIREN) est **≠ celui de `self`** (JMT Déco,
> `FR41944684497`).

Une facture Häcker porte `FR41944684497` (JMT = acheteur, en en-tête) **et**
`DE174736262` (Häcker = vendeur). En retenant l'ID ≠ self → **HACKER**, quelle
que soit la mise en page. Fiabilité renforcée : préfixe TVA validé
(`EU_VAT_PREFIXES`) et SIREN/SIRET validés par **Luhn**, ce qui écarte les faux
positifs (mots comme « FEUVRIER », numéros de téléphone) qui fausseraient la
détection.

Ordre de résolution : TVA → SIRET/SIREN → domaine email (hors perso) → fuzzy nom
→ apprentissage (nouvelle entité `auto_learned` / `to_review`). Aucun tiers
identifié mais `self` présent ⇒ **facture de vente** (émetteur = self).

## Plan de rangement — cas de référence (validés)

| Facture | Chemin cible | Méthode |
|---------|--------------|---------|
| Häcker `126181723`, réf. FEUVRIER (réf. du brief) | `2026-07 Juillet/FEUVRIER/HACKER/HACKER_FacturX_2026-07-09_126181723.pdf` | TVA (DE ≠ self) |
| IN-IPSO `FPA0002846` (m²) | `2026-06 Juin/MARTIN/IN-IPSO/IN-IPSO_FacturX_2026-06-23_FPA0002846.pdf` | TVA |
| GPDIS avoir `RCL000962826` | `2026-07 Juillet/DURAND/GPDIS/AVOIR_GPDIS_FacturX_2026-07-03_RCL000962826.pdf` | TVA (+ préfixe AVOIR_) |
| Vente JMT (acompte) | `2026-07 Juillet/SCHWEITZER/JMT_DECO/JMT_DECO_FacturX_2026-07-05_F-100.pdf` | self → contremarque = client |
| Raison Home (franchiseur) | `2026-07 Juillet/Communication/…` | exception Communication (ID fort franchiseur) |
| Menuiserie JH (SAV GRAIRE + MARTINEAU) | `2026-07 Juillet/MARTINEAU/MENUISERIE_JH/…` | SIREN ; contremarque **dominante** (900 € > 100 €), GRAIRE loguée |

## Registre — état initial (`suppliers_registry.json`, seed)

13 entités. `self` = JMT_DECO (`FR41944684497`), franchiseur = Communication
(`FR88428155956`).

**Identités fortes déjà connues** : HACKER (DE174736262), IN-IPSO
(FR28753043371), GPDIS (FR64327127247), INTERBAT (FR57315041756), BAUS
(FR93398118679), MENUISERIE_JH (SIREN 978827517), LEROY_MERLIN (SIRET
38456094201209).

**Entités à compléter (pas encore d'identifiant fort — apprentissage au prochain
passage via `complete_entry`, sans créer de doublon)** :

- **LMC** — seulement `lmcstore.com` ; TVA/SIRET à capter.
- **DISCAC** — seulement `discac.fr` ; TVA/SIRET à capter.
- **EDUARDO_LEITE_CORREIA_FERNANDES** — auto-entrepreneur (`no_vat`), identifié
  par nom.

**Entrées `to_review`** :

- **JARDINERIE_CHEVREUSE** — coordonnées issues d'une photo de faible qualité
  (`source_image`, `to_confirm`) : à valider sur une source nette.

_Aucune entité `auto_learned` pour l'instant : elles seront ajoutées à chaud dès
qu'une facture d'un émetteur inconnu sera traitée._

## Migration des fichiers existants — `scripts/reclasse_existant.py`

- **Dry-run par défaut** : produit le plan `ancien → nouveau` (et réécrit ce
  rapport) sans rien déplacer.
- **`--apply`** : crée les dossiers manquants et **déplace** les fichiers
  (jamais de suppression ; l'original n'est pas détruit).
- **Idempotence** : un fichier déjà correctement rangé est laissé en place.
- **Source d'identité** : le XML Factur-X **embarqué** dans chaque PDF (TVA
  vendeur/acheteur) → la règle d'or s'applique même si l'ancien dossier était
  erroné.

Exécution recommandée :

```bash
# 1) Plan (aucune modification)
DRIVE_FOLDER_ID=<racine> python scripts/reclasse_existant.py
# 2) Application après revue du plan
DRIVE_FOLDER_ID=<racine> python scripts/reclasse_existant.py --apply
```

## Garde-fous

- Le classement **ne fait jamais échouer** le pipeline : registre indisponible
  ou erreur ⇒ repli sur l'ancien schéma 2 niveaux ; incertitude ⇒ `_A_CLASSER`
  + log `WARNING`.
- Doublon (même fournisseur + n° de pièce, via le nom de fichier) ⇒ `INFO
  doublon ignoré`, pas de réécriture.
- Registre réécrit **atomiquement** (fichier temporaire + `os.replace`).
- `self` (VAT/SIRET) et racine Drive en configuration/env, pas en dur.

## Limitations connues

- Pour la **migration**, la contremarque n'est disponible que si elle figure
  dans le XML embarqué (BuyerReference) ou pour une vente JMT (destinataire) ;
  sinon le fichier migre sous `_A_CLASSER` (à reclasser manuellement ou après
  ré-OCR). Le pipeline en flux, lui, dispose de l'OCR complet.
- L'apprentissage utilise la partie non-`self` déjà extraite par Gemini (pas
  d'appel Gemini supplémentaire) ; un bloc émetteur totalement absent des
  données structurées reste `_A_CLASSER`.
