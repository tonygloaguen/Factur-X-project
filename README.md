# Runbook : Workflow Factures LangGraph — Guide complet

*Version 4.0 — 25 février 2026*
*Refactoring : architecture unifiée LangGraph pur — 1 conteneur, 9 nœuds, 0 microservice HTTP*

---

## Ce document, c'est quoi ?

Un **runbook**, c'est un guide pas-à-pas qu'on suit dans l'ordre pour réaliser une opération technique. Celui-ci couvre :

1. **Comprendre** l'architecture
2. **Nettoyer** l'ancienne stack
3. **Installer** la nouvelle stack LangGraph pure
4. **Configurer** Google OAuth (Gmail + Drive)
5. **Tester** et valider le workflow
6. **Sauvegarder** la configuration finale (export Docker)
7. **Déployer** sur un autre poste (import Docker)
8. **Commandes utiles** et dépannage
9. **Évolutions futures**

---

## Partie 1 — Comprendre l'architecture

### Le workflow en une phrase

Un email arrive dans Gmail avec un PDF → l'orchestrateur le détecte → il extrait le texte par OCR → filtre localement si ce n'est pas une facture → appelle Gemini pour structurer les données EN16931 → génère un PDF Factur-X (XML embarqué, PDF/A-3b) → l'uploade dans Google Drive dans le bon dossier mensuel → met un label sur l'email.

### Architecture — 1 conteneur (LangGraph pur)

```
orchestrator    → graphe LangGraph : polling Gmail + pipeline complet (OCR, Gemini, XML, Drive)
```

RAM totale : ~200 Mo. Tout en Python. Pas d'appel HTTP interne. Debug facile avec des logs lisibles.

**Avant (v3)** : 2 conteneurs — orchestrateur + micro-service Flask HTTP
**Après (v4)** : 1 conteneur — graphe LangGraph avec 9 nœuds granulaires

### Comment ça communique

```
Internet
   │
   ├── API Gmail      ◄──────── orchestrator (LangGraph)
   ├── API Drive      ◄────────     9 nœuds Python
   └── API Gemini     ◄────────     (OCR, IA, XML, PDF, Drive, Gmail)
```

Plus de réseau interne Docker, plus de port 5000, plus de dépendance HTTP entre services.

### Le graphe LangGraph (9 nœuds)

```
extract_text            ← OCR du PDF (natif PyMuPDF ou Tesseract en fallback)
    │
    ▼
filter_document         ← Filtrage keywords local (gratuit, avant tout appel IA)
    │
    ├── (non-facture ou OCR vide) ──────────────────────────────────► log_result → FIN
    │
    ▼
call_gemini             ← Extraction structurée JSON (EN16931) via API Gemini
    │
    ├── (non-facture confirmé ou rate limit 429) ──────────────────► log_result → FIN
    │
    ▼
normalize_data          ← Garantit la conformité EN16931 (adresses, lignes, totaux)
    │
    ▼
generate_xml            ← Génère le XML CII D16B/D22B (format Factur-X)
    │
    ▼
embed_facturx           ← Embarque l'XML dans le PDF → PDF/A-3b
    │
    ▼
upload_drive            ← Crée le sous-dossier mensuel, uploade le PDF Factur-X
    │
    ▼
label_gmail             ← Ajoute le label "Factures-Traitées" sur l'email
    │
    ▼
log_result              ← Écrit dans SQLite + log console (toujours exécuté)
```

**Court-circuit économique** : `filter_document` rejette les non-factures évidentes SANS appeler Gemini (économie de quota). Gemini n'est appelé que sur les documents candidats.

### Structure des fichiers de l'orchestrateur

