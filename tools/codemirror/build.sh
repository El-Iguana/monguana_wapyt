#!/usr/bin/env bash
# Rebuild appcode/vendor/codemirror/monguana-editor.js from the pinned
# packages in package.json. The output is committed; node_modules is not.
#
#   tools/codemirror/build.sh
#
# Vendored rather than loaded from a CDN because pytincture's CSP only allows
# scripts from 'self'. One IIFE that sets window.MgEditor: see entry.js.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/../../appcode/vendor/codemirror"
cd "$HERE"
[ -d node_modules ] || npm ci --no-audit --no-fund
mkdir -p "$OUT"
npx esbuild entry.js --bundle --minify --format=iife --target=es2020 \
  --legal-comments=none --outfile="$OUT/monguana-editor.js"
# What went in, for anyone checking the vendored file against its sources.
{
  echo "monguana-editor.js — CodeMirror 6, bundled by tools/codemirror/build.sh"
  echo "esbuild $(npx esbuild --version)"
  npm ls --depth=0 --omit=dev 2>/dev/null | tail -n +2 | sed 's/^[├└]── //'
  echo "sha256 $(sha256sum "$OUT/monguana-editor.js" | cut -d' ' -f1)"
} > "$OUT/VERSION"
cp node_modules/@codemirror/view/LICENSE "$OUT/LICENSE"
ls -l "$OUT"
