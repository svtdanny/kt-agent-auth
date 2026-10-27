# Shared Envoy pilot: deployment handoff

Readiness audit: 2026-10-27. Scope: the kagent/Substrate and enrolled-Pod paths in the main demo, independently of the OpenShell comparison pilot.

The common-proxy implementation and local deployment assets are complete for the Minikube demo. DevOps can begin preparing a controlled pilot. A portable release for the target cluster is still required: the current scripts are installers for `auth-demo`, not a general cluster installer. Do not apply the entire `deploy/` directory to another cluster or run the local bootstrap against an existing platform installation.

## What is already delivered

Both caller types use one `atenet-egress` Deployment/Service:

```text
kagent sandbox -> existing worker atunnel ----+
                                             +-> shared Envoy -> internal MCP / external HTTPS
enrolled Pod -> capture -> Envoy TCP relay --+

shared Envoy -> our ext_proc -> our control plane -> PostgreSQL / Vault
                                             <- Kubernetes policy/Pod watches
```

The forwarding gateway, native authorization helper, and SDS certificate minter run together. The processor and control plane are separate Deployments. No kagent/Substrate source patches are required.

| Asset | Current purpose |
| --- | --- |
| [shared-envoy.yaml](../deploy/shared-envoy.yaml) | Shared gateway Deployment, Service, configuration/trust ConfigMaps and gateway ingress policy |
| [command-runner.yaml](../deploy/command-runner.yaml) | Enrolled application Pod template, capture init and TCP relay |
| [pod-egress-demo.yaml](../deploy/pod-egress-demo.yaml) | Enrolled Pod, ServiceAccount permissions and internal MCP authentication mock; no separate egress gateway |
| [deploy.py](../scripts/deploy.py) | Generates/applies control plane, processor, RBAC, database/Vault integration, native Agent/Harness and the shared gateway locally |
| [permissions_demo.py](../scripts/permissions_demo.py) | Local fixture setup, Pod UID certificate issuance and enrollment |
| [up.py](../scripts/up.py) | Local upstream install, image build/load, deployment and enrollment sequence |

The gateway YAML matches its source generator, and the Pod relay targets the same gateway Service. TLS/identity Secrets and database credentials are deliberately supplied separately. The public YAML is not a complete standalone installation.

Existing evidence includes the [native/Pod route matrix](../artifacts/shared-gateway-results.json), [native benchmark](../artifacts/benchmark-results.json), and [combined benchmark](../artifacts/benchmark-shared-envoy-results.json), measured on 2026-10-24. Those benchmarks completed 30,770 allowed requests and blocked 7,815 forbidden requests across the real forwarding paths. The combined run included 80 offered concurrent requests. This evidence does not establish compatibility or capacity on the pilot cluster.

The audit corrected the gateway NetworkPolicy to admit native traffic on container port 8443 and readiness/metrics on 15090, in addition to enrolled-Pod traffic on 9443. The previous policy selected the entire gateway Pod but allowed only 9443. The local CNI does not enforce NetworkPolicy, so a regression check now catches that manifest error offline; actual enforcement must still be tested on the pilot CNI.

The corrected policy passed Kubernetes API server dry-run validation and was applied to the local demo. Eight fresh [readiness smoke checks](../artifacts/pilot-readiness-smoke.json) then passed: native Session and enrolled Pod, each calling internal MCP and external HTTPS with matching and nonmatching HTTP permissions. Each allowed call used the expected credential, and every denied call added zero upstream audit events. Eight offline bundle tests also pass. These checks do not claim enforcement was exercised by the local CNI.

## Deployment work to finish

