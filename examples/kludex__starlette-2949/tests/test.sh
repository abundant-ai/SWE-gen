#!/bin/bash

cd /app/src

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "tests"
cp "/tests/test_exceptions.py" "tests/test_exceptions.py"

# Run mypy to check types in the test file
mypy tests/test_exceptions.py
mypy_status=$?

# If mypy passes, run the actual tests
if [ $mypy_status -eq 0 ]; then
    pytest -xvs tests/test_exceptions.py
    test_status=$?
else
    test_status=$mypy_status
fi

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
