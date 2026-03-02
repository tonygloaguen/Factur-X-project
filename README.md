# Factur-X Automation — LangGraph + Gemini + Google APIs

> Automatisation complète du traitement des factures fournisseurs :
> **Gmail → OCR → Gemini → XML EN16931 → PDF/A-3 → Google Drive + Matrice de suivi**
>
> Architecture : **1 conteneur Docker**, **10 nœuds LangGraph**, **Pure Python**

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-orange)
![Factur--X](https://img.shields.io/badge/Factur--X-EN16931-green)
![Docker](https://img.shields.io/badge/Docker-Compose-blue?logo=docker)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

---

## Sommaire

1. [Présentation du projet](#1-présentation-du-projet)
2. [Architecture technique](#2-architecture-technique)
3. [LangGraph — Concepts clés et pédagogie](#3-langgraph--concepts-clés-et-pédagogie)
4. [Les 10 nœuds du workflow](#4-les-10-nœuds-du-workflow)
5. [Installation locale depuis le repo](#5-installation-locale-depuis-le-repo)
6. [Configuration Google OAuth2 (credentials)](#6-configuration-google-oauth2-credentials)
7. [Lancement et vérification](#7-lancement-et-vérification)
8. [Déploiement sur un nouveau poste](#8-déploiement-sur-un-nouveau-poste)
9. [Commandes utiles et dépannage](#9-commandes-utiles-et-dépannage)
10. [CI/CD et tests](#10-cicd-et-tests)
11. [Évolutions futures](#11-évolutions-futures)

---

## 1. Présentation du projet

### Problème résolu

Les factures fournisseurs arrivent en PDF par email. Les traiter manuellement (vérifier, classer, extraire les données, créer les entrées comptables) est répétitif et source d'erreurs.

### Solution

Un agent autonome surveille une boîte Gmail, détecte les PDFs de factures, les analyse par IA et génère des fichiers **Factur-X EN16931** — le format européen de facture électronique structurée (PDF + XML embarqué). Les fichiers sont archivés dans Google Drive dans des sous-dossiers mensuels.

### Ce que le projet démontre

| Compétence | Implémentation |
|---|---|
| **LangGraph** | Workflow orienté graphe, 10 nœuds, routage conditionnel |
| **Gemini API** | Extraction structurée JSON depuis PDF OCR |
| **Google APIs** | Gmail OAuth2, Drive v3, Sheets v4, gestion des tokens |
| **Standard Factur-X** | XML CII D16B, profil EN16931, PDF/A-3b |
| **Python avancé** | TypedDict, SQLite WAL, backoff exponentiel, injection de dépendances |
| **Docker** | Conteneur autonome, volumes persistants, restart policy |

---

## 2. Architecture technique

### Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────────┐
│  Docker Container  ·  orchestrator                              │
│                                                                 │
│  main.py ──► boucle polling Gmail (toutes les 15 min)          │
│     │                                                           │
│     └──► Pour chaque PDF détecté :                             │
│             workflow.invoke(état_initial)                       │
│                  │                                              │
│                  ▼  LangGraph exécute les 10 nœuds             │
│       ┌──────────────────────────────────┐                      │
│       │  extract_text → filter_document  │                      │
│       │  → call_gemini → normalize_data  │                      │
│       │  → generate_xml → embed_facturx  │                      │
│       │  → upload_drive → update_matrix  │                      │
│       │  → label_gmail  → log_result     │                      │
│       └──────────────────────────────────┘                      │
│                                                                 │
│  Volumes persistants :                                          │
│    orchestrator/credentials.json  ← OAuth credentials (lecture) │
│    orchestrator/token.json        ← Token OAuth2 (lecture/écriture) │
│    orchestrator_data/state.db     ← SQLite anti-retraitement    │
└─────────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
    Gmail API            Gemini API           Drive API
   (OAuth2)            (REST Gemini)          (OAuth2)
```

### Refactoring de l'architecture

Ce projet est passé de **2 conteneurs** (orchestrateur + micro-service Flask HTTP) à **1 conteneur autonome** :

| | Avant | Après |
|---|---|---|
| Conteneurs | 2 (orchestrator + facturx-service) | 1 (orchestrator) |
| Communication | HTTP interne (appels REST) | Appels Python directs |
| Nœuds LangGraph | 4 nœuds | 9 nœuds |
| Logique métier | Dispersée dans 2 services | Centralisée dans `facturx_utils.py` |
| Complexité opérationnelle | Plus élevée (2 builds, 2 logs) | Simple (1 build, 1 log) |

### Structure des fichiers

```
Factur-X-project/
├── docker-compose.yml              ← 1 service, toutes les variables d'env
├── .env                             ← Secrets locaux (gitignored)
│
├── orchestrator/
│   ├── Dockerfile                  ← Image Python 3.12-slim
│   ├── requirements.txt            ← LangGraph, Google APIs, Gemini, etc.
│   │
│   ├── main.py                     ← Point d'entrée : boucle polling Gmail
│   ├── graph.py                    ← Topologie du graphe (nœuds + arêtes)
│   ├── nodes.py                    ← Les 10 nœuds + 2 routeurs
│   ├── state.py                    ← TypedDict InvoiceState (état partagé)
│   ├── services.py                 ← GoogleServices + StateDB (SQLite)
│   └── facturx_utils.py            ← Fonctions métier pures (OCR, XML, PDF)
│
├── orchestrator_data/
│   └── state.db                    ← SQLite anti-retraitement (gitignored)
│
├── tests/
│   ├── test_facturx_en16931.py     ← Tests de conformité XML EN16931
│   └── test_gmail_integration_invoice.py
│
└── tools/
    └── ci_validate_facturx.py      ← Validation CI GitHub Actions
```

---

## 3. LangGraph — Concepts clés et pédagogie

### Qu'est-ce que LangGraph ?

LangGraph est un framework Python de **LangChain** pour construire des agents et workflows complexes sous forme de **graphes orientés**. Contrairement à une suite d'appels de fonctions séquentiels, LangGraph modélise explicitement :

- **Les nœuds** : unités de traitement (fonctions Python pures)
- **Les arêtes** : les chemins possibles entre nœuds
- **L'état** : une mémoire partagée passée entre tous les nœuds

### Concept 1 — L'État (State)

L'état est un `TypedDict` Python qui joue le rôle de **mémoire partagée** entre tous les nœuds. C'est la seule façon pour les nœuds de se transmettre des données.

```python
# state.py
class InvoiceState(TypedDict):
    # Données d'entrée (injectées avant invoke)
    message_id: str
    pdf_bytes: bytes
    pdf_filename: str

    # Enrichi au fil des nœuds
    ocr_text: str           # → par extract_text
    invoice_data: dict      # → par call_gemini (puis normalize_data)
    xml_bytes: bytes        # → par generate_xml
    facturx_pdf: bytes      # → par embed_facturx
    drive_file_url: str     # → par upload_drive

    # Gestion des erreurs
    processing_error: str   # Toute valeur non-vide = erreur upstream
```

**Règle fondamentale :**
```
nœud(état_complet) → dict_partiel
état_suivant = {**état_actuel, **dict_partiel}
```

Chaque nœud ne retourne que les champs qu'il modifie. LangGraph fusionne automatiquement.

### Concept 2 — Les Nœuds (Nodes)

Un nœud est une **fonction Python ordinaire** :

```python
def node_extract_text(state: InvoiceState) -> dict:
    # 1. Lire ce dont on a besoin
    pdf_bytes = state["pdf_bytes"]

    # 2. Faire le travail
    ocr_text = extract_text_from_pdf(pdf_bytes)

    # 3. Retourner UNIQUEMENT les champs modifiés
    return {"ocr_text": ocr_text}
```

**Pattern — Guard Clause :** la plupart des nœuds commencent par vérifier `processing_error`. Si une erreur upstream est détectée, le nœud ne fait rien et retourne `{}`. Cela permet d'avoir des arêtes directes simples même en cas d'erreur :

```python
def node_generate_xml(state: InvoiceState) -> dict:
    if state.get("processing_error"):
        return {}  # Erreur upstream → on court-circuite
    # ... traitement normal
```

### Concept 3 — Le Graphe et les Arêtes

```python
# graph.py
graph = StateGraph(InvoiceState)

# Enregistrer les nœuds
graph.add_node("extract_text", node_extract_text)
graph.add_node("filter_document", node_filter_document)
graph.add_node("call_gemini", node_call_gemini)
# ...

# Arête de départ
graph.set_entry_point("extract_text")

# Arêtes directes (chemin normal)
graph.add_edge("extract_text", "filter_document")

# Arête conditionnelle (branchement dynamique)
graph.add_conditional_edges(
    "filter_document",
    route_after_filter,          # Fonction routeur
    {
        "call_gemini": "call_gemini",    # Si candidat facture
        "log_result":  "log_result",     # Si rejeté (économise le quota Gemini)
    }
)

workflow = graph.compile()
```

### Concept 4 — Les Routeurs (Edge Functions)

Un routeur est une fonction qui reçoit l'état et retourne le **nom du nœud suivant** :

```python
def route_after_filter(state: InvoiceState) -> str:
    if state.get("processing_error"):
        return "log_result"   # Court-circuit → log direct
    return "call_gemini"      # Chemin normal → appel Gemini
```

### Concept 5 — Injection et Invocation

```python
# main.py — Pour chaque PDF trouvé dans Gmail
initial_state: InvoiceState = {
    "message_id": msg_id,
    "pdf_bytes": pdf_data,
    "pdf_filename": "facture.pdf",
    "subject": "...",
    "sender": "...",
    # ... autres champs initiaux
    "processing_error": "",   # Pas d'erreur au départ
    "services": google_services,  # Injection de dépendances
    "state_db": sqlite_db,
}

# LangGraph exécute les 9 nœuds dans l'ordre défini
workflow.invoke(initial_state)
```

### Pourquoi LangGraph plutôt qu'une liste de fonctions ?

| Critère | Liste de fonctions | LangGraph |
|---|---|---|
| Branchement conditionnel | `if/else` mélangé dans la logique | Routeurs explicites dans le graphe |
| Visualisation | Aucune | DAG visualisable |
| Testabilité | Difficile (effets de bord) | Chaque nœud testable isolément |
| Checkpointing | À implémenter manuellement | Intégré (sauvegarde/reprise d'état) |
| Parallélisme | Manuel | Nœuds indépendants en `//` natif |
| Extensibilité | Modifier le code existant | Ajouter un nœud + une arête |

---

## 4. Les 10 nœuds du workflow

```
                     ┌──────────────┐
                     │ extract_text │  OCR : PyMuPDF natif ou Tesseract fallback
                     └──────┬───────┘
                            │
                     ┌──────▼──────────┐
                     │ filter_document │  Mots-clés facture : GRATUIT, sans Gemini
                     └──────┬──────────┘
             ┌──────────────┤
   [rejeté / erreur]   [candidat facture]
             ↓              ↓
        log_result   ┌─────▼──────────┐
           (END)     │  call_gemini   │  IA : extraction JSON structuré (quota)
                     └──────┬─────────┘
             ┌──────────────┤
   [pas facture / 429]  [est une facture]
             ↓              ↓
        log_result   ┌──────▼──────────┐
           (END)     │ normalize_data  │  Garantit conformité EN16931 (fallbacks)
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │  generate_xml   │  XML CII D16B/D22B (standard Factur-X)
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │  embed_facturx  │  PDF/A-3b + XML embarqué
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │  upload_drive   │  Sous-dossier mensuel "2026-02 Février"
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │ update_matrix   │  Coche "X" dans la matrice Excel Drive
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │   label_gmail   │  Label "Factures-Traitées" sur l'email
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │   log_result    │  SQLite + logs — TOUJOURS exécuté
                     └──────┬──────────┘
                            │
                           END
```

| # | Nœud | Rôle | Coût |
|---|---|---|---|
| 1 | `extract_text` | OCR du PDF (PyMuPDF natif, Tesseract en fallback) | Gratuit |
| 2 | `filter_document` | Filtrage par mots-clés facture avant appel IA | Gratuit |
| 3 | `call_gemini` | Extraction JSON structuré via Gemini Flash | Quota API |
| 4 | `normalize_data` | Normalisation + fallbacks pour conformité EN16931 | Gratuit |
| 5 | `generate_xml` | Génération XML CII D16B/D22B | Gratuit |
| 6 | `embed_facturx` | Assemblage PDF/A-3b avec XML embarqué | Gratuit |
| 7 | `upload_drive` | Upload dans sous-dossier Drive mensuel | Gratuit |
| 8 | `update_matrix` | Coche "X" fournisseur/mois dans `Suivi_Transmission_Factures_Comptable.xlsx` | Gratuit |
| 9 | `label_gmail` | Application du label sur l'email source | Gratuit |
| 10 | `log_result` | Écriture SQLite + logs structurés | Gratuit |

### Nœud 8 — `update_matrix` : matrice de suivi fournisseurs

Après chaque upload réussi sur Drive, ce nœud met à jour automatiquement la feuille Excel de suivi `Suivi_Transmission_Factures_Comptable.xlsx` hébergée sur Google Drive.

**Structure de la matrice :**
```
| Client     | Référence facture | Octobre 2025 | Novembre 2025 | Mars 2026 | ...
|------------|-------------------|--------------|---------------|-----------|
| GARNIER    | GPDIS             |              |       X       |           |
| BAUDE      | MSA               |       X      |               |           |
| WHHITECHURCH | INTERBAT        |              |       X       |           |
```

**Logique de matching :**
- **Colonne du mois** : recherche l'en-tête `"Mois AAAA"` (ex: `"Mars 2026"`) basée sur la date de la facture
- **Ligne fournisseur** : col A = nom du client (acheteur), col B = nom du fournisseur (vendeur)
- Correspondance **partielle et insensible** à la casse et aux accents (`"MSA FRANCE"` trouve `"MSA"`)
- **Non-bloquant** : si la ligne n'est pas trouvée ou si l'API Sheets échoue, un `[WARN]` est loggé et le workflow continue normalement

### Anti-retraitement SQLite

La base SQLite (`orchestrator_data/state.db`) enregistre chaque couple `(message_id, filename)` traité. À chaque cycle de polling, les PDFs déjà vus sont sautés **avant** tout appel IA — économisant le quota Gemini.

```
Statuts SQLite :
  success              → traitement complet réussi
  not_invoice          → rejeté avant Gemini (filtre mots-clés)
  not_invoice_gemini   → rejeté par Gemini après analyse
  error                → erreur technique (Drive, Gmail...)
  (pas de ligne)       → rate_limit_429 → sera retenté au prochain cycle
```

---

## 5. Installation locale depuis le repo

### Prérequis

- **Git**
- **Docker Desktop** (Windows/Mac) ou **Docker Engine** (Linux)
- **Python 3.10+** (uniquement pour générer le token OAuth, pas pour faire tourner le projet)

### Étape 1 — Cloner le repo

```bash
git clone https://github.com/tonygloaguen/Factur-X-project.git
cd Factur-X-project
```

> `credentials.json` et `token.json` sont dans `.gitignore`. Ils ne sont jamais versionnés.
> Vous devrez les créer manuellement (voir section 6).

### Étape 2 — Créer le fichier `.env`

Créez un fichier `.env` à la racine du projet (au même niveau que `docker-compose.yml`) :

```ini
# ── IA Gemini ────────────────────────────────────────────────────
# Clé API gratuite : https://aistudio.google.com/apikey
GEMINI_API_KEY=votre_cle_gemini_ici

# Modèle Gemini (gemini-2.5-flash recommandé — rapide et bon marché)
GEMINI_MODEL=gemini-2.5-flash

# Profil Factur-X — DOIT être exactement "en16931" (minuscules)
FACTURX_PROFILE=en16931

# ── Google Drive ─────────────────────────────────────────────────
# ID du dossier Drive racine (PAS l'URL entière, uniquement l'ID)
# Exemple d'URL : https://drive.google.com/drive/folders/1cPFMtFhN-VOnPJ...
# → DRIVE_FOLDER_ID=1cPFMtFhN-VOnPJ...   (sans le ?usp=drive_link)
DRIVE_FOLDER_ID=identifiant_dossier_drive

# ID Google Sheets de la matrice de suivi fournisseurs
# Fichier : Suivi_Transmission_Factures_Comptable.xlsx
# URL     : https://docs.google.com/spreadsheets/d/[CET_ID]/edit
DRIVE_MATRIX_FILE_ID=1drSsQQVtgniDLg5vHK2jTTadP3EYgm-b

# ── Gmail polling ─────────────────────────────────────────────────
# Intervalle de polling en secondes (900 = 15 minutes)
POLL_INTERVAL=900

# Label appliqué aux emails une fois traités
GMAIL_LABEL=Factures-Traitées

# Requête Gmail (syntaxe Google Search)
GMAIL_QUERY=has:attachment filename:pdf -label:Factures-Traitées newer_than:7d
```

> **Points critiques :**
> - `FACTURX_PROFILE` : doit être `en16931` en minuscules exactement
> - `DRIVE_FOLDER_ID` : uniquement l'ID (la partie après `/folders/`), jamais l'URL complète
> - `DRIVE_MATRIX_FILE_ID` : l'ID dans l'URL `spreadsheets/d/[ID]/edit` — déjà pré-rempli avec le fichier actuel
> - Ne commitez jamais ce fichier (il est dans `.gitignore`)

### Étape 3 — Créer le répertoire de données

```bash
# Linux / Mac
mkdir -p orchestrator_data

# Windows (PowerShell)
New-Item -ItemType Directory -Force -Path orchestrator_data
```

Ce répertoire contiendra la base SQLite montée en volume dans le conteneur.

---

## 6. Configuration Google OAuth2 (credentials)

> **Opération unique par compte Google.** Le token généré se renouvelle automatiquement.

### 6.1 — Créer un projet Google Cloud

1. Allez sur [https://console.cloud.google.com/](https://console.cloud.google.com/)
2. Sélecteur de projet (en haut) → **Nouveau projet**
3. Nom : `Factures-Automatisation` → Créer

### 6.2 — Activer les APIs

1. Menu (☰) → **API et services** → **Bibliothèque**
2. Rechercher **Gmail API** → Activer
3. Rechercher **Google Drive API** → Activer

### 6.3 — Configurer l'écran de consentement OAuth

Dans **Google Auth Platform** (anciennement "Écran de consentement OAuth") :

1. **Branding** : Nom de l'app = `Factures-Auto`, email d'assistance = votre email
2. **Audience** : Type = **Externe**, ajoutez votre email en **utilisateur test**
3. **Accès aux données** (Scopes) : ajoutez ces trois scopes :
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/drive.file`
   - `https://www.googleapis.com/auth/spreadsheets`

### 6.4 — Créer les identifiants OAuth

1. **Google Auth Platform** → **Clients** → **+ Créer un client**
2. Type d'application : **Application de bureau**
3. Nom : `orchestrator-factures` → Créer
4. Téléchargez le fichier JSON → Renommez-le `credentials.json`
5. Placez-le dans `orchestrator/credentials.json`

```
Factur-X-project/
└── orchestrator/
    └── credentials.json   ← ici (jamais committé, dans .gitignore)
```

### 6.5 — Générer le token OAuth (première connexion)

Cette étape nécessite Python en local et ouvre un navigateur pour l'autorisation Google.

**Sur Windows (PowerShell) :**
```powershell
pip install google-api-python-client google-auth-oauthlib

cd C:\...\Factur-X-project\orchestrator

python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    ['https://www.googleapis.com/auth/gmail.modify',
     'https://www.googleapis.com/auth/drive.file',
     'https://www.googleapis.com/auth/spreadsheets']
)
creds = flow.run_local_server(port=8090, open_browser=True)
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print('Token sauvegarde dans token.json !')
"
```

**Sur Linux/Mac :**
```bash
pip install google-api-python-client google-auth-oauthlib

cd /chemin/vers/Factur-X-project/orchestrator

python3 -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    ['https://www.googleapis.com/auth/gmail.modify',
     'https://www.googleapis.com/auth/drive.file',
     'https://www.googleapis.com/auth/spreadsheets']
)
creds = flow.run_local_server(port=8090, open_browser=True)
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print('Token sauvegarde dans token.json !')
"
```

Le navigateur s'ouvre → Connectez-vous → Autorisez l'accès → `token.json` est créé.

### 6.6 — Vérification de la structure avant lancement

```
Factur-X-project/
├── .env                    ← créé à l'étape 5.2
├── docker-compose.yml
├── orchestrator_data/      ← créé à l'étape 5.3
└── orchestrator/
    ├── credentials.json    ← téléchargé à l'étape 6.4
    ├── token.json          ← généré à l'étape 6.5
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py
    ├── graph.py
    ├── nodes.py
    ├── state.py
    ├── services.py
    └── facturx_utils.py
```

> **Piège fréquent sur Windows :** vérifiez que `credentials.json` est un **fichier** et non un **dossier** :
> ```powershell
> dir orchestrator\credentials.json
> # Attendu : -a----   (fichier)
> # Erreur  : d-----   (dossier — double-clic au lieu de téléchargement)
> ```

---

## 7. Lancement et vérification

### Premier démarrage

```bash
cd Factur-X-project
docker compose up -d --build
```

Le build prend 2-3 minutes (téléchargement des dépendances Python).

### Vérifier l'état

```bash
docker compose ps
```

Résultat attendu :
```
NAME           STATUS    PORTS
orchestrator   Up
```

### Vérifier les logs au démarrage

```bash
docker compose logs -f orchestrator
```

Logs normaux attendus :
```
2026-02-26 10:00:00 [INFO] ============================================================
2026-02-26 10:00:00 [INFO] Orchestrateur LangGraph — Factures fournisseurs
2026-02-26 10:00:00 [INFO] ============================================================
2026-02-26 10:00:01 [INFO] StateDB ouverte : /app/data/state.db
2026-02-26 10:00:02 [INFO] Connexion Google OK
2026-02-26 10:00:02 [INFO] Graphe LangGraph compilé (10 nœuds)
2026-02-26 10:00:02 [INFO] Démarrage de la boucle de polling...
2026-02-26 10:00:03 [INFO] Aucun nouvel email avec facture détecté
2026-02-26 10:00:03 [INFO] Prochaine vérification dans 900 secondes...
```

### Tester avec une facture réelle

1. Envoyez-vous un email avec une facture PDF en pièce jointe
2. Forcez un scan immédiat : `docker compose restart orchestrator`
3. Suivez les logs : `docker compose logs -f orchestrator`

Logs de traitement attendus :
```
[INFO] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[INFO] Nouvel email : 'Facture EDF Février 2026' de noreply@edf.fr
[INFO] [ 1/10] extract_text : facture_edf.pdf (142 Ko)
[INFO] [ 2/10] filter_document : candidat facture (score 8/10)
[INFO] [ 3/10] call_gemini : appel API...
[INFO] [ 4/10] normalize_data : 3 lignes, TVA 20%, TTC 245.60€
[INFO] [ 5/10] generate_xml : XML EN16931 généré (4821 bytes)
[INFO] [ 6/10] embed_facturx : PDF/A-3b créé (187 Ko)
[INFO] [ 7/10] upload_drive : → 2026-02 Février/
[INFO] [ 8/10] update_matrix : X écrit en E12 (fournisseur=EDF, client=GARNIER, mois=Février 2026)
[INFO] [ 9/10] label_gmail : label 'Factures-Traitées' appliqué
[INFO] ✅ Succès : EDF | INV-2026-02-001 | 245.60 € TTC → https://drive.google.com/...
```

---

## 8. Déploiement sur un nouveau poste

### Prérequis sur le poste cible

| Logiciel | Obligatoire | Usage |
|---|---|---|
| Git | Oui | Cloner le repo |
| Docker Desktop (Windows/Mac) ou Docker Engine (Linux) | Oui | Faire tourner le conteneur |
| Python 3.10+ | Oui (une fois) | Générer le token OAuth |
| Connexion Internet permanente | Oui | Gmail, Drive, Gemini APIs |

### Option A — Via GitHub (recommandé)

```bash
# 1. Cloner le repo
git clone https://github.com/tonygloaguen/Factur-X-project.git
cd Factur-X-project

# 2. Créer le .env (voir section 5.2)
# 3. Placer credentials.json dans orchestrator/ (voir section 6.4)
# 4. Générer token.json (voir section 6.5)

# 5. Créer le répertoire de données
mkdir -p orchestrator_data       # Linux/Mac
New-Item -ItemType Directory -Force -Path orchestrator_data   # Windows

# 6. Lancer
docker compose up -d --build
```

### Option B — Via archive ZIP (poste sans accès GitHub)

**Sur le poste source :**
```powershell
# Windows PowerShell
Compress-Archive -Path C:\...\Factur-X-project\* -DestinationPath C:\Factur-X-backup.zip -Force
```

**Sur le poste cible :**
```powershell
# Extraire
Expand-Archive -Path C:\Factur-X-backup.zip -DestinationPath C:\Factur-X-project -Force

cd C:\Factur-X-project

# Supprimer l'ancien token (invalide sur le nouveau poste)
del orchestrator\token.json

# Régénérer le token (voir section 6.5) puis lancer
docker compose up -d --build
```

> `token.json` est lié à une session d'autorisation spécifique.
> Il doit être régénéré sur chaque nouveau poste, même si `credentials.json` est identique.

### Redémarrage automatique avec le PC

Le conteneur est configuré avec `restart: unless-stopped`. Il redémarre automatiquement quand Docker redémarre.

**Windows (Docker Desktop) :**
- Settings → General → Cocher **"Start Docker Desktop when you sign in to your computer"**

**Linux (systemd) :**
```bash
sudo systemctl enable docker
```

Cycle quotidien normal :
1. Vous allumez le PC
2. Docker Desktop démarre automatiquement
3. Le conteneur `orchestrator` redémarre automatiquement
4. Le polling Gmail reprend — rien à faire

> `docker compose down` **supprime** les conteneurs → pas de restart automatique au boot.
> Réservez cette commande à la maintenance. Pour l'usage quotidien, éteignez le PC normalement.

---

## 9. Commandes utiles et dépannage

### Commandes du quotidien

```bash
docker compose ps                              # État du conteneur
docker compose logs -f orchestrator            # Logs en temps réel
docker compose logs --tail 50 orchestrator     # 50 dernières lignes
docker compose restart orchestrator            # Forcer un scan immédiat
```

### Gérer le cycle de vie

```bash
docker compose stop                # Arrête (conservé → restart au boot)
docker compose start               # Redémarre après un stop
docker compose down                # ⚠️ Supprime le conteneur
docker compose up -d               # Recrée après un down
docker compose up -d --build       # Reconstruit l'image (après modif du code)
```

### Après modification du code

```bash
docker compose up -d --build
```

### Après modification du `.env`

```bash
docker compose up -d   # Pas besoin de --build
```

### Statistiques SQLite

```bash
docker compose exec orchestrator python3 -c "
import sqlite3
conn = sqlite3.connect('/app/data/state.db')
for row in conn.execute('SELECT status, COUNT(*) FROM processed GROUP BY status'):
    print(row)
"
```

### Nettoyage disque

```bash
docker system prune -f    # Supprime images/conteneurs orphelins
```

### Dépannage fréquent

**Le conteneur redémarre en boucle :**
```bash
docker compose logs --tail 30 orchestrator
```
Causes courantes :
- `credentials.json` absent ou invalide (vérifier que c'est un fichier, pas un dossier)
- `token.json` absent → lancer la procédure OAuth (section 6.5)
- `DRIVE_FOLDER_ID` mal configuré dans `.env` (URL au lieu de l'ID seul)
- `GEMINI_API_KEY` manquant ou invalide

**Token Google expiré ou scopes insuffisants (erreur 403 sur Sheets) :**
```bash
# Linux/Mac
rm orchestrator/token.json
# Windows
del orchestrator\token.json
```
Régénérer le token (section 6.5) en incluant bien les 3 scopes (gmail, drive, spreadsheets) puis relancer `docker compose up -d`.

> Si vous avez ajouté le scope `spreadsheets` après une première utilisation, l'ancien `token.json`
> ne contient pas ce scope. Supprimez-le et régénérez-le pour que la mise à jour de la matrice fonctionne.

**Erreur 429 Gemini (rate limit) :**
Le backoff exponentiel est intégré. L'email sera retenté au cycle suivant (rien n'est inscrit en SQLite pour ce cas). Si le problème persiste, réduire `MAX_EMAILS_PER_CYCLE` dans `.env`.

**`DRIVE_FOLDER_ID` incorrect :**
L'ID est uniquement la chaîne après `/folders/` dans l'URL Drive, sans le `?usp=drive_link`.

---

## 10. CI/CD et tests

Le workflow GitHub Actions (`.github/workflows/validate-facturx.yml`) s'exécute sur chaque push et pull request :

```
push / PR
    │
    ├── Validate PDF/A avec veraPDF (si des PDFs de test sont présents)
    └── Validate embedded Factur-X XML (tools/ci_validate_facturx.py)
         └── Vérifie : namespace CII, champs obligatoires EN16931,
                       validité du XML embarqué dans les PDFs de test
```

**Lancer les tests localement :**
```bash
pip install pypdf lxml pytest
pytest tests/test_facturx_en16931.py -v
```

---

## 11. Évolutions futures

LangGraph permet d'ajouter des nœuds sans modifier les existants :

**Court terme**
- Enrichissement SIRET via API INSEE (nouveau nœud entre `normalize_data` et `generate_xml`)
- Détection de doublons Drive (nouveau nœud avant `upload_drive`)
- Coloration conditionnelle de la cellule dans la matrice (vert si reçu dans les délais)

**Moyen terme**
- Agent de relance fournisseurs (factures impayées après J+30)
- Rapprochement bancaire automatisé
- Multi-comptes Gmail (plusieurs instances orchestrateur)

**Long terme**
- Profil Factur-X **EXTENDED** (données supplémentaires sectorielles)
- Interface web de supervision (dashboard React + API FastAPI)
- Déploiement cloud (GCP Cloud Run, AWS ECS)

---

## Licence

MIT — Voir [LICENSE](LICENSE)

---

*Stack : Python 3.12 · LangGraph 0.2 · Gemini 2.5 Flash · Factur-X EN16931 · Docker*