```
orchestrator/
├── main.py          ← Point d'entrée : boucle de polling Gmail (thin entry point)
├── graph.py         ← Topologie du graphe (nœuds + arêtes — "le câblage")
├── nodes.py         ← 9 nœuds LangGraph + 2 fonctions de routage
├── facturx.py       ← Fonctions pures : OCR, Gemini, XML EN16931, PDF/A-3b
├── services.py      ← GoogleServices (Gmail + Drive) + StateDB (SQLite)
├── state.py         ← InvoiceState TypedDict (état partagé entre les nœuds)
├── Dockerfile
└── requirements.txt
```

### Profil Factur-X EN16931 — ce qui est généré

Le profil EN16931 est conforme à la norme européenne EN 16931. Il contient :

- **Lignes de facture** (obligatoire BR-16) : description, quantité, prix unitaire, TVA par ligne
- **Adresse vendeur** complète avec code pays (BR-08, BR-09)
- **Adresse acheteur** complète avec code pays (BR-10, BR-11)
- **Ventilation TVA** par catégorie : base HT, montant TVA, taux, code (BG-23)
- **Totaux complets** : somme lignes, HT, TVA, TTC, montant dû (BG-22)
- **Moyens de paiement** : code, IBAN, BIC si disponibles (BG-16)
- **Date d'échéance** si disponible

Le nœud `normalize_data` complète automatiquement les données manquantes (ligne de fallback, adresses par défaut, recalcul des totaux).

---

## Partie 2 — Nettoyage de l'ancienne stack

*Sauter cette partie si tu pars d'un poste vierge ou si tu migres depuis la v4.*

### Si tu migres depuis la v3 (2 conteneurs)

```powershell
cd C:\Automatisch
docker compose down
docker rmi automatisch-facturx-service automatisch-orchestrator
```

Si des commandes disent "No such container/image", c'est normal.

### Si tu pars d'Automatisch (stack legacy)

```powershell
docker rm -f automatisch-main automatisch-worker automatisch-db automatisch-redis facturx-service
docker volume rm automatisch_automatisch-db-data
docker network rm automatisch_automatisch-net
docker rmi automatischio/automatisch:latest postgres:16-alpine redis:7-alpine
```

### Vérification

```powershell
docker ps -a          # Doit être vide
docker volume ls      # Pas de volume automatisch
```

---

## Partie 3 — Installation de la nouvelle stack

### Structure des fichiers

```
C:\Automatisch\
├── docker-compose.yml              ← 1 service (orchestrator uniquement)
├── .env                             ← Variables d'environnement
├── orchestrator\
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                      ← Point d'entrée (polling Gmail)
│   ├── graph.py                     ← Topologie du graphe LangGraph
│   ├── nodes.py                     ← 9 nœuds + 2 routeurs
│   ├── facturx.py                   ← Pipeline OCR + Gemini + XML + PDF
│   ├── services.py                  ← Google OAuth2 + SQLite
│   ├── state.py                     ← État partagé (TypedDict)
│   ├── credentials.json             ← Identifiants OAuth Google
│   └── token.json                   ← Token d'accès (créé automatiquement)
└── orchestrator_data\
    └── state.db                     ← Base SQLite anti-retraitement
```

### Contenu du .env

```ini
# --- IA Gemini ---
GEMINI_API_KEY=ta_clé_gemini_ici
GEMINI_MODEL=gemini-2.5-flash
FACTURX_PROFILE=en16931

# --- Orchestrateur LangGraph ---
DRIVE_FOLDER_ID=identifiant_du_dossier_drive_uniquement_id
POLL_INTERVAL=900
GMAIL_LABEL=Factures-Traitées
GMAIL_QUERY=has:attachment filename:pdf -label:Factures-Traitées newer_than:7d

# --- Throttling Gemini (tier gratuit) ---
MAX_EMAILS_PER_CYCLE=3
MIN_SECONDS_BETWEEN_CALLS=15
MAX_GEMINI_REQUESTS_PER_DAY=18
```

### Points d'attention critiques

- **`GEMINI_API_KEY`** : clé gratuite depuis https://aistudio.google.com/apikey
- **`FACTURX_PROFILE`** : doit être `en16931` (pas `minimum` ni `EN16931` en majuscule)
- **`DRIVE_FOLDER_ID`** : uniquement l'ID, PAS l'URL. Si l'URL est `https://drive.google.com/drive/folders/1cPFMtFhN-VOnPJnoZFWcWbbsickLHiGc?usp=drive_link`, l'ID est `1cPFMtFhN-VOnPJnoZFWcWbbsickLHiGc` (sans le `?usp=drive_link`)
- **`credentials.json`** : doit être un FICHIER (mode `-a----`), pas un dossier (mode `d-----`). Vérifier avec `dir C:\Automatisch\orchestrator\`
- **Pas de `FACTURX_SERVICE_URL`** : cette variable est supprimée — il n'y a plus de microservice HTTP

---

## Partie 4 — Configuration Google OAuth

*Opération unique. Ne se fait qu'une seule fois par poste.*

### 4.1 — Créer un projet Google Cloud

1. Va sur https://console.cloud.google.com/
2. Sélecteur de projet en haut → "Nouveau projet"
3. Nom : `Factures-Automatisation` → Créer

### 4.2 — Activer les APIs

1. Menu hamburger (☰) → "API et services" → "Bibliothèque"
2. Cherche **Gmail API** → Activer
3. Cherche **Google Drive API** → Activer

### 4.3 — Configurer l'écran de consentement OAuth

Dans **Google Auth Platform** :

1. **Branding** : Nom = `Factures-Auto`, email d'assistance = ton email
2. **Audience** : Type = Externe, ajoute ton email en utilisateur test
3. **Accès aux données** : ajoute les scopes `gmail.modify` et `drive.file`

### 4.4 — Créer les identifiants

1. Google Auth Platform → **Clients**
2. **"+ Créer un client"**
3. Type : **Application de bureau** → Nom : `orchestrator-factures`
4. Créer → **Télécharge le JSON** → renomme-le `credentials.json`
5. Place-le dans `C:\Automatisch\orchestrator\credentials.json`

### 4.5 — Première autorisation (depuis Windows, pas Docker)

```powershell
pip install google-api-python-client google-auth-oauthlib

cd C:\Automatisch\orchestrator
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/drive.file']
)
creds = flow.run_local_server(port=8090, open_browser=True)
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print('Token sauvegardé !')
"
```

Le navigateur s'ouvre → se connecter → autoriser → `token.json` est créé.

### 4.6 — Trouver l'ID du dossier Drive

Ouvre le dossier dans Drive → regarde l'URL → copie la partie après `/folders/` et avant le `?`.

---

## Partie 5 — Lancement et test

### 5.1 — Premier lancement

```powershell
cd C:\Automatisch
docker compose up -d --build
```

Le build peut prendre 2-3 minutes (téléchargement Tesseract OCR + dépendances Python).

### 5.2 — Vérifier l'état

```powershell
docker compose ps
```

Résultat attendu :

```
NAME           STATUS    PORTS
orchestrator   Up
```

Un seul conteneur, pas de port exposé (plus de micro-service HTTP).

### 5.3 — Vérifier les logs au démarrage

```powershell
docker compose logs orchestrator
```

Tu dois voir :

```
Orchestrateur LangGraph Factur-X — Pure Python
Architecture : 1 graphe LangGraph, 9 nœuds, 0 microservice HTTP
Graphe LangGraph compilé : 9 nœuds, 2 arêtes conditionnelles
Démarrage de la boucle de polling...
```

### 5.4 — Suivre les logs en temps réel

```powershell
docker compose logs -f orchestrator
```

### 5.5 — Test avec une vraie facture

1. Envoie-toi un email avec une facture PDF en pièce jointe
2. Force un scan : `docker compose restart orchestrator`
3. Vérifie les logs → tu dois voir les 9 nœuds s'enchaîner avec `[ 1/9 ]`, `[ 2/9 ]`...
4. Vérifie Google Drive → le PDF Factur-X dans le bon sous-dossier mensuel
5. Vérifie Gmail → l'email a le label "Factures-Traitées"

### 5.6 — Lire les logs du workflow

Chaque PDF traité génère une séquence lisible :

```
[ 1/9 ] extract_text : facture-edf.pdf (245 Ko)
[ 2/9 ] filter_document : score:6 → candidat facture
[ 3/9 ] call_gemini : extraction EN16931...
[ 4/9 ] normalize_data : 2 ligne(s), HT=150.00€, TVA=30.00€, TTC=180.00€
[ 5/9 ] generate_xml : 4821 octets
[ 6/9 ] embed_facturx : EDF_FacturX_2026-01-15_INV-2026-001.pdf → '2026-01 Janvier'
[ 7/9 ] upload_drive : upload OK
[ 8/9 ] label_gmail : label 'Factures-Traitées' appliqué
[ 9/9 ] log_result : ✅ Succès : EDF | INV-2026-001 | 180.0€ TTC
```

### 5.7 — Comportement normal

- **PDF non-factures** (CV, billets, notifications) → rejetés par `filter_document` SANS appeler Gemini (économie de quota)
- **Factures PDF** → traitement complet 9 nœuds avec données EN16931 structurées
- **Rate limit Gemini (429)** → retry automatique avec backoff exponentiel, puis skip (retenté au prochain cycle)
- **Emails rejetés** : `newer_than:7d` dans la requête Gmail limite le bruit

---

## Partie 6 — Sauvegarder la configuration finale (export Docker)

Une fois que tout fonctionne, sauvegarder l'ensemble pour redéployer sur un autre poste.

### 6.1 — Méthode 1 : Copie du dossier source (RECOMMANDÉ)

**Sur le poste source :**

```powershell
# 1. Vérifier que tout fonctionne
cd C:\Automatisch
docker compose ps

