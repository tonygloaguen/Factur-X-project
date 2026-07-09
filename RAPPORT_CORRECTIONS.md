# RAPPORT_CORRECTIONS — Fiabilisation du pipeline Factur-X

Mission : faire passer le taux de succès réel de ~35 % à > 90 % sans régresser
l'existant, en traçant chaque divergence (fichier:ligne) avant tout patch.

Méthode : chaque lot = un commit conventionnel, validé contre le **schematron
officiel EN16931** (factur-x + saxonche, moteur Saxon réel) avant de continuer.

## Cartographie (point de départ)

| Rôle | Emplacement |
|------|-------------|
| Construction du XML CII (D16B) | `orchestrator/facturx_utils.py` → `generate_facturx_xml_en16931()` |
| Embedding PDF/A-3 + XSD/schematron | `orchestrator/facturx_utils.py` → `embed_facturx_in_pdf()` / lib `factur-x` |
| Normalisation EN16931 (pré-CII) | `orchestrator/facturx_utils.py` → `normalize_invoice_data()` |
| Validation officielle (tests) | `facturx.xml_check_xsd` / `facturx.xml_check_schematron` |

---

## P0 — `@unitCode is not allowed` (cause n°1, 252 occurrences)

**Divergence** — `facturx_utils.py:961` : `normalize_invoice_data` conservait
l'unité brute de Gemini (« m² », « UNI », « ml »…), écrite telle quelle dans
`BilledQuantity/@unitCode` (`facturx_utils.py:1140`). Le schematron rejette tout
code hors UN/ECE Rec 20.

**Fix** — `orchestrator/unece_units.py` : `to_unece()` déterministe (fallback
`C62`, ne lève jamais ; piège métier « ml » = mètre linéaire `MTR`, pas `MLT` ;
idempotence des codes déjà valides). Appelé dans `normalize_invoice_data` avant
le CII. Prompt Gemini : renvoyer l'unité telle qu'imprimée (le mapper convertit).

**Schematron avant/après** — factures IN-IPSO au m² (`FPA0002846`,
`FPA0002861`, `FAC0045917`) : ❌ `@unitCode is not allowed` → ✅ `unitCode="MTK"`
valide. Contrôle négatif inclus (unité brute « m² » → rejet).

Commit : `fix(cii): normaliser @unitCode vers UN/ECE Rec 20 avant construction du CII`

## PRÉREQUIS — Régression schematron silencieuse (dépendances)

**Divergence** — `requirements-ci.txt` et `orchestrator/requirements.txt`
déclaraient `factur-x>=3.10` **sans borne haute**. Depuis la dernière CI verte,
la contrainte flotte vers **factur-x 6.1**, qui a supprimé la validation
schematron en-process (saxonche) au profit d'un serveur Saxon HTTP externe
(`localhost:5000`). En son absence : `Skipping schematron check` + retour `True`
sans lever (`facturx.py:450`).

Conséquences : CI en échec (contrôles négatifs « DID NOT RAISE »), et surtout
**en production** l'embed n'aurait plus rejeté les XML invalides au prochain
rebuild d'image → factures invalides uploadées sans contrôle.

**Fix** — `factur-x>=4.0,<5` + `saxonche>=12.4` dans les deux fichiers de
dépendances. Vérifié : 4.x valide en-process et LÈVE sur BR-CO-25 ; 5.x/6.x
ignorent sans serveur.

Commit : `fix(deps): épingler factur-x <5 + saxonche pour restaurer la validation schematron`

## P1 — Cohérence des montants (BR-CO-13 / BR-S-08 / BR-CO-15)

**Divergence** — `facturx_utils.py:1024` (ventilation TVA recalculée seulement si
absente) et `facturx_utils.py:1049-1054` (totaux Gemini conservés sauf s'ils
valent 0). Des totaux non nuls mais incohérents franchissaient le CII.

**Fix** — recalcul intégral en `Decimal` (ROUND_HALF_UP, quantize 0.01) qui
**écrase** les agrégats de Gemini ; ventilation TVA toujours recomposée par
(code, taux) ; détection des lignes exprimées en TTC (Leroy Merlin chantier) et
reconversion en HT au taux ligne avant agrégation.

**Schematron avant/après** — totaux incohérents `999/1/1234` → `37,77/7,55/45,32`
valide ; lignes TTC → HT reconverties ; multi-taux 20 %/5,5 % → BR-S-08 OK.

Commit : `fix(cii): recalculer les totaux en Decimal et écraser les agrégats de Gemini`

