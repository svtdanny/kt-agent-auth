# Project deployment bundles

One GitLab project maps to one namespace, one shared WorkerPool and one dedicated Envoy/native authorization/sdsmint/combined authorizer bundle. Agents in the project reuse this infrastructure. Session → Agent → ServiceAccount UID bindings remain immutable in PostgreSQL; project credentials are mounted only into its authorizer.

Canonical YAML and deployment instructions are in k8s/cluster, k8s/namespace and k8s/agent. The local installer reads the same sources. The existing Substrate namespace routing overlay is unchanged; kagent and worker binaries remain unchanged.

Verify Go tests/race/vet, bundle rendering, internal/external HTTP rules and two real Agents sharing one project pool/proxy. Production readiness, availability and release packaging requirements remain in docs/devops-production-task.md.
