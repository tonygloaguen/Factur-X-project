FROM python:3.12-slim

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
RUN pip install --no-cache-dir -r requirements.txt

# Copier tous les modules Python de l'orchestrateur
COPY state.py services.py facturx_utils.py nodes.py graph.py main.py ./

# CKV_DOCKER_3 : créer un utilisateur non-root pour limiter la surface d'attaque
RUN useradd -r -u 1001 -s /sbin/nologin appuser
USER appuser

# CKV_DOCKER_2 : vérification que le process Python est bien vivant
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# L'orchestrateur tourne en continu (boucle de polling toutes les N secondes)
# -u : unbuffered stdout/stderr pour voir les logs en temps réel
CMD ["python", "-u", "main.py"]
