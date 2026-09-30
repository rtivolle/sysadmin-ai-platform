#!/usr/bin/env bash
# install-hooks.sh — installe les git hooks du dépôt (idempotent).
# Configure git core.hooksPath=.githooks dans le dépôt courant et vérifie
# que les hooks sont exécutables. Relançable sans effet de bord.
set -euo pipefail

repo="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || { printf 'install-hooks: erreur: pas dans un dépôt git\n' >&2; exit 1; }
cd "$repo"

if [ ! -d .githooks ]; then
    printf 'install-hooks: erreur: .githooks introuvable à la racine du dépôt (%s)\n' "$repo" >&2
    exit 1
fi

git config core.hooksPath .githooks

ok=1
for hook in pre-commit pre-push; do
    if [ -f ".githooks/$hook" ]; then
        chmod +x ".githooks/$hook"
        if [ ! -x ".githooks/$hook" ]; then
            printf 'install-hooks: avertissement: .githooks/%s non exécutable\n' "$hook" >&2
            ok=0
        fi
    else
        printf 'install-hooks: avertissement: .githooks/%s absent\n' "$hook" >&2
        ok=0
    fi
done

current="$(git config --get core.hooksPath || true)"
printf 'install-hooks: core.hooksPath=%s\n' "$current"

if [ "$current" = ".githooks" ] && [ "$ok" = 1 ]; then
    printf 'install-hooks: hooks installés et exécutables — OK\n'
else
    printf 'install-hooks: installation partielle — voir les avertissements ci-dessus\n' >&2
    exit 1
fi
