# Runbook : Workflow Factures LangGraph — Guide complet

*Version 3.0 — 24 février 2026*
*Mis à jour : profil EN16931, export/import Docker, corrections en conditions réelles*

---

## Ce document, c'est quoi ?

Un **runbook**, c'est un guide pas-à-pas qu'on suit dans l'ordre pour réaliser une opération technique. Celui-ci couvre :

1. **Comprendre** l'architecture
2. **Nettoyer** l'ancienne stack Automatisch
3. **Installer** la nouvelle stack LangGraph + Factur-X EN16931
4. **Configurer** Google OAuth (Gmail + Drive)
5. **Tester** et valider le workflow
6. **Sauvegarder** la configuration finale (export Docker)
7. **Déployer** sur un autre poste (import Docker)
8. **Commandes utiles** et dépannage
9. **Évolutions futures**

---

## Partie 1 — Comprendre l'architecture

### Le workflow en une phrase

Un email arrive dans Gmail avec un PDF → l'orchestrateur le détecte → le micro-service l'analyse par OCR + IA Gemini → génère un PDF Factur-X EN16931 conforme (avec lignes, TVA, adresses) → l'uploade dans Google Drive dans le bon dossier mensuel → met un label sur l'email.

### Architecture — 2 conteneurs

```
facturx-service       → Micro-service Python OCR + Gemini + Factur-X EN16931 (port 5000)
orchestrator          → Script Python LangGraph : polling Gmail → appel micro-service → Drive → label
```

RAM totale : ~200 Mo. Tout en Python. Debug facile avec des logs lisibles.

### Comment ça communique

```
Internet                          Réseau Docker interne
   │                              (facturx-net)
   │
   ├── API Gmail      ◄──────── orchestrator (LangGraph)
   ├── API Drive      ◄────────    │
   └── API Gemini     ◄────────    └──► facturx-service:5000
                                        (OCR + Gemini + Factur-X EN16931)
```

### Le graphe LangGraph (5 nœuds)

```
poll_gmail                          ← Cherche les emails non traités avec PJ PDF
    │
    ▼
process_invoice                     ← POST vers facturx-service (OCR + Gemini + XML EN16931)
    │
    ├── (si erreur ou pas une facture) ──► log_result → FIN
    │
    ▼
upload_drive                        ← Crée le sous-dossier mensuel, uploade le PDF Factur-X
    │
    ▼
label_gmail                         ← Ajoute le label "Factures-Traitées" sur l'email
    │
    ▼
log_result                          ← Log le résultat (succès ou échec)
```

### Profil Factur-X EN16931 — ce qui est généré

Le profil EN16931 est le profil conforme à la norme européenne. Il contient :

- **Lignes de facture** (obligatoire BR-16) : description, quantité, prix unitaire, TVA par ligne
- **Adresse vendeur** complète avec code pays (BR-08, BR-09)
- **Adresse acheteur** complète avec code pays (BR-10, BR-11)
- **Ventilation TVA** par catégorie : base HT, montant TVA, taux, code (BG-23)
- **Totaux complets** : somme lignes, HT, TVA, TTC, montant dû (BG-22)
- **Moyens de paiement** : code, IBAN, BIC si disponibles (BG-16)
- **Date d'échéance** si disponible

Le micro-service inclut une couche de **normalisation** qui complète automatiquement les données manquantes (ligne de fallback, adresses par défaut, recalcul des totaux).

---

## Partie 2 — Nettoyage de l'ancienne stack Automatisch

*Sauter cette partie si tu pars d'un poste vierge.*

### Si Docker Desktop n'est pas lancé

Menu Démarrer → "Docker Desktop" → attendre que la baleine 🐋 soit stable (1-2 minutes).

### Si le docker-compose.yml a déjà été supprimé

```powershell
docker rm -f automatisch-main automatisch-worker automatisch-db automatisch-redis facturx-service
docker volume rm automatisch_automatisch-db-data
docker network rm automatisch_automatisch-net
docker rmi automatischio/automatisch:latest postgres:16-alpine redis:7-alpine
```

