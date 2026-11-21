#!/bin/sh
# Session hook shim (core.hooksPath). Enforcing hooks go through githook.py;
# every other hook chains straight to the repository's own hook.
name=${SDLC_HOOK_NAME:-$(basename "$0")}
here=$(cd "$(dirname "$0")" && pwd -P)
case "$name" in
  pre-push|pre-commit|pre-merge-commit) exec python3 "$here/githook.py" "$name" "$@" ;;
  reference-transaction) [ "$1" = prepared ] && exec python3 "$here/githook.py" "$name" "$@" ;;
esac
dir=""
for scope in --local --global --system; do
  v=$(git config "$scope" --get core.hooksPath 2>/dev/null) && [ -n "$v" ] && { dir=$v; break; }
done
if [ -z "$dir" ]; then
  dir="$(git rev-parse --git-common-dir 2>/dev/null)/hooks"
else
  case "$dir" in "~"*) dir="$HOME${dir#\~}";; /*) ;; *) dir="$(git rev-parse --show-toplevel)/$dir";; esac
fi
[ "$(cd "$dir" 2>/dev/null && pwd -P)" = "$here" ] && exit 0
[ -x "$dir/$name" ] && exec "$dir/$name" "$@"
exit 0
