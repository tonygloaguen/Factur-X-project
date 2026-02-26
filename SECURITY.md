# Security Policy

## Supported Versions

Les versions actuellement maintenues et supportées en matière de sécurité sont :

| Version                | Support sécurité |
| ---------------------- | ---------------- |
| main                   | ✅ Oui            |
| autres branches / tags | ❌ Non            |

Toute vulnérabilité doit être reproduite sur la branche `main` ou sur la dernière version publiée.

---

## Reporting a Vulnerability

Si vous découvrez une vulnérabilité de sécurité, **merci de ne pas ouvrir d’issue publique**.

Utilisez l’un des canaux suivants :

### Option recommandée (GitHub)

* Utilisez la fonctionnalité **Private vulnerability reporting** du dépôt GitHub
  (bouton *“Report a vulnerability”* dans l’onglet **Security**).

### Alternative (si nécessaire)

* Contact direct par email :
  **[security@TODO-domain.example](mailto:security@TODO-domain.example)**
  (à remplacer si besoin)

Merci d’inclure :

* Une description claire de la vulnérabilité
* Les étapes de reproduction
* L’impact potentiel (données, intégrité, disponibilité)
* Toute preuve utile (logs, captures, PoC)

---

## Disclosure Policy

* Accusé de réception sous **72 heures**
* Analyse et qualification sous **7 jours**
* Correctif ou mitigation dès que possible selon la sévérité
* Publication d’un **Security Advisory GitHub** si nécessaire

Les signalements responsables sont appréciés. Aucune action légale ne sera engagée contre les chercheurs agissant de bonne foi.

---

## Scope

Inclus :

* Code source du dépôt
* Pipelines CI/CD (GitHub Actions)
* Dépendances déclarées
* Configuration exposée dans le dépôt

Exclus :

* Infrastructures tierces
* Services externes non maintenus dans ce dépôt
* Attaques de type DoS / brute-force

---

## Security Best Practices (maintainers)

* Analyse SAST / CodeQL activée
* Dependabot activé pour les dépendances
* Secret scanning activé
* Revue obligatoire via Pull Request

---

Merci pour votre contribution à la sécurité du projet.
