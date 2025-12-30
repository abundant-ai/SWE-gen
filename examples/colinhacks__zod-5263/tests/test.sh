#!/bin/bash

cd /app/src

# Environment variables already set in Dockerfile

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "packages/zod/src/v4/classic/tests"
cp "/tests/packages/zod/src/v4/classic/tests/recursive-types.test.ts" "packages/zod/src/v4/classic/tests/recursive-types.test.ts"

# Run vitest on the specific test file (disable coverage for subset runs)
npx vitest run \
  packages/zod/src/v4/classic/tests/recursive-types.test.ts \
  --coverage.enabled=false
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
