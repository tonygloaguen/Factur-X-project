#!/usr/bin/env python3
"""
test_flash_lite.py — Benchmark gemini-2.1-flash-lite vs gemini-2.5-flash
=========================================================================

Compare deux modèles Gemini sur des factures PDF réelles (fixtures/).

Mesures :
  - Précision : proportion de champs clés extraits (non-null, non-zéro)
  - Latence   : temps de réponse API wall-clock
  - Coût      : estimation USD basée sur le comptage de tokens

Sans vérité terrain, la "précision" mesure la présence des champs critiques.
Pour une comparaison avec vérité terrain, ajoutez un fichier
``fixtures/ground_truth.jsonl`` (une ligne JSON par PDF, clé ``filename``).

Usage::

    GEMINI_API_KEY=xxx python tests/test_flash_lite.py
    GEMINI_API_KEY=xxx python tests/test_flash_lite.py --report-out /tmp/bench.json
    GEMINI_API_KEY=xxx python tests/test_flash_lite.py --fixtures-dir /data/factures

Prérequis::

    pip install requests  # déjà présent dans requirements
    # PyMuPDF (fitz) déjà présent dans requirements

Contraintes :
  - Python 3.11+, async/await (asyncio.to_thread autour de requests sync)
  - Credentials via GEMINI_API_KEY uniquement
  - Ne modifie aucun code de prod — script isolé
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests  # sync HTTP, wrapped via asyncio.to_thread

# ── Chemins -----------------------------------------------------------------
_TESTS_DIR = Path(__file__).parent
_ORCH_DIR = _TESTS_DIR.parent / "orchestrator"
sys.path.insert(0, str(_ORCH_DIR))

# Imports prod en lecture seule (OCR + prompt) — aucune modification
from facturx_utils import GEMINI_SYSTEM_PROMPT, extract_text_from_pdf  # noqa: E402

logger = logging.getLogger("benchmark")

# ── Modèles à comparer (overridable via --models) ---------------------------
DEFAULT_MODELS: list[str] = ["gemini-2.5-flash", "gemini-2.1-flash-lite"]

# ── Pricing estimatif (USD / 1M tokens) -------------------------------------
# Source : https://ai.google.dev/pricing — vérifier avant de prendre des
# décisions budgétaires, les tarifs changent.
PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {
        "input_per_1m": 0.15,
        "output_per_1m": 0.60,
    },
    "gemini-2.1-flash-lite": {
        "input_per_1m": 0.075,   # estimation
        "output_per_1m": 0.30,   # estimation
    },
}

# ── Champs scorés (présence = extraction réussie) ---------------------------
SCORED_FIELDS: list[str] = [
    "montant_ht",
    "montant_ttc",
    "montant_tva",
    "date_facture",
    "numero_facture",
    "vendeur.nom_court",
]


# ── Dataclasses résultats ----------------------------------------------------

@dataclass
class ModelResult:
    """Résultat d'un appel modèle sur un PDF."""

    model: str
    pdf_name: str
    latency_s: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    extracted: dict[str, Any]
    field_scores: dict[str, bool]   # field_key → True si extrait
    precision_score: float           # fraction de champs extraits
    error: str | None = None


@dataclass
class BenchmarkReport:
    """Rapport complet du benchmark."""

    generated_at: str
    models: list[str]
    fixture_count: int
    pricing_note: str
    results: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


# ── Helpers API Gemini -------------------------------------------------------

def _api_url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )


def _build_payload(ocr_text: str) -> dict[str, Any]:
    user_message = f"Texte OCR de la facture :\n\n{ocr_text[:8000]}"
    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": GEMINI_SYSTEM_PROMPT + "\n\n" + user_message}],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }


def _clean_json(raw: str) -> str:
    """Nettoyage minimal du JSON Gemini (commentaires, trailing commas)."""
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"//[^\n]*", "", raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return raw.strip()


def _call_gemini_sync(
    model: str,
    api_key: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], int, int]:
    """Appel Gemini synchrone. Retourne (json_extrait, tokens_input, tokens_output)."""
    resp = requests.post(
        _api_url(model),
        headers={"x-goog-api-key": api_key},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()

    raw_text: str = body["candidates"][0]["content"]["parts"][0]["text"]
    usage: dict[str, int] = body.get("usageMetadata", {})
    input_tokens: int = usage.get("promptTokenCount", 0)
    output_tokens: int = usage.get("candidatesTokenCount", 0)

    try:
        extracted: dict[str, Any] = json.loads(raw_text)
    except json.JSONDecodeError:
        extracted = json.loads(_clean_json(raw_text))

    return extracted, input_tokens, output_tokens


# ── Scoring -----------------------------------------------------------------

def _get_nested(data: dict[str, Any], dotted_key: str) -> Any:
    """Navigue dans un dict imbriqué via clé pointée ('vendeur.nom_court')."""
    val: Any = data
    for part in dotted_key.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


def _score_fields(data: dict[str, Any]) -> dict[str, bool]:
    """Score chaque champ clé : True si extrait (non-null, non-zéro, non-vide)."""
    scores: dict[str, bool] = {}
    for fk in SCORED_FIELDS:
        val = _get_nested(data, fk)
        scores[fk] = bool(val and val != 0 and val != "")
    return scores


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICING.get(model, {"input_per_1m": 0.0, "output_per_1m": 0.0})
    return (
        input_tokens * p["input_per_1m"] / 1_000_000
        + output_tokens * p["output_per_1m"] / 1_000_000
    )


# ── Async orchestration -----------------------------------------------------

async def _call_model(
    model: str,
    api_key: str,
    payload: dict[str, Any],
    pdf_name: str,
) -> ModelResult:
    """Wrapper async autour de l'appel synchrone (asyncio.to_thread)."""
    t0 = time.perf_counter()
    try:
        extracted, inp, out = await asyncio.to_thread(
            _call_gemini_sync, model, api_key, payload
        )
        latency = time.perf_counter() - t0
        scores = _score_fields(extracted)
        precision = sum(scores.values()) / len(scores) if scores else 0.0
        return ModelResult(
            model=model,
            pdf_name=pdf_name,
            latency_s=round(latency, 3),
            input_tokens=inp,
            output_tokens=out,
            cost_usd=round(_estimate_cost(model, inp, out), 8),
            extracted=extracted,
            field_scores=scores,
            precision_score=round(precision, 4),
        )
    except Exception as exc:
        latency = time.perf_counter() - t0
        logger.error("Erreur modèle=%s pdf=%s : %s", model, pdf_name, exc)
        return ModelResult(
            model=model,
            pdf_name=pdf_name,
            latency_s=round(latency, 3),
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            extracted={},
            field_scores={fk: False for fk in SCORED_FIELDS},
            precision_score=0.0,
            error=str(exc),
        )


async def _benchmark_pdf(
    pdf_path: Path,
    models: list[str],
    api_key: str,
) -> list[ModelResult]:
    """Lance tous les modèles sur un PDF en parallèle."""
    logger.info("  PDF: %s", pdf_path.name)
    pdf_bytes = pdf_path.read_bytes()
    ocr_text: str = await asyncio.to_thread(extract_text_from_pdf, pdf_bytes)
    payload = _build_payload(ocr_text)
    tasks = [_call_model(m, api_key, payload, pdf_path.name) for m in models]
    return list(await asyncio.gather(*tasks))


# ── Rapport -----------------------------------------------------------------

def _build_summary(
    results: list[ModelResult],
    models: list[str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for model in models:
        mr = [r for r in results if r.model == model]
        ok = [r for r in mr if r.error is None]
        if not ok:
            summary[model] = {"errors": len(mr), "invoices_processed": len(mr)}
            continue
        summary[model] = {
            "invoices_processed": len(mr),
            "errors": len(mr) - len(ok),
            "avg_precision": round(sum(r.precision_score for r in ok) / len(ok), 4),
            "avg_latency_s": round(sum(r.latency_s for r in ok) / len(ok), 3),
            "total_cost_usd": round(sum(r.cost_usd for r in ok), 8),
            "avg_input_tokens": int(sum(r.input_tokens for r in ok) / len(ok)),
            "avg_output_tokens": int(sum(r.output_tokens for r in ok) / len(ok)),
            "per_field_precision": {
                fk: round(sum(r.field_scores.get(fk, False) for r in ok) / len(ok), 4)
                for fk in SCORED_FIELDS
            },
        }

    # Comparaison de coût entre les deux modèles
    if len(models) == 2:
        c_ref = summary.get(models[0], {}).get("total_cost_usd", 0.0)
        c_new = summary.get(models[1], {}).get("total_cost_usd", 0.0)
        if c_ref and c_ref > 0:
            summary["cost_delta_pct"] = round((c_new - c_ref) / c_ref * 100, 1)

    return summary


def _console_summary(summary: dict[str, Any], models: list[str]) -> None:
    W = 72
    print("\n" + "=" * W)
    print("  BENCHMARK GEMINI — Résumé comparatif")
    print("=" * W)

    col_w = max(26, (W - 36) // max(len(models), 1))
    header = f"  {'Métrique':<32}"
    for m in models:
        header += f"  {m[:col_w]:<{col_w}}"
    print(header)
    print("-" * W)

    rows: list[tuple[str, str]] = [
        ("Précision moy. (champs extraits)", "avg_precision"),
        ("Latence moy. (s)", "avg_latency_s"),
        ("Coût total estimé (USD)", "total_cost_usd"),
        ("Tokens input (moy.)", "avg_input_tokens"),
        ("Tokens output (moy.)", "avg_output_tokens"),
        ("Factures traitées", "invoices_processed"),
        ("Erreurs", "errors"),
    ]
    for label, key in rows:
        row = f"  {label:<32}"
        for m in models:
            val = summary.get(m, {}).get(key, "N/A")
            row += f"  {str(val):<{col_w}}"
        print(row)

    print(f"\n  {'Précision par champ :'}")
    for fk in SCORED_FIELDS:
        row = f"    {fk:<30}"
        for m in models:
            val = summary.get(m, {}).get("per_field_precision", {}).get(fk, "N/A")
            row += f"  {str(val):<{col_w}}"
        print(row)

    if "cost_delta_pct" in summary:
        delta = summary["cost_delta_pct"]
        direction = "moins cher" if delta < 0 else "plus cher"
        print(
            f"\n  Δ coût {models[1]} vs {models[0]} : "
            f"{abs(delta):.1f}% {direction}"
        )

    print("=" * W)
    print(
        "  AVERTISSEMENT : les prix sont des estimations."
        " Vérifiez https://ai.google.dev/pricing\n"
    )


# ── Entrypoint ---------------------------------------------------------------

async def main(argv: list[str] | None = None) -> int:
    """Point d'entrée principal du benchmark."""
    parser = argparse.ArgumentParser(
        description="Benchmark Gemini models on invoice extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fixtures-dir",
        default=str(_TESTS_DIR / "fixtures"),
        help="Dossier contenant les PDFs de test (défaut : tests/fixtures/)",
    )
    parser.add_argument(
        "--report-out",
        default="",
        metavar="PATH",
        help="Chemin de sortie du rapport JSON (défaut : stdout)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        help="Modèles Gemini à comparer",
    )
    parser.add_argument(
        "--max-invoices",
        type=int,
        default=10,
        help="Nombre max de PDFs à traiter (défaut : 10)",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY manquante — export GEMINI_API_KEY=<votre_clé>")
        return 1

    fixtures_dir = Path(args.fixtures_dir)
    if not fixtures_dir.exists():
        logger.error("Dossier fixtures introuvable : %s", fixtures_dir)
        return 1

    pdfs = sorted(fixtures_dir.glob("*.pdf"))[: args.max_invoices]
    if not pdfs:
        logger.error(
            "Aucun PDF trouvé dans %s — ajoutez des factures de test.", fixtures_dir
        )
        return 1

    logger.info(
        "Benchmark démarré : %d PDF(s), modèles=%s", len(pdfs), args.models
    )

    all_results: list[ModelResult] = []
    for pdf_path in pdfs:
        batch = await _benchmark_pdf(pdf_path, args.models, api_key)
        all_results.extend(batch)

    summary = _build_summary(all_results, args.models)
    report = BenchmarkReport(
        generated_at=datetime.now().isoformat(),
        models=args.models,
        fixture_count=len(pdfs),
        pricing_note=(
            "Prix estimatifs USD/1M tokens. "
            "Source: https://ai.google.dev/pricing"
        ),
        results=[asdict(r) for r in all_results],
        summary=summary,
    )

    _console_summary(summary, args.models)

    report_json = json.dumps(asdict(report), indent=2, ensure_ascii=False)
    if args.report_out:
        Path(args.report_out).write_text(report_json, encoding="utf-8")
        logger.info("Rapport JSON écrit : %s", args.report_out)
    else:
        print("\n--- RAPPORT JSON ---\n")
        print(report_json)

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(asyncio.run(main()))
