# H-2 : version épinglée sur un tag patch (plus stable que :3.12-slim).
# Pour épingler sur un digest SHA256 (niveau max) :
#   docker pull python:3.12-slim
#   docker inspect python:3.12-slim --format='{{index .RepoDigests 0}}'
# → remplacer cette ligne par : FROM python@sha256:<digest>
# Renovate/Dependabot peut maintenir ce digest automatiquement.
FROM python:3.12.9-slim

# ── Dépendances système ───────────────────────────────────────────────────────
# Tesseract OCR : reconnaissance de caractères pour les PDFs scannés
#   tesseract-ocr-fra : langue française (principal)
#   tesseract-ocr-deu : langue allemande (factures DE/CH/AT)
#   tesseract-ocr-eng : langue anglaise (fallback + noms techniques)
# Ghostscript : requis par certains traitements PDF (lib factur-x)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fra \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installer les dépendances Python en premier (meilleure mise en cache Docker)
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copier tous les modules Python de l'orchestrateur
COPY state.py services.py facturx_utils.py schemas.py nodes.py graph.py main.py ./

# CKV_DOCKER_3 : créer un utilisateur non-root pour limiter la surface d'attaque
RUN useradd -r -u 1001 -s /sbin/nologin appuser
USER appuser

# H-6 : healthcheck réel basé sur un fichier heartbeat écrit par main.py.
# Le fichier /tmp/heartbeat est mis à jour à chaque cycle de polling.
# Seuil : 1800s (30 min) = 2 × POLL_INTERVAL par défaut.
# Si le process est bloqué ou crashé, le fichier devient obsolète → unhealthy.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "\
import sys, time; \
from pathlib import Path; \
p = Path('/tmp/heartbeat'); \
sys.exit(0 if p.exists() and (time.time() - float(p.read_text())) < 1800 else 1)" \
    || exit 1

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1

# L'orchestrateur tourne en continu (boucle de polling toutes les N secondes)
# -u : unbuffered stdout/stderr pour voir les logs en temps réel
CMD ["python", "-u", "main.py"]
