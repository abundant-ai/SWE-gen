#!/bin/bash

cd /app/src

# Set environment variables for tests
export CI=true

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "test/helpers"
cp "/tests/helpers/server.js" "test/helpers/server.js"
mkdir -p "test/unit/adapters"
cp "/tests/unit/adapters/http.js" "test/unit/adapters/http.js"

# Run ONLY the specific test files using Mocha
npx mocha test/unit/adapters/http.js --timeout 30000 --exit
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