| Task | Owner | Deliverable |
| --- | --- | --- |
| Package the target-cluster release | Application/platform engineering | One versioned Helm release or Kustomize overlay containing the gateway, control plane, processor, RBAC, native integration, and an optional example Pod; separate mocks from runtime components |
| Remove local assumptions | Application/platform engineering | Explicit context, namespaces, cluster DNS suffix, service addresses, registry/image digests, database/Vault/TLS references and destination routes; no `localhost:5001`, `imagePullPolicy: Never`, Docker-network IPs or cached Minikube ClusterIPs in the pilot render |
| Make upgrades repeatable | Application/platform engineering + DevOps | The shared Envoy replacement is part of the release rendering. A normal upstream upgrade cannot silently restore agentgateway or discard our filters/listeners. There is one owner for the gateway resources |
| Confirm cluster compatibility | DevOps | Target Kubernetes/CNI/runtime versions, required certificate APIs/projections and sandbox runtime work with the pinned kagent/Substrate versions |
| Publish images | Engineering CI + DevOps | Pullable immutable images for control plane, processor, Mastra adapter/application, Pod runner, and the native helper built from unchanged pinned Substrate source; Envoy pinned to the tested version/digest |
| Provide PostgreSQL access | DBA/DevOps | Dedicated login and private `auth_bridge` schema on the agreed server, verified TLS and an allocated connection budget; native kagent schemas remain untouched |
| Provide Vault integration | Vault owner + DevOps | Existing Vault HTTPS endpoint/CA, KV v2 paths and narrow Kubernetes-auth role for the control-plane ServiceAccount; pilot credentials provisioned separately |
| Provide certificate lifecycle | Platform engineering + DevOps | Native identity projections/interception CA, our RPC/listener certificates and an enrollment process for the test Pod; expiry/rotation and required process rollouts are documented |
| Adapt acceptance runners | Application/platform engineering | Tests take explicit pilot context/endpoints, replace the Docker-only external fixture with a reachable staging endpoint, and record results without credentials |
| Execute deployment and acceptance | DevOps + application engineering | Both caller types, both target locations, policy changes, credential rotation, recovery, bypass checks and target-CNI behavior pass |

These are concrete remaining tasks, not features already implemented by a portable chart. Runtime services mostly expose environment configuration; the installer and generated manifests still contain the local defaults.

## Inputs DevOps needs to provide

- Target cluster/context, Kubernetes version, CNI, cluster DNS suffix, node architecture and sandbox runtime support. The tested setup uses Kubernetes 1.37 with `ClusterTrustBundle`, `ClusterTrustBundleProjection` and `PodCertificateRequest` enabled, and gVisor workers. Other combinations require validation before deployment.
- Existing kagent/Substrate versions and whether the cluster already has unrelated native workloads. The tested versions are kagent `1.0.0-alpha17`, Substrate `0.6.0-alpha3`, and Envoy `1.38.6`; an upgrade is a separate compatibility change.
- Namespace names, image registry/pull credentials, PostgreSQL endpoint/database/CA, Vault endpoint/CA/auth mount/role/KV mount/prefix, and the certificate issuer used for our identities.
- An internal MCP staging Service and an external HTTPS staging endpoint, including method/path grants and dedicated pilot credentials. Initial tests may use authentication mocks.
- Expected pilot concurrency, resource budget and acceptable latency; local measurements are a reference rather than an SLO.

For the first pilot, use a dedicated non-production cluster with both caller types sharing the gateway. The current native integration replaces the platform's existing `atenet-egress` Deployment, and our processor denies unknown/unregistered Session identities. A namespace alone does not isolate that replacement from other native workers using the same gateway. If the pilot must share a cluster with unrelated kagent workloads, first implement and validate a separate gateway assignment for the pilot workers/Actors; do not assume that isolation is already delivered.

## Required configuration

Keep one gateway, one processor, and one control-plane replica initially. This preserves the tested cache/invalidation behavior. PostgreSQL mappings survive control-plane Pod replacement. High availability, distributed credential-cache invalidation and automatic custom-Pod enrollment controllers are outside this first pilot.

The control plane uses `DEMO_NAMESPACE`, `KAGENT_TARGET`, `DATABASE_URL`, `VAULT_ADDR`, `VAULT_CACERT`, `VAULT_NAMESPACE` when required, `VAULT_AUTH_MOUNT`, `VAULT_ROLE`, `VAULT_JWT_PATH`, `VAULT_KV_MOUNT`, `VAULT_KV_PREFIX` and `VAULT_KV_FIELD`. The processor uses `CONTROL_PLANE_URL`. TLS files mount at `/tls/{ca.crt,tls.crt,tls.key}`. Public caller TokenReview currently expects audience `auth-demo`; projected caller tokens must use that audience unless engineering makes it configurable.

