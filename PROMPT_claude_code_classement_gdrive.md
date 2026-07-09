# Mission — Affiner le rangement GDrive (mois / contremarque / fournisseur) + identification fiable de l'émetteur

## Périmètre
Complément du brief `PROMPT_claude_code_facturx.md` (qui traite CII/schematron/capture). **Ici, un seul sujet** : le nœud qui **range** le PDF Factur-X dans Google Drive, et l'**identification robuste** du fournisseur (émetteur) et de la contremarque (chantier). Ne touche pas à la génération CII ni au polling Gmail.

## Méthode imposée (NON négociable)
> Avant tout patch : **trace l'exécution ligne par ligne, identifie le point de divergence exact, PUIS patche.** Aucun patch spéculatif. Cite `fichier:ligne`.

Commence par localiser : (a) le nœud qui écrit sur Drive et construit le chemin, (b) là où sont extraits vendeur/acheteur/référence depuis la sortie Gemini. Confirme-les-moi avant de modifier.

---

## Contexte actuel
- Le renommage fonctionne : `{Fournisseur}_FacturX_{date}_{num}.pdf`.
- Le rangement actuel n'a **qu'un niveau** : le mois (`2026-07 Juillet/`).
- Objectif : passer à **3 niveaux** + une exception, avec une identification du fournisseur **certaine** (pas heuristique sur le nom).

## Arborescence cible
```
<AAAA-MM Mois>/                 ← inchangé (garde le format existant, ex. "2026-07 Juillet")
    <CONTREMARQUE>/             ← client final du chantier, JAMAIS "JMT DECO"
        <FOURNISSEUR>/          ← nom canonique du registre (ex. HACKER, IN-IPSO)
            <fichier>.pdf
```
**Exception franchiseur** — toute facture dont l'**émetteur** est **Raison Home** (le franchiseur, pas la simple mention d'enseigne) :
```
<AAAA-MM Mois>/Communication/<fichier>.pdf
```
**Exemple de référence à respecter exactement** : la facture Häcker n° `126181723`, réf. `FEUVRIER`, du 09/07/2026 →
`2026-07 Juillet/FEUVRIER/HACKER/HACKER_FacturX_2026-07-09_126181723.pdf`

---

## Cœur de la tâche : registre fournisseurs persistant + apprentissage

Un fichier `suppliers_registry.json` (seed fourni : `suppliers_registry.seed.json`) est la **source de vérité** de l'identité des fournisseurs. Charge-le au démarrage, enrichis-le à chaud, réécris-le atomiquement.

Schéma par entité (clé = nom canonique = nom du dossier) : `role` (`self` | `franchisor` | `supplier`), `legal_name`, `brand`, `vat[]`, `siret[]`, `email_domains[]`, `country`, `flags[]`, `note`. Voir le seed.

Au chargement, construis des **index inversés** en mémoire : `vat → canonical`, `siret → canonical`, `domain → canonical`.

### Règle d'or d'identification de l'émetteur
> **Le fournisseur = l'entité identifiée sur la facture dont l'identifiant fort (TVA intracom, puis SIRET) est ≠ celui de `self` (JMT Déco).**

C'est ce qui résout le **piège Häcker** : la facture affiche `FR41944684497` (JMT Déco = acheteur) **et** `DE174736262` (Häcker = vendeur). L'en-tête met « SARL JMT déco » en haut, mais c'est le destinataire. En prenant l'ID fort **qui n'est pas celui de self**, tu ne peux pas te tromper d'émetteur, quelle que soit la mise en page.

Ordre de résolution (déterministe → flou), s'arrêter au premier concluant :
1. Extraire **tous** les identifiants de la facture : n° TVA (regex `[A-Z]{2}\s?[0-9A-Z ]{2,13}`, normaliser sans espaces), SIRET (14 chiffres), SIREN (9), domaines d'email.
2. Retirer ceux de `self`. **Match TVA** sur l'index → canonical. Sinon **match SIRET/SIREN**.
3. Sinon **match domaine email** (hors domaines perso : gmail/hotmail/outlook/orange/wanadoo…).
4. Sinon **fuzzy sur `legal_name`/`brand`** (seuil élevé, ex. rapidfuzz ≥ 90).
5. Toujours rien → **apprentissage** : demander à Gemini un JSON strict `{legal_name, brand, vat, siret, email_domains, country}` à partir du bloc émetteur, générer un `canonical` (slug MAJUSCULE), **ajouter l'entrée** au registre avec `flags:["auto_learned","to_review"]`, et router. Loguer en `INFO` pour revue humaine.

