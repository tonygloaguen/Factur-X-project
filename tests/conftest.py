"""
conftest.py — Configuration pytest partagée pour le projet Factur-X
=====================================================================

Fournit :
  - Stubs des dépendances lourdes absentes en env CI/test léger
    (googleapiclient, langgraph, lxml, fitz, facturx…)
  - Cela permet d'importer nodes.py / facturx_utils.py dans les tests
    sans avoir installé toutes les dépendances de production.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _stub(name: str) -> MagicMock:
    """Crée un MagicMock et l'enregistre dans sys.modules si absent."""
    if name not in sys.modules:
        mock = MagicMock()
        sys.modules[name] = mock
        return mock
    return sys.modules[name]  # type: ignore[return-value]


# ── Google API client (non installé en env test léger) ----------------------
for _pkg in (
    "googleapiclient",
    "googleapiclient.http",
    "googleapiclient.discovery",
    "google",
    "google.oauth2",
    "google.oauth2.credentials",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google_auth_oauthlib",
    "google_auth_oauthlib.flow",
):
    _stub(_pkg)

# MediaIoBaseUpload doit être un vrai attribut mockable
import googleapiclient.http  # noqa: E402  (après stub)
googleapiclient.http.MediaIoBaseUpload = MagicMock()

# ── LangGraph (non installé en env test léger) --------------------------------
for _pkg in ("langgraph", "langgraph.graph"):
    _stub(_pkg)

# StateGraph et END accessibles via mock
import langgraph.graph  # noqa: E402
langgraph.graph.StateGraph = MagicMock()
langgraph.graph.END = "END"

# ── Bibliothèques PDF/XML lourdes (présentes en prod, absentes en CI léger) ---
for _pkg in (
    "fitz",
    "lxml",
    "lxml.etree",
    "facturx",
):
    _stub(_pkg)

# ── Requests est léger et généralement présent ; stub défensif seulement ----
if "requests" not in sys.modules:
    _stub("requests")