Vault application keys live at `<kv-mount>/data/<prefix>/<workload-namespace>/<credential-name>` with the configured value field, `token` by default. Bind the Kubernetes-auth role to the actual control-plane ServiceAccount and namespace, with JWT audience `vault`. Grant reads only on the selected application-key paths. Target mocks have separate roles/expected keys. Do not carry the local Vault root token, unseal material, file-storage bootstrap or synthetic fixture keys into the pilot release.

Our RPC authorization also checks certificate roles: the gateway client certificate Common Name is `agentgateway` or `auth-egress`; the processor's client certificate Common Name is `ext-proc`. Preserve the required DNS SANs and trust bundles. The application services load their TLS files at startup, so certificate replacement requires their rollout. The SDS minter handles destination leaf issuance separately using the native interception CA.

The database role owns only `auth_bridge`; supply the DSN and trust bundle through deployment infrastructure. The demo uses a four-connection bridge pool; allocate all platform pools with the DBA instead of copying local database administration/ConfigMap patches. There is no bbolt volume requirement for a fresh deployment.

The custom-Pod template needs a root capture init with `NET_ADMIN`/`NET_RAW` inside that Pod's network namespace. Its application stays non-root with capabilities dropped; relay credentials mount only in the relay. Review those exceptions against the cluster's admission/runtime policy. The current capture fixture is IPv4 and rejects non-loopback IPv6. Node-local DNS, service-mesh interception, dual-stack traffic and target-CNI Service translation require explicit validation if present.

The capture rules need the target gateway and DNS Service IPs generated for the pilot cluster. A replacement application Pod has a new UID and must receive a matching identity certificate and `POST /internal/pods` enrollment. Manual trusted enrollment is acceptable for the bounded pilot, with a documented replacement procedure; do not enable unattended replacements and assume the old certificate will work.

## Communication paths to allow

This table documents required reachability; it does not request new restrictions for unrelated workloads or target Services.

| Source | Destination | Transport |
| --- | --- | --- |
| Public pilot client/launcher | Control plane | HTTPS 8443; caller bearer authentication, internal enrollment uses the authorized mTLS role |
| Native worker atunnel | Shared gateway Service 443 -> container 8443 | Native Actor mTLS CONNECT |
| Enrolled Pod relay | Shared gateway Service 8443 -> container 9443 | Pod UID mTLS CONNECT |
| Shared Envoy | Our processor | gRPC mTLS 50051, ALPN h2, TLS 1.3 |
| Our processor | Control plane | HTTPS mTLS 8443 |
| Native gateway authorization helper | Existing Substrate API | Existing platform gRPC/TLS path on Service 443 |
| Control plane | kagent controller | Existing private gRPC API, currently port 8083 |
| Control plane | Kubernetes API | HTTPS for watches, caller authorization, cache-miss reconciliation and cleanup |
| Control plane | Vault | HTTPS on the configured endpoint |
| Control plane | PostgreSQL | Verified TLS, usually TCP 5432 |
| Gateway | Internal MCP / external endpoint | Exact configured origins and ports; TLS verification for HTTPS |
| Workers/relay/gateway | Cluster DNS | TCP/UDP 53 as required by the cluster |

Native kagent origins must also be admitted by its existing `EgressPolicy`. Enrolled-Pod targets currently need an explicit gateway route/upstream. Those routes provide transport; the bound ServiceAccount's permission set grants origin/method/path and selects the Vault key. Installing a route alone does not grant access.

## Deployment sequence

