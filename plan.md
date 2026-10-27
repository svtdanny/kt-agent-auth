# Dedicated agent pilot

Implemented direction: one namespace per deployed Agent, dedicated Envoy + sdsmint + native authorizer + combined custom authorizer. ConfigMap policies are watched; GitLab/Vault credentials are mounted only into the custom authorizer. PostgreSQL persists immutable Session/ServiceAccount UID bindings.

The pinned Substrate API needs the tracked routing overlay. kagent, workers and atunnel remain unchanged. The old shared proxy, custom Pod enrollment, direct Vault client and separate processor/control-plane RPC are removed.

Validation: Go tests/race/vet, manifest validation, real Minikube native Sessions to internal HTTP MCP and external HTTPS, permission updates, Secret rotation, isolation, restart persistence, bounded load and SQL integration. Current results are recorded in artifacts/dedicated-*.json when tests complete.

Deferred: approval UI, generic TCP/PostgreSQL adapters, multiple deployed Agents in one namespace, production CNI/HA testing and a portable Helm release.
