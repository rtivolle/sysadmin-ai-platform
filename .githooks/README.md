# Git hooks du dépôt

Hooks locaux, versionnés dans `.githooks/` (activés via `core.hooksPath`,
aucune écriture dans `.git/hooks/`).

## Installation

```bash
./scripts/install-hooks.sh
```

Le script est idempotent : il configure `git config core.hooksPath .githooks`
dans le dépôt courant et vérifie que `pre-commit` / `pre-push` sont
exécutables. À relancer après un `git clone` frais (la config git n'est pas
clonée).

## pre-commit (bloquant, < ~10 s)

Ne regarde que les fichiers **stagés** :

1. **Scan de secrets sur les lignes ajoutées** — clés AWS (`AKIA…`),
   tokens GitHub (`ghp_…`, `gho_…`), clés privées PEM/OpenSSH,
   clés Stripe (`sk-live-…`, `sk-test-…`), tokens Slack (`xox…-`),
   et affectations `password|passwd|secret|api_key|token = <valeur>`
   qui ne ressemblent pas à un placeholder (`changeme`, `os.environ`,
   `None`, …). Chaque détection est rapportée `fichier:ligne`.
2. **`bash -n`** sur les `*.sh` stagés ; **`shellcheck`** en plus s'il est
   installé (avertissement seulement s'il est absent).
3. **Compilation Python** (`compile()`) sur les `*.py` stagés ;
   **`ruff check`** en plus s'il est installé (avertissement seulement
   s'il est absent).

## pre-push (rapide)

1. `make compile` (repli : `python3 -m compileall backend/services backend/tests`
   si le venv est absent) — un échec **bloque** le push ;
2. subset pytest rapide et hermétique
   (`backend/tests/tier1_unit/test_githooks.py`) **uniquement si**
   `backend/.venv` + pytest existent — sinon simple avertissement,
   jamais bloquant sur environnement incomplet.

## Contournement (faux positif)

```bash
git commit --no-verify   # contourne pre-commit
git push --no-verify     # contourne pre-push
```

À n'utiliser qu'en connaissance de cause — de préférence après avoir
signalé le faux positif pour ajuster les motifs.
