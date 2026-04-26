#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Apply selected ArgoCD Application manifests to local MicroK8s
#
# Usage:
#   ./deploy.sh [--env local|staging] [--dry-run]
#
# Edit services.local.yaml to choose which services get deployed,
# then run this script.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV="local"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --env) ENV="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

CONFIG_FILE="$SCRIPT_DIR/services.${ENV}.yaml"
ARGOCD_APPS_DIR="$SCRIPT_DIR/argocd/${ENV}/apps"
ROOT_APP="$SCRIPT_DIR/argocd/${ENV}/root-app.yaml"

[[ ! -f "$CONFIG_FILE" ]] && { echo "Config not found: $CONFIG_FILE"; exit 1; }

if ! command -v yq &>/dev/null; then
  echo "Error: 'yq' is required."
  echo "Install: snap install yq  OR  brew install yq"
  exit 1
fi

ENABLED=$(yq '.services[] | select(.enabled == true) | .name' "$CONFIG_FILE")

if [[ -z "$ENABLED" ]]; then
  echo "No services enabled in $CONFIG_FILE"
  exit 0
fi

echo "======================================"
echo " Environment : $ENV"
echo " Dry run     : $DRY_RUN"
echo " Services    :"
echo "$ENABLED" | sed 's/^/   - /'
echo "======================================"
echo ""

KUBECTL="kubectl"
[[ "$DRY_RUN" == "true" ]] && KUBECTL="echo [DRY RUN] kubectl"

ROOT_EXISTS=$(kubectl get application "root-app-${ENV}" -n argocd --ignore-not-found 2>/dev/null || true)
if [[ -z "$ROOT_EXISTS" ]]; then
  echo ">>> Bootstrapping root-app for env: $ENV"
  $KUBECTL apply -f "$ROOT_APP"
  echo ""
fi

while IFS= read -r svc; do
  APP_FILE="$ARGOCD_APPS_DIR/${svc}.yaml"
  if [[ ! -f "$APP_FILE" ]]; then
    echo "WARNING: No manifest for '$svc' at $APP_FILE — skipping"
    continue
  fi
  echo ">>> Deploying: $svc"
  $KUBECTL apply -f "$APP_FILE"
done <<< "$ENABLED"

echo ""
echo "Done. ArgoCD will sync automatically."
echo "Check: kubectl get applications -n argocd"
