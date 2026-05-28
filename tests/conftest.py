"""
conftest.py — Configuration pytest partagée pour le projet Factur-X
=====================================================================

Fournit des stubs des dépendances lourdes absentes en env CI/test léger
(googleapiclient, langgraph, lxml, fitz, facturx…).

Cela permet d'importer nodes.py / facturx_utils.py dans les tests unitaires
sans avoir installé toutes les dépendances de production.

Règle de sécurité : un module n'est stubifié QUE s'il est introuvable
(ImportError). Si le paquet est réellement installé, il est utilisé tel quel.
Cela évite d'écraser les vraies bibliothèques dans les tests d'intégration
(ex : test_facturx_en16931.py qui utilise la vraie lib facturx).
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock


def _stub_if_missing(name: str) -> bool:
    """Stubifie ``name`` dans sys.modules UNIQUEMENT s'il n'est pas importable.

    Returns:
        True si un stub a été injecté, False si le vrai module existe.
    """
    if name in sys.modules:
        return False  # Déjà chargé (réel ou stub précédent)
    try:
        importlib.import_module(name)
        return False  # Importable → on laisse le vrai module
    except (ImportError, ModuleNotFoundError):
        sys.modules[name] = MagicMock()
        return True


# ── Google API client --------------------------------------------------------
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
    _stub_if_missing(_pkg)

# MediaIoBaseUpload : attribut indispensable pour l'import de nodes.py
import googleapiclient.http  # noqa: E402
if not hasattr(googleapiclient.http, "MediaIoBaseUpload") or isinstance(
    googleapiclient.http.MediaIoBaseUpload, MagicMock
):
    googleapiclient.http.MediaIoBaseUpload = MagicMock()

# ── LangGraph ----------------------------------------------------------------
for _pkg in ("langgraph", "langgraph.graph"):
    _stub_if_missing(_pkg)

import langgraph.graph  # noqa: E402
if isinstance(getattr(langgraph.graph, "StateGraph", None), MagicMock) or not hasattr(
    langgraph.graph, "StateGraph"
):
    langgraph.graph.StateGraph = MagicMock()
    langgraph.graph.END = "END"

# ── requests (appels Gemini) -------------------------------------------------
_stub_if_missing("requests")
# requests.exceptions doit exister comme module avec ConnectionError et Timeout
import requests  # noqa: E402
if isinstance(requests, MagicMock) or not hasattr(requests, "exceptions"):
    exc_mock = MagicMock()
    exc_mock.ConnectionError = type("ConnectionError", (Exception,), {})
    exc_mock.Timeout = type("Timeout", (Exception,), {})

    class _HTTPError(Exception):
        def __init__(self, *args, response=None, **kwargs):
            super().__init__(*args, **kwargs)
            self.response = response

    exc_mock.HTTPError = _HTTPError
    requests.exceptions = exc_mock
    sys.modules["requests.exceptions"] = exc_mock

# ── Pydantic (schemas.py) ---------------------------------------------------
_stub_if_missing("pydantic")
import pydantic  # noqa: E402
if isinstance(pydantic, MagicMock) or not hasattr(pydantic, "ValidationError"):
    pydantic.ValidationError = type("ValidationError", (Exception,), {})
    pydantic.BaseModel = MagicMock()
    pydantic.Field = MagicMock()
    sys.modules["pydantic"] = pydantic

# ── Bibliothèques PDF/XML lourdes --------------------------------------------
# Stubifiées UNIQUEMENT si absentes — le test_facturx_en16931.py utilise
# les vraies (fitz, lxml, facturx) quand elles sont installées en CI.
for _pkg in (
    "fitz",
    "lxml",
    "lxml.etree",
    "facturx",
):
    _stub_if_missing(_pkg)