Si des commandes disent "No such container", c'est normal.

### Si le docker-compose.yml existe encore

```powershell
cd C:\Automatisch
docker compose down
docker volume rm automatisch_automatisch-db-data
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
├── docker-compose.yml              ← 2 services (facturx-service + orchestrator)
├── .env                             ← Variables d'environnement
├── facturx-service\
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py                       ← Micro-service OCR + Gemini + Factur-X EN16931
├── orchestrator\
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                      ← Workflow LangGraph
│   ├── credentials.json             ← Identifiants OAuth Google
│   └── token.json                   ← Token d'accès (créé automatiquement)
└── orchestrator_data\
    └── state.db                     ← Base SQLite anti-retraitement
```

### Contenu du .env

```ini
# --- Micro-service Factur-X ---
GEMINI_API_KEY=ta_clé_gemini_ici
GEMINI_MODEL=gemini-2.5-flash
FACTURX_PROFILE=en16931

# --- Orchestrateur LangGraph ---
DRIVE_FOLDER_ID=identifiant_du_dossier_drive_uniquement_id
POLL_INTERVAL=900
GMAIL_LABEL=Factures-Traitées
GMAIL_QUERY=has:attachment filename:pdf -label:Factures-Traitées newer_than:7d
```

### Points d'attention critiques

- **`GEMINI_API_KEY`** : clé gratuite depuis https://aistudio.google.com/apikey
- **`FACTURX_PROFILE`** : doit être `en16931` (pas `minimum` ni `EN16931` en majuscule)
- **`DRIVE_FOLDER_ID`** : uniquement l'ID, PAS l'URL. Si l'URL est `https://drive.google.com/drive/folders/1cPFMtFhN-VOnPJnoZFWcWbbsickLHiGc?usp=drive_link`, l'ID est `1cPFMtFhN-VOnPJnoZFWcWbbsickLHiGc` (sans le `?usp=drive_link`)
- **`docker-compose.yml`** : la ligne `FACTURX_PROFILE` doit contenir `:-` (tiret) et pas juste `:` → `${FACTURX_PROFILE:-en16931}`
- **`credentials.json`** : doit être un FICHIER (mode `-a----`), pas un dossier (mode `d-----`). Vérifier avec `dir C:\Automatisch\orchestrator\`

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

### 5.2 — Vérifier l'état

```powershell
docker compose ps
```

Résultat attendu :

```
NAME              STATUS              PORTS
facturx-service   Up (healthy)        0.0.0.0:5000->5000/tcp
orchestrator      Up
```

### 5.3 — Vérifier le profil EN16931

```powershell
curl http://localhost:5000/health
```

Doit retourner : `"facturx_profile": "en16931"`

### 5.4 — Vérifier les logs

```powershell
docker compose logs -f orchestrator
```

### 5.5 — Test avec une vraie facture

1. Envoie-toi un email avec une facture PDF en pièce jointe
2. Force un scan : `docker compose restart orchestrator`
3. Vérifie les logs → tu dois voir les lignes extraites et le profil EN16931
4. Vérifie Google Drive → le PDF Factur-X dans le bon sous-dossier mensuel
5. Vérifie Gmail → l'email a le label "Factures-Traitées"

### 5.6 — Comportement normal

- **PDF non-factures** (CV, billets, notifications) → rejetés par le filtrage local SANS appeler Gemini (économie de quota)
- **Factures PDF** → traitement complet EN16931 avec lignes détaillées
- **Rate limit Gemini (429)** → retry automatique avec backoff exponentiel + pause 5s entre chaque PDF
- **Emails rejetés** → re-scannés au prochain cycle, mais `newer_than:7d` limite le bruit

---

## Partie 6 — Sauvegarder la configuration finale (export Docker)

Une fois que tout fonctionne, il faut sauvegarder l'ensemble pour pouvoir le redéployer facilement sur un autre poste.

### 6.1 — Méthode 1 : Copie du dossier source (RECOMMANDÉ)

C'est la méthode la plus simple et la plus fiable. Elle permet de reconstruire les images Docker sur le poste cible.

**Sur le poste source :**

```powershell
# 1. Vérifier que tout fonctionne
cd C:\Automatisch
docker compose ps

