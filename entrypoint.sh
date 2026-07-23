#!/bin/sh
set -e

# Pick up any internal/private CA certs mounted into ca-certificates.d
# (see docker-compose.yml) so git/https can verify servers signed by them.
if [ -n "$(ls -A /usr/local/share/ca-certificates 2>/dev/null)" ]; then
  update-ca-certificates
fi

if [ -n "$GIT_USER_NAME" ]; then
  git config --global user.name "$GIT_USER_NAME"
fi

if [ -n "$GIT_USER_EMAIL" ]; then
  git config --global user.email "$GIT_USER_EMAIL"
fi

exec "$@"