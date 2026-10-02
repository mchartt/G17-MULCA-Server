#!/bin/sh
# Docker secrets arrive as files under /run/secrets, but the server reads
# plain environment variables. Export each secret whose file name is a valid
# variable name (e.g. OPENAI_API_KEY), unless that variable is already set.
set -e

if [ -d /run/secrets ]; then
    for file in /run/secrets/*; do
        [ -f "$file" ] || continue
        name=$(basename "$file")
        case "$name" in
            *[!A-Za-z0-9_]* | [0-9]*) continue ;;
        esac
        eval "current=\${$name:-}"
        if [ -z "$current" ]; then
            export "$name=$(cat "$file")"
        fi
    done
fi

exec "$@"