Quand une entité connue apparaît avec un **identifiant fort nouveau** (ex. LMC/DISCAC pour lesquels le seed n'a que le domaine), **complète** l'entrée existante (append vat/siret) au lieu de créer un doublon. C'est ça, « apprendre au fur et à mesure les coordonnées complètes ».

---

## Identification de la contremarque (nom du dossier niveau 2)
La contremarque = **le client final du chantier**, jamais `self`. Les fournisseurs la logent dans des libellés variables — cherche-les dans cet ordre et prends le premier non vide :
`Contremarque`, `C/M`, `Référence`, `Référence client`, `Réf. commande client`, `V/Ref` / `V/REF`, sinon le **nom associé à l'adresse de chantier/travaux**.

Cas particuliers :
- **Facture de vente** (émetteur = `self`, ex. acomptes JMT Déco) : la contremarque = le **destinataire** (client particulier), et le fournisseur = `JMT_DECO`. Ex. `2026-07 Juillet/SCHWEITZER/JMT_DECO/…`.
- **Plusieurs contremarques** sur une facture (ex. Menuiserie JH : lignes « SAV GRAIRE » + « SAV MARTINEAU ») : route sur la **contremarque dominante** (montant le plus élevé) et loggue les autres ; ne duplique pas le fichier.
- Contremarque introuvable → dossier `_A_CLASSER/` sous le mois + log `WARNING` (ne bloque jamais).
- **Ne jamais** utiliser « RAISON HOME » comme contremarque : c'est l'enseigne de `self`.

---

## Cas particuliers à coder explicitement
- **Raison Home émetteur vs enseigne** : router vers `Communication/` **uniquement** si l'ID fort de l'émetteur = celui du franchiseur (`FR88428155956`/`42815595600058`). La chaîne « RAISON HOME » accolée à JMT Déco comme client ne déclenche PAS l'exception.
- **Avoir** (GPDIS, mention « AVOIR », montants négatifs) : se range normalement (mois/contremarque/fournisseur), mais préfixe le fichier par `AVOIR_` pour le distinguer.
- **Doublon** (même `fournisseur` + `n° pièce`) : ne pas réécrire ; si déjà présent, loguer `INFO doublon ignoré` et supprimer le second de la file.
- **Intracommunautaire** (Häcker : « Livraison intracommunautaire exonérée ») : n'affecte pas le chemin, mais vérifie que `flags` contient `intracommunity` (à défaut, l'ajouter au registre).

---

## Normalisation des noms de dossier (slugify)
Fonction unique et testée : MAJUSCULES, accents retirés (NFKD), espaces → `_`, caractères non alphanumériques supprimés sauf `_` et `-`. `"Häcker" → HACKER`, `"Aurélie SCHWEITZER" → SCHWEITZER` (garde le **nom de famille** pour la contremarque). Le nom canonique du registre prime sur le slug brut du fournisseur.

---

## Garde-fous & migration
1. **Dry-run par défaut** : un flag `--apply` requis pour déplacer/créer réellement. Sans lui, produire un **plan** (`ancien_chemin → nouveau_chemin`) dans un rapport.
2. **Idempotence** : relancer ne doit rien casser (détecter fichier déjà bien rangé).
3. **Migration** : script one-shot `scripts/reclasse_existant.py` qui applique la nouvelle arbo aux fichiers déjà sur Drive, en dry-run d'abord.
4. Ne jamais **supprimer** un original ; déplacer/copier uniquement. Toute action tracée.
5. Le rangement ne doit pas faire échouer le pipeline : toute incertitude → `_A_CLASSER/` + log, jamais une exception non gérée.

## Contraintes de code
Python 3.11+, mypy strict, docstrings Google, secrets via env. `self` (VAT/SIRET) et le chemin racine Drive en configuration/env, pas en dur. Écriture registre **atomique** (fichier temp + `os.replace`).

## Livrables
1. `supplier_registry.py` (chargement, index inversés, résolution émetteur, apprentissage, écriture atomique).
2. `classify.py` (calcul du triplet mois/contremarque/fournisseur + exception Communication + slugify).
3. `scripts/reclasse_existant.py` (migration dry-run/apply).
4. `suppliers_registry.json` initialisé depuis le seed.
5. Tests : résolution émetteur sur le **piège Häcker** (JMT en tête ⇒ vendeur = HACKER), exception Raison Home (émetteur vs enseigne), vente JMT ⇒ contremarque = client, doublon Leroy Merlin, avoir GPDIS. Fixtures = les 19 PDF + la facture Häcker `126181723`.
6. `RAPPORT_CLASSEMENT.md` : plan de rangement des fichiers connus, entités apprises, entrées `to_review`.

## Point de départ
Confirme-moi (a) le fichier/nœud d'écriture Drive et (b) le point d'extraction vendeur/acheteur. Puis implémente `supplier_registry.py` et valide d'abord le **piège Häcker** avant tout le reste.