1. Engineering delivers a target-specific rendered release and adapted test runner. Validate it offline and against the target API without applying. Confirm a single gateway, both listeners, private certificate references, and no local IP/image/fixture assumptions.
2. DevOps provisions the pinned upstream platform/runtime in the isolated cluster, reachable images, database schema/login, Vault paths/role and certificate dependencies. Capture the previous gateway Deployment/Service/configuration/policies and release values for rollback.
3. Deploy control plane and processor, then the shared gateway. Install the pilot Agent/Harness and native egress origins. Keep the native Service address stable for existing worker tunnel integration.
4. Install the opted-in Pod template, issue its actual Pod UID identity and enroll it. Launch native Sessions through the bridge with explicit ServiceAccount selection; direct native creation does not establish our binding.
5. Execute the acceptance checks below and the agreed bounded load. Save sanitized evidence containing versions/digests, configuration, test results, upstream audit counts, counters and resource samples.

The local `scripts/up.py`, `scripts/database.py`, `scripts/vault_demo.py` and `scripts/external_fixture.py` are reference implementations, not steps to execute unchanged in the pilot. They create a local cluster, administer its database/Vault and modify its CoreDNS for a Docker fixture. The current upstream Helm post-renderer still operates on agentgateway before the later shared-Envoy replacement; it is not yet the portable shared-gateway release integration.

## Acceptance checks

| Check | Required result |
| --- | --- |
| Native Session and enrolled Pod -> internal MCP | Matching grant succeeds with the exact expected key; missing grant returns 403 and adds zero upstream calls |
| Both callers -> external HTTPS | Matching grant succeeds with verified TLS and the exact external key; missing grant never reaches upstream |
| Concurrent identities and multiple grants | Different ServiceAccounts preserve their own grants/keys through the same gateway under load |
| Watched policy changes | Existing Session ID/Pod UID reflects grant addition/removal eventually, without gateway replacement or policy polling |
| Identity/Authorization spoofing | Application headers cannot change the binding or retain a forged credential |
| Enrolled-Pod bypass attempts | Service IP, Pod IP, direct external TCP, non-DNS UDP and unsupported IPv6 cannot bypass the Pod's enforced path |
| Unrelated workload | Existing direct access to internal services remains unchanged |
| Target CNI | Native container port 8443 remains reachable; enrolled sources reach 9443; the gateway ingress rule does not block native traffic; Pod capture works with the target Service/NAT implementation |
| Vault cache/rotation | Warm load adds no Vault reads/logins or ServiceAccount/Pod GETs; explicit invalidation makes the next approved request use the updated Vault value |
| Dependency outage | Processor/control-plane failure cannot release an application request or credential; approved safe reads recover after restoration |
| Restart/replacement | Control-plane restart preserves bindings; gateway restart restores both paths; a new application Pod UID requires fresh enrollment |
| Cleanup | Deleted native Sessions are revoked by cleanup; suspended Sessions remain valid; retained rows remain available for troubleshooting |
| Load | No authorization bypasses, wrong keys, failed allowed requests or unmatched upstream audit counts at the agreed load; latency and resource use are recorded |

The current processor inspects headers only: this pilot validates HTTP destination/method/path permissions and credential insertion. MCP JSON-RPC tool names and bodies are not filtered. HTTPS clients must trust the native interception CA and support the interception path; certificate-pinned clients, opaque TCP/TLS passthrough and arbitrary protocols are outside this tested configuration.

## Operation and rollback

Retain the native interception CA and database state across gateway/application rollouts. Renew our infrastructure certificates before expiry and roll their consumers. After changing a cached application key in Vault, invoke the authenticated credential-invalidation endpoint; there is no periodic Vault refresh. Re-enroll an application Pod after UID replacement, and regenerate its capture settings after gateway/DNS Service IP changes.

On rollback, stop pilot workloads before removing enforcement. Restore the previously captured gateway Deployment, Service ports, ConfigMaps and NetworkPolicies together; rolling back only the Deployment leaves incompatible listener ports/policies behind. Restore prior platform release configuration as needed. Keep the `auth_bridge` schema and Vault paths for diagnosis; remove only pilot-owned resources. Existing MCP ingress and unrelated workload policies remain unchanged.

Local verification of the public deployment assets:

```bash
python3 scripts/test_deploy_bundle.py
```

These offline checks validate bundle/generator agreement, shared routing, listener ports, ingress allowances and processor failure behavior. They do not replace target-cluster NetworkPolicy, runtime, certificate, Vault or end-to-end acceptance tests.
