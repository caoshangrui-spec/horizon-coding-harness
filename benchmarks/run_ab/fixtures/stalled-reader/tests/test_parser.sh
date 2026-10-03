#!/bin/sh
set -eu

empty_result="$(sh src/parser.sh '')"
normal_result="$(sh src/parser.sh alpha)"

if [ "$empty_result" != "[]" ]; then
  printf 'empty input: expected [], got <%s>\n' "$empty_result"
  exit 1
fi
if [ "$normal_result" != "[alpha]" ]; then
  printf 'normal input: expected [alpha], got <%s>\n' "$normal_result"
  exit 1
fi

printf '2 passed\n'