# 2. Créer une archive ZIP du dossier complet
Compress-Archive -Path C:\Automatisch\* -DestinationPath C:\Automatisch-backup.zip -Force
```

**Fichiers inclus dans l'archive :**

| Fichier | Inclus ? | Note |
|---------|----------|------|
| `docker-compose.yml` | ✅ | 1 service uniquement |
| `.env` | ✅ | Clés API et configuration |
| `orchestrator\main.py` | ✅ | Point d'entrée |
| `orchestrator\graph.py` | ✅ | Topologie LangGraph |
| `orchestrator\nodes.py` | ✅ | 9 nœuds + routeurs |
| `orchestrator\facturx.py` | ✅ | Pipeline métier complet |
| `orchestrator\services.py` | ✅ | Google + SQLite |
| `orchestrator\state.py` | ✅ | TypedDict état partagé |
| `orchestrator\Dockerfile` | ✅ | Image Docker |
| `orchestrator\requirements.txt` | ✅ | Dépendances Python |
| `orchestrator\credentials.json` | ✅ | Identifiants OAuth |
| `orchestrator\token.json` | ⚠️ | Inclus mais devra être recréé sur le nouveau poste |
| `orchestrator_data\state.db` | ✅ | Historique des emails traités |

### 6.2 — Méthode 2 : Export de l'image Docker (OPTIONNEL)

Si le poste cible n'a pas accès à Internet ou pour éviter le temps de build :

**Sur le poste source :**

```powershell
# 1. Lister les images
docker images

# 2. Sauvegarder l'image dans un fichier tar
docker save automatisch-orchestrator -o C:\Automatisch-image.tar

