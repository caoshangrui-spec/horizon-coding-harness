#!/bin/sh
set -eu

actual="$(sh src/greet.sh Codex)"
expected="Hello, Codex!"

if [ "$actual" != "$expected" ]; then
  echo "expected: $expected"
  echo "actual:   $actual"
  exit 1
fi

echo "greeting regression passed"
