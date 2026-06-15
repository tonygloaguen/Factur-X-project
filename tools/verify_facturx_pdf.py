#!/usr/bin/env python3
"""
verify_facturx_pdf.py — Preuve indépendante de validité d'un PDF Factur-X
==========================================================================

Vérifie qu'un PDF Factur-X local contient un XML EN16931 réellement valide
contre le XSD ET le schematron officiel (lib factur-x / moteur Saxon).

Indépendant : n'utilise NI Gmail NI Google Drive, aucun mock. Prend en entrée
le chemin local du PDF Factur-X généré (à récupérer depuis Drive ou le serveur).

Usage :
    python3 tools/verify_facturx_pdf.py /chemin/vers/facture.pdf

Codes de sortie :
    0 → OK (XSD + schematron valides)
    2 → fichier introuvable / illisible
    3 → aucun XML Factur-X trouvé dans le PDF
    4 → schematron rejette BR-CO-25 (montant dû positif sans BT-9 ni BT-20)
    5 → autre échec de validation XSD ou schematron

Aucune dépendance au reste du projet : seul ``factur-x`` est requis
(présent dans requirements-ci.txt et dans l'image du conteneur).
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Valide un PDF Factur-X (XSD + schematron officiel)."
    )
    parser.add_argument("pdf_path", help="Chemin local du PDF Factur-X à vérifier")
    args = parser.parse_args()

    pdf_path = args.pdf_path
    if not os.path.isfile(pdf_path):
        print(f"ERREUR - fichier introuvable : {pdf_path}", file=sys.stderr)
        return 2

    try:
        import facturx
    except ImportError as exc:  # pragma: no cover - dépendance d'environnement
        print(
            f"ERREUR - la librairie 'factur-x' n'est pas installée : {exc}",
            file=sys.stderr,
        )
        return 2

    # 1) Extraction SANS validation : get_xml_from_pdf(check_*=True) ne lève PAS
    #    sur un XML invalide — il l'ignore et renvoie (None, None). On extrait donc
    #    le XML brut, puis on valide explicitement pour pouvoir distinguer les motifs.
    try:
        with open(pdf_path, "rb") as fh:
            xml_filename, xml_bytes = facturx.get_xml_from_pdf(
                fh, check_xsd=False, check_schematron=False
            )
    except Exception as exc:  # noqa: BLE001
        print(f"ERREUR - lecture du PDF impossible : {exc}", file=sys.stderr)
        return 2

    # (None, None) n'est JAMAIS une validation réussie : c'est l'absence de XML.
    if not xml_filename or not xml_bytes:
        print(
            f"ECHEC - PDF sans XML Factur-X embarqué : {pdf_path}",
            file=sys.stderr,
        )
        return 3

    # 2) Validation XSD officielle (lève si invalide).
    try:
        facturx.xml_check_xsd(xml_bytes, flavor="factur-x", level="en16931")
    except Exception as exc:  # noqa: BLE001
        print(f"ECHEC - XSD invalid : {exc}", file=sys.stderr)
        return 5

    # 3) Validation schematron officielle (lève si invalide) — Saxon/saxonche.
    try:
        facturx.xml_check_schematron(xml_bytes, flavor="factur-x", level="en16931")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "BR-CO-25" in message:
            print(
                "ECHEC - Schematron invalid (BR-CO-25) : montant dû positif sans "
                "échéance (BT-9) ni conditions de paiement (BT-20).",
                file=sys.stderr,
            )
            print(message, file=sys.stderr)
            return 4
        print(f"ECHEC - Schematron invalid : {message}", file=sys.stderr)
        return 5

    print("OK - PDF Factur-X valide XSD + schematron")
    print(f"     fichier   : {pdf_path}")
    print(f"     XML       : {xml_filename} ({len(xml_bytes)} octets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
