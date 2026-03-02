#!/usr/bin/env python3
"""
graph.py — Topologie du graphe LangGraph Factur-X
==================================================

Ce module ASSEMBLE les nœuds et les arêtes pour former le workflow complet.
C'est la "vue architecturale" de l'application : on voit d'un coup d'œil
le flux de données et les chemins de décision.

CONCEPT CLÉ : Le Graphe Orienté (DAG)
---------------------------------------
LangGraph représente le workflow comme un graphe orienté acyclique :
  - Les NŒUDS sont des fonctions qui traitent l'état
  - Les ARÊTES DIRECTES définissent la séquence normale
  - Les ARÊTES CONDITIONNELLES permettent le branchement dynamique

Topologie du graphe :

                     ┌──────────────┐
                     │ extract_text │ (OCR — natif ou Tesseract)
                     └──────┬───────┘
                            │ (toujours)
                     ┌──────▼──────────┐
                     │ filter_document │ (filtrage keywords — gratuit)
                     └──────┬──────────┘
             ┌──────────────┤
    [not_invoice / erreur]  │ [document candidat facture]
             ↓              ↓
        log_result   ┌─────▼──────────┐
           (END)     │  call_gemini   │ (IA extraction JSON — payant quota)
                     └──────┬─────────┘
             ┌──────────────┤
    [not_invoice / 429]     │ [est une facture]
             ↓              ↓
        log_result   ┌──────▼──────────┐
           (END)     │ normalize_data  │ (garantit conformité EN16931)
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │  generate_xml   │ (XML CII D16B/D22B)
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │  embed_facturx  │ (PDF/A-3b + XML embarqué)
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │  upload_drive   │ (Google Drive — sous-dossier mensuel)
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │ update_matrix   │ (matrice Excel Drive — coche X fournisseur/mois)
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │   label_gmail   │ (label "Factures-Traitées")
                     └──────┬──────────┘
                            │
                     ┌──────▼──────────┐
                     │   log_result    │ (SQLite + logs — toujours atteint)
                     └──────┬──────────┘
                            │
                           END

Note sur les guard clauses :
  Les nœuds normalize_data → label_gmail vérifient state["processing_error"]
  en entrée et retournent {} si positionné. Cela permet d'avoir des arêtes
  directes simples tout en gérant les erreurs proprement.
  log_result est TOUJOURS exécuté (il lit processing_error pour décider
  ce qu'il écrit dans SQLite).
"""

from langgraph.graph import END, StateGraph

from state import InvoiceState
from nodes import (
    node_extract_text,
    node_filter_document,
    node_call_gemini,
    node_normalize_data,
    node_generate_xml,
    node_embed_facturx,
    node_upload_drive,
    node_update_matrix,
    node_label_gmail,
    node_log_result,
    route_after_filter,
    route_after_gemini,
)


def build_graph():
    """
    Construit et compile le graphe LangGraph Factur-X.

    La compilation (g.compile()) :
      - Valide la topologie (nœuds connectés, pas de cycles)
      - Crée un CompiledStateGraph avec la méthode .invoke(state)
      - Permet d'ajouter optionnellement un checkpointer (persistence)

    Returns:
        CompiledStateGraph : workflow prêt à être invoqué
    """
    g = StateGraph(InvoiceState)

    # ── Enregistrement des 10 nœuds ─────────────────────────────────────────
    # Format : g.add_node("nom_du_noeud", fonction_du_noeud)
    # Le NOM est la clé utilisée dans les arêtes et les routeurs.
    g.add_node("extract_text",    node_extract_text)
    g.add_node("filter_document", node_filter_document)
    g.add_node("call_gemini",     node_call_gemini)
    g.add_node("normalize_data",  node_normalize_data)
    g.add_node("generate_xml",    node_generate_xml)
    g.add_node("embed_facturx",   node_embed_facturx)
    g.add_node("upload_drive",    node_upload_drive)
    g.add_node("update_matrix",   node_update_matrix)
    g.add_node("label_gmail",     node_label_gmail)
    g.add_node("log_result",      node_log_result)

    # ── Point d'entrée ───────────────────────────────────────────────────────
    g.set_entry_point("extract_text")

    # ── Arête directe : extract_text → filter_document ───────────────────────
    # On passe toujours par le filtrage. Si OCR a échoué, filter_document
    # a une guard clause qui retourne {} sans analyser le texte vide.
    # Le routeur route_after_filter détectera processing_error et ira à log_result.
    g.add_edge("extract_text", "filter_document")

    # ── Arête conditionnelle : filter_document ─────────────────────────────
    # Court-circuit ÉCONOMIQUE : si le document n'est pas une facture selon
    # le filtre de keywords (analyse locale gratuite), on saute Gemini.
    # Sans cette arête, filter_document irait toujours vers call_gemini.
    g.add_conditional_edges(
        "filter_document",
        route_after_filter,           # Routeur : retourne "call_gemini" ou "log_result"
        {
            "call_gemini": "call_gemini",
            "log_result":  "log_result",
        },
    )

    # ── Arête conditionnelle : call_gemini ────────────────────────────────
    # Court-circuit FONCTIONNEL : si Gemini dit "pas une facture" ou s'il y a
    # un rate limit 429, on saute la génération XML (inutile ou impossible).
    g.add_conditional_edges(
        "call_gemini",
        route_after_gemini,           # Routeur : retourne "normalize_data" ou "log_result"
        {
            "normalize_data": "normalize_data",
            "log_result":     "log_result",
        },
    )

    # ── Arêtes directes : happy path de génération Factur-X ─────────────────
    # Ces nœuds ont chacun une guard clause interne (if processing_error: return {})
    # pour gérer les erreurs d'un nœud précédent sans arêtes conditionnelles supplémentaires.
    g.add_edge("normalize_data", "generate_xml")
    g.add_edge("generate_xml",   "embed_facturx")
    g.add_edge("embed_facturx",  "upload_drive")
    g.add_edge("upload_drive",   "update_matrix")
    g.add_edge("update_matrix",  "label_gmail")
    g.add_edge("label_gmail",    "log_result")

    # ── Arête finale : log_result → END ─────────────────────────────────────
    # log_result est toujours le nœud terminal, qu'on arrive par le happy path
    # ou par un court-circuit conditionnel. Il écrit dans SQLite et logge.
    g.add_edge("log_result", END)

    return g.compile()