# 2. Créer une archive ZIP du dossier complet
Compress-Archive -Path C:\Automatisch\* -DestinationPath C:\Automatisch-backup.zip -Force

# 3. Vérifier le contenu
# L'archive doit contenir :
#   docker-compose.yml, .env, 
#   facturx-service\ (Dockerfile, app.py, requirements.txt)
#   orchestrator\ (Dockerfile, main.py, requirements.txt, credentials.json)
#   orchestrator_data\ (state.db)
```

**Fichiers inclus dans l'archive :**

| Fichier | Inclus ? | Note |
|---------|----------|------|
| `docker-compose.yml` | ✅ | Configuration des services |
| `.env` | ✅ | Clés API et configuration |
| `facturx-service\*` | ✅ | Micro-service complet |
| `orchestrator\main.py` | ✅ | Workflow LangGraph |
| `orchestrator\Dockerfile` | ✅ | Image Docker |
| `orchestrator\requirements.txt` | ✅ | Dépendances Python |
| `orchestrator\credentials.json` | ✅ | Identifiants OAuth |
| `orchestrator\token.json` | ⚠️ | Inclus mais devra être recréé sur le nouveau poste |
| `orchestrator_data\state.db` | ✅ | Historique des emails traités |

### 6.2 — Méthode 2 : Export des images Docker (OPTIONNEL)

Si le poste cible n'a pas accès à Internet (ou si tu veux éviter le temps de `docker compose build`), tu peux exporter les images déjà construites.

**Sur le poste source :**

```powershell
# 1. Lister les images
docker images

# 2. Sauvegarder les images dans un fichier tar
docker save automatisch-facturx-service automatisch-orchestrator -o C:\Automatisch-images.tar

# 3. Le fichier fait environ 500 Mo - 1 Go
dir C:\Automatisch-images.tar
```

**Copier sur le poste cible :**

Copie les deux fichiers sur une clé USB ou un partage réseau :
- `C:\Automatisch-backup.zip` (~100 Ko, le code source)
- `C:\Automatisch-images.tar` (~500 Mo - 1 Go, les images Docker pré-construites)

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

# 4. Construire les images et lancer
cd C:\Automatisch
docker compose up -d --build

# 5. Vérifier
docker compose ps
curl http://localhost:5000/health
docker compose logs -f orchestrator
```

### 7.4 — Déploiement avec les images pré-construites (Méthode 2)

```powershell
# 1. Extraire l'archive source
Expand-Archive -Path C:\Automatisch-backup.zip -DestinationPath C:\Automatisch -Force

# 2. Charger les images Docker sauvegardées
docker load -i C:\Automatisch-images.tar

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

# 4. Lancer (sans --build car les images sont déjà chargées)
cd C:\Automatisch
docker compose up -d

# 5. Vérifier
docker compose ps
curl http://localhost:5000/health
docker compose logs -f orchestrator
```

### 7.5 — Redémarrage automatique

**La solution redémarre toute seule quand le PC s'allume**, grâce à deux mécanismes :

| Mécanisme | Rôle | Vérifier |
|-----------|------|----------|
| **Docker Desktop "Start on login"** | Lance Docker au boot Windows | Settings → General → ✅ |
| **`restart: unless-stopped`** | Relance les conteneurs au démarrage de Docker | Déjà dans docker-compose.yml ✅ |

**Scénario quotidien :**

1. Tu éteins le PC → les conteneurs s'arrêtent
2. Tu rallumes → Windows → Docker Desktop → conteneurs redémarrent → polling Gmail reprend
3. Rien à faire, c'est transparent