## P2 — Factures jamais captées (5 cas) + traçabilité des échecs

**Divergence** — `main.py:62` (`_ensure_7d`) forçait `newer_than:7d` en dur :
toute facture > J-7 au démarrage devenait invisible pour toujours
(`BAUS-2026-05-000514`, 3× `RE…MARTINEAU`/Häcker). De plus, un échec dur laissait
l'email « nu » (aucun label) → perdu à la sortie de la fenêtre.

**Fix** —
- `main.py` : `_ensure_window` + `POLL_WINDOW_DAYS` (défaut **30**, paramétrable)
  et `GMAIL_CATCHUP=true` (rattrapage sans borne). Rétro-compat `GMAIL_ENFORCE_7D`.
- `nodes.py` : label `Factures-Erreur` sur échec dur (`_apply_error_label`,
  best-effort) → traçabilité + requête de reprise.
- `scripts/replay.py` : rejeu manuel `--message-id` / `--since` / `--query`
  (+ `--force`), hors fenêtre de polling.

Commit : `feat(collecte): fenêtre polling paramétrable, label Factures-Erreur, replay CLI`

## P3 — Robustesse extraction (type_facture / devise = null)

**Divergence** — `schemas.py:71-72` : `type_facture: str = "380"` / `devise: str
= "EUR"`. Le défaut Pydantic ne s'applique que si la clé est **absente**, pas si
elle vaut `None`. Gemini renvoie souvent `type_facture: null` (12 occ.) →
`model_validate` lève → `nodes.py:211` conserve les données brutes, perdant TOUTE
la coercition.

**Fix** — `type_facture: Literal["380","381","384","389"]` + validators `before`
(None/vide/inconnu → `380` ; devise None → `EUR`). Garde `or` en profondeur dans
`normalize_invoice_data`.

Commit : `fix(schema): tolérer type_facture/devise = null renvoyés par Gemini`

## P4 — Fournisseur étranger / autoliquidation (BR-CO-26, BR-S-02, BR-AE)

**Divergence** — Häcker (`@haecker-kuechen.de`) et cas reverse charge : sans TVA
vendeur (BT-31) ni SIRET, le schematron rejette (BR-CO-26 / BR-S-02 / BR-AE-02).
Le pipeline produisait alors un XML invalide au lieu de router explicitement.

**Fix** —
- Prompt Gemini renforcé : extraire la TVA vendeur AVEC préfixe pays (chercher
  `USt-IdNr`, `VAT`, `TVA`) et la TVA acheteur en cas d'autoliquidation.
- `party_identification_error()` : détecte le défaut d'identification (mêmes
  règles que le schematron) et route en `Factures-Erreur`
  (`erreur_identification:…`) AVANT de produire un XML invalide.

**Schematron avant/après** — fournisseur DE avec TVA → ✅ valide ; autoliquidation
(TVA vendeur + SIRET acheteur) → ✅ valide ; vendeur sans identifiant → flag garde
= rejet schematron (BR-CO-26), aligné.

Commit : `fix(p4): router les factures non identifiables (BR-CO-26/BR-AE) en erreur`

---

## Couverture de tests

Validés contre le schematron **officiel** (factur-x 4.x + saxonche) en CI :
`test_facturx_schematron.py` (cibles P0 m²→MTK, P1 totaux, P4 étranger/AE +
contrôles négatifs). Logique pure (tourne partout) : `test_unece_units.py`,
`test_totals_reconciliation.py`, `test_type_facture_default.py`,
`test_gmail_window.py`, `test_error_label.py`, `test_party_identification.py`.
Tous ajoutés au job `validate-facturx`.

## Limitations connues (non implémentées)

- **Éco-participation (DEA/PMCB/DEEE) et escompte document-level** (P1) :
  nécessitent les structures CII `BG-20`/`BG-21` (allowances/charges au niveau
  document) absentes du générateur actuel. À traiter dans un lot dédié pour ne
  pas risquer l'ordre XSD des éléments. En l'état, ces montants doivent être
  portés en ligne de facture.
- **Conversion des pièces jointes image (jpg) via Tesseract** (P2, optionnel) :
  non implémentée ; `facture jardinerie.jpg` reste hors périmètre du filtre
  `filename:pdf`.
- **Golden set des 15 PDF réels** : les fixtures binaires ne sont pas versionnées
  dans le dépôt ; la non-régression est assurée par des factures synthétiques
  représentatives validées contre le schematron officiel.
