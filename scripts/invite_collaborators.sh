#!/usr/bin/env bash
# Make cell-note-agent private and invite collaborators (write access).
# Usage:
#   ./scripts/invite_collaborators.sh user1 user2 user3
# Or edit COLLABS below, then: ./scripts/invite_collaborators.sh
set -euo pipefail

REPO="inulxy/cell-note-agent"
# Default collaborators — replace with real GitHub usernames, or pass as args:
COLLABS=("$@")

if [[ ${#COLLABS[@]} -eq 0 ]]; then
  echo "Usage: $0 <github-username> [more-usernames...]"
  echo "Example: $0 alice bob charlie"
  exit 1
fi

echo "==> Auth check"
gh auth status -h github.com

echo "==> Set repo visibility to private"
gh repo edit "$REPO" --visibility private --accept-visibility-change-consequences

echo "==> Invite collaborators with write (push) permission"
for u in "${COLLABS[@]}"; do
  echo "  - inviting @$u ..."
  gh api -X PUT "repos/${REPO}/collaborators/${u}" \
    -f permission=push \
    --silent || echo "    (failed for @$u — check username)"
done

echo "==> Current collaborators"
gh api "repos/${REPO}/collaborators" --jq '.[] | "\(.login)\t\(.role_name // .permissions.push)"'

echo "==> Done. Repo: https://github.com/${REPO}"
echo "Invited users must accept the email/GitHub invitation."