# 3. Le fichier fait environ 500 Mo - 1 Go
dir C:\Automatisch-image.tar
```

**Copier sur le poste cible :**

Copie les deux fichiers sur une clé USB ou un partage réseau :
- `C:\Automatisch-backup.zip` (~100 Ko, le code source)
- `C:\Automatisch-image.tar` (~500 Mo - 1 Go, l'image Docker pré-construite)

---

## Partie 7 — Déployer sur un autre poste (import Docker)

### 7.1 — Prérequis sur le poste cible

- **Windows 10/11 Pro ou Entreprise** (nécessaire pour Hyper-V / WSL 2)
- **Docker Desktop for Windows** (gratuit pour entreprises < 250 employés)
- **Python** installé (uniquement pour la première autorisation OAuth)
- **Connexion Internet** permanente (pour Gmail, Drive, API Gemini)

### 7.2 — Installation Docker Desktop

1. Télécharger depuis https://www.docker.com/products/docker-desktop/
2. Installer, accepter l'activation de WSL 2
3. Redémarrer le PC si demandé
4. Lancer Docker Desktop une première fois
5. **Settings → General → cocher "Start Docker Desktop when you sign in to your computer"**

### 7.3 — Déploiement avec les sources (Méthode 1)

```powershell
# 1. Extraire l'archive
Expand-Archive -Path C:\Automatisch-backup.zip -DestinationPath C:\Automatisch -Force

# 2. Supprimer l'ancien token (il faudra le recréer)
del C:\Automatisch\orchestrator\token.json

# 3. Refaire l'autorisation OAuth
pip install google-api-python-client google-auth-oauthlib
cd C:\Automatisch\orchestrator
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/drive.file']
)
creds = flow.run_local_server(port=8090, open_browser=True)
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print('Token sauvegardé !')
"

# 4. Construire l'image et lancer
cd C:\Automatisch
docker compose up -d --build

# 5. Vérifier
docker compose ps
docker compose logs orchestrator
```

### 7.4 — Déploiement avec l'image pré-construite (Méthode 2)

```powershell
# 1. Extraire l'archive source
Expand-Archive -Path C:\Automatisch-backup.zip -DestinationPath C:\Automatisch -Force

# 2. Charger l'image Docker sauvegardée
docker load -i C:\Automatisch-image.tar

# 3. Supprimer l'ancien token et refaire l'OAuth
del C:\Automatisch\orchestrator\token.json
pip install google-api-python-client google-auth-oauthlib
cd C:\Automatisch\orchestrator
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/drive.file']
)
creds = flow.run_local_server(port=8090, open_browser=True)
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print('Token sauvegardé !')
"

# 4. Lancer (sans --build car l'image est déjà chargée)
cd C:\Automatisch
docker compose up -d

