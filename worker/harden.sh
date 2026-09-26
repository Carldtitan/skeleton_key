#!/bin/bash
# Containment for sandbox and runner containers on a worker VM:
#  - no new connections from containers into the private VPC (control plane, other workers)
#  - no access to the cloud metadata service (it exposes the startup script)
# Replies to connections the control plane opens (CDP, noVNC) are still allowed.
set -e
for rule in \
  "-d 169.254.169.254/32 -j DROP" \
  "-d 10.10.0.0/24 -m conntrack --ctstate NEW -j DROP"; do
  iptables -C DOCKER-USER $rule 2>/dev/null || iptables -I DOCKER-USER $rule
done
