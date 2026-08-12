#!/usr/bin/env bash
# Mount skills/ and scripts/ into .pi/ for Pi coding agent discovery.
# Same pattern as scIsoAgent: keep skills/ and scripts/ as siblings, then link under .pi/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

mkdir -p .pi
ln -sfn ../skills  .pi/skills
ln -sfn ../scripts .pi/scripts

echo "[ok] Pi skill mount ready:"
echo "  $ROOT/.pi/skills  -> ../skills"
echo "  $ROOT/.pi/scripts -> ../scripts"
echo
echo "Next:"
echo "  cd $ROOT"
echo "  pi"
echo "  then in chat: /skill:sc-epi-agent"
echo
echo "Available skills:"
find skills -name SKILL.md | sed 's|skills/||;s|/SKILL.md||' | sort | sed 's/^/  - /'