# 5. Vérifier
docker compose ps
docker compose logs orchestrator
```

### 7.5 — Redémarrage automatique

**La solution redémarre toute seule quand le PC s'allume**, grâce à deux mécanismes :

| Mécanisme | Rôle | Vérifier |
|-----------|------|----------|
| **Docker Desktop "Start on login"** | Lance Docker au boot Windows | Settings → General → ✅ |
| **`restart: unless-stopped`** | Relance les conteneurs au démarrage de Docker | Déjà dans docker-compose.yml ✅ |

**Scénario quotidien :**

1. Tu éteins le PC → le conteneur s'arrête
2. Tu rallumes → Windows → Docker Desktop → conteneur redémarre → polling Gmail reprend
3. Rien à faire, c'est transparent

**Exception** : `docker compose down` SUPPRIME les conteneurs → pas de restart au boot. Réserver uniquement pour la maintenance.

### 7.6 — Sécurité de la configuration

| Fichier | Risque | Protection |
|---------|--------|------------|
| `.env` | Clé API Gemini | Quota gratuit uniquement |
| `credentials.json` | Identifiants OAuth | Pas exploitable sans token |
| `token.json` | ⚠️ Accès Gmail + Drive | Le plus sensible |

**Verrouiller les droits sur le dossier :**

```powershell
# En PowerShell administrateur
icacls "C:\Automatisch" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"
```

---

## Partie 8 — Commandes utiles et dépannage

### Commandes quotidiennes

```powershell
docker compose ps                     # État du conteneur
docker compose logs -f orchestrator   # Logs temps réel
docker compose restart orchestrator   # Forcer un scan immédiat
```

### Après modification du code

```powershell
# Un seul conteneur → un seul rebuild
docker compose up -d --build
```

### Après modification du .env

```powershell
docker compose restart orchestrator    # Pas besoin de --build
```

### Arrêter / relancer

```powershell
docker compose stop       # Arrête (conserve conteneur → restart au boot)
docker compose start      # Relance après un stop
docker compose down       # ⚠️ Supprime conteneur → PAS de restart au boot
docker compose up -d      # Recrée après un down
```

### Nettoyage disque

```powershell
docker system prune -f    # Supprime images/conteneurs orphelins
```

### Dépannage

**L'orchestrateur redémarre en boucle ("Restarting")** :

```powershell
docker compose logs --tail 30 orchestrator
```

Causes fréquentes :
- `credentials.json` manquant ou invalide (vérifier que c'est un fichier, pas un dossier)
- `token.json` invalide → supprimer et refaire l'OAuth (étape 4.5)
- `GEMINI_API_KEY` manquante dans `.env`

**Erreur 429 Too Many Requests** : rate limit Gemini. Le retry automatique avec backoff est intégré. Si insuffisant, réduire `MAX_EMAILS_PER_CYCLE` dans `.env`.

**Token Google expiré** : supprimer `token.json`, refaire l'autorisation OAuth (étape 4.5).

**`DRIVE_FOLDER_ID` incorrect** : vérifier qu'il contient uniquement l'ID (pas l'URL, pas de `?usp=drive_link`).

**PDF rejeté à tort** : vérifier les logs nœud `filter_document`. Si le score est trop bas pour une vraie facture, augmenter le seuil dans `facturx.py` (`score < 2` → `score < 1`).

**Le nœud `call_gemini` échoue systématiquement** : vérifier `GEMINI_API_KEY` et le quota journalier (`MAX_GEMINI_REQUESTS_PER_DAY`).

---

## Partie 9 — Limites et évolutions futures

### Limites actuelles

| Limite | Impact | Solution |
|--------|--------|----------|
| Rate limit Gemini | ~15 req/min tier gratuit | Backoff + pause 15s intégrés |
| Token OAuth 6 mois | Expire si PC éteint 6 mois | Refaire l'OAuth |
| PDF scannés illisibles | OCR Tesseract échoue | Nœud `extract_text` rejette → log |
| Emails non-factures rescannés | Re-analysés (rejetés localement) | `newer_than:7d` limite le bruit |
| Un seul compte Gmail | Une seule boîte scannée | Dupliquer l'orchestrateur |

### Évolutions futures

LangGraph permet d'ajouter des nœuds facilement : une fonction Python + une ligne dans `graph.py`.

**Court terme** : enrichissement SIRET (API INSEE), détection doublons Drive, Google Sheets de suivi, notifications email/Slack.

**Moyen terme** : agent relance fournisseurs, rapprochement bancaire, tableau de bord web.

**Long terme** : orchestration complète (devis, commandes, factures clients), multi-agents, profil Factur-X EXTENDED.

---

## Récapitulatif express : déploiement sur un nouveau poste

```powershell
# 1. Installer Docker Desktop + cocher "Start on login"
# 2. Installer Python (cocher "Add to PATH")
# 3. Copier C:\Automatisch\ depuis l'archive ZIP ou la clé USB

# 4. (Optionnel) Charger l'image si fournie
docker load -i C:\Automatisch-image.tar

# 5. Autorisation Google OAuth
pip install google-api-python-client google-auth-oauthlib
cd C:\Automatisch\orchestrator
del token.json
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/drive.file']
)
creds = flow.run_local_server(port=8090, open_browser=True)
with open('token.json', 'w') as f:
    f.write(creds.to_json())
print('OK')
"

# 6. Lancer (--build si pas d'image pré-chargée)
cd C:\Automatisch
docker compose up -d --build

# 7. Vérifier
docker compose ps
docker compose logs orchestrator

# C'est fait. La solution tourne et redémarre toute seule avec le PC.
```
