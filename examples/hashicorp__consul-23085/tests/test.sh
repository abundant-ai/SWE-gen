#!/bin/bash

cd /app/src

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "agent/config"
cp "/tests/agent/config/runtime_test.go" "agent/config/runtime_test.go"
mkdir -p "agent/consul/state"
cp "/tests/agent/consul/state/virtual_ips_test.go" "agent/consul/state/virtual_ips_test.go"

# Run specific test packages for the PR
go test -v ./agent/consul/state -run "TestParseVirtualIPCIDR|TestSetVirtualIPConfigOverrides"
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