**Exception** : `docker compose down` SUPPRIME les conteneurs → pas de restart au boot. Réserver uniquement pour la maintenance. Pour le quotidien, éteindre le PC normalement suffit.

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
docker compose ps                           # État des conteneurs
docker compose logs -f orchestrator          # Logs temps réel
docker compose logs --tail 50 facturx-service # Dernières lignes micro-service
docker compose restart orchestrator          # Forcer un scan immédiat
```

### Après modification du code

```powershell
docker compose up -d --build facturx-service    # Si app.py modifié
docker compose up -d --build orchestrator       # Si main.py modifié
docker compose up -d --build                    # Si les deux modifiés
```

### Après modification du .env

```powershell
docker compose restart orchestrator    # Pas besoin de --build
```

### Arrêter / relancer

```powershell
docker compose stop       # Arrête (conserve conteneurs → restart au boot)
docker compose start      # Relance après un stop
docker compose down       # ⚠️ Supprime conteneurs → PAS de restart au boot
docker compose up -d      # Recrée après un down
```

### Nettoyage disque

```powershell
docker system prune -f    # Supprime images/conteneurs orphelins
```

### Dépannage

**L'orchestrateur redémarre en boucle ("Restarting")** :

```powershell
docker compose logs --tail 20 orchestrator
```

Causes : `credentials.json` manquant/invalide (vérifier que c'est un fichier, pas un dossier), `token.json` invalide (supprimer et refaire l'OAuth étape 4.5), micro-service inaccessible.

**Erreur 502 Bad Gateway** : le micro-service est surchargé ou a crashé. Vérifier `docker compose logs --tail 20 facturx-service`. Souvent causé par rate limit Gemini.

**Erreur 429 Too Many Requests** : rate limit Gemini. Le retry automatique avec backoff est intégré. Si insuffisant, augmenter `time.sleep(5)` dans `main.py`.

**Erreur d'interpolation docker-compose** (`invalid interpolation format`) : vérifier que la ligne `FACTURX_PROFILE` dans docker-compose.yml contient `:-` (avec tiret) et pas juste `:`. Correct : `${FACTURX_PROFILE:-en16931}`.

**Token Google expiré** : supprimer `token.json`, refaire l'autorisation OAuth (étape 4.5).

**DRIVE_FOLDER_ID incorrect** : vérifier qu'il contient uniquement l'ID (pas l'URL, pas de `?usp=drive_link`).

---

## Partie 9 — Limites et évolutions futures

### Limites actuelles

| Limite | Impact | Solution |
|--------|--------|----------|
| Rate limit Gemini | ~15 req/min tier gratuit | Backoff + pause 5s intégrés |
| Token OAuth 6 mois | Expire si PC éteint 6 mois | Refaire l'OAuth |
| PDF scannés illisibles | OCR échoue | Erreur 422, email ignoré |
| Emails non-factures rescannés | Re-analysés à chaque cycle | `newer_than:7d` limite le bruit |
| Un seul compte Gmail | Une seule boîte scannée | Dupliquer l'orchestrator |

### Évolutions futures

LangGraph permet d'ajouter des nœuds facilement : une fonction Python + une ligne dans le graphe.

**Court terme** : enrichissement SIRET (API INSEE), détection doublons Drive, Google Sheets de suivi, notifications email/Slack.

**Moyen terme** : agent relance fournisseurs, rapprochement bancaire, tableau de bord web.

**Long terme** : orchestration complète (devis, commandes, factures clients), multi-agents, profil Factur-X EXTENDED.

---

## Récapitulatif express : déploiement sur un nouveau poste

```powershell
# 1. Installer Docker Desktop + cocher "Start on login"
# 2. Installer Python (cocher "Add to PATH")
# 3. Copier C:\Automatisch\ depuis l'archive ZIP ou la clé USB

# 4. (Optionnel) Charger les images si fournies
docker load -i C:\Automatisch-images.tar

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

# 6. Lancer (--build si pas d'images pré-chargées)
cd C:\Automatisch
docker compose up -d --build

# 7. Vérifier
docker compose ps
curl http://localhost:5000/health
docker compose logs -f orchestrator

# C'est fait. La solution tourne et redémarre toute seule avec le PC.
```
