# Project namespace bundle

Owner: DevOps/project automation. Apply once per GitLab repository/project. Multiple Agents in this namespace reuse its WorkerPool and dedicated proxy. Reconcile these shared files through the project deployment pipeline.

| File | Resources |
| --- | --- |
| `namespace.yaml` | Project namespace with the upstream API-access integration label |
| `worker-pool.yaml` | Shared gVisor WorkerPool; configure runtime/capacity here |
| `egress-deployment.yaml`, `services.yaml` | One project Envoy/native authorizer/combined authorizer/sdsmint bundle and two Services |
| `authorizer-rbac.yaml` | Project runtime ServiceAccount, watch permissions and reviewer binding |
| `egress-policy.yaml` | Shared native EgressPolicy for transport origins |
| `permissions.yaml` | Managed logical ServiceAccount and watched permission ConfigMap |
| `callers.yaml` | Example authenticated caller identity and its account-selection permission |
| `envoy/` | Envoy and SDS YAML, assembled into ConfigMaps by the renderer |
| `gitlab-ci.example.yml` | Project Vault → mounted Kubernetes Secret delivery example |

The reviewer binding is cluster-scoped, so the platform deployment identity must apply that part. Project runtime has no Secret-read API rights. The example caller is a Kubernetes ServiceAccount using the bridge's current `auth-demo` audience; replace the example binding with the intended callers. It is separate from the GitLab deployment identity and the worker's platform identity.

Before the gateway becomes ready, provision these Secrets in the project namespace:

| Secret | Required keys | Source |
| --- | --- | --- |
| `application-credentials` | `credentials.json` | Project Vault paths, delivered by CI |
| `authorizer-db` | `DATABASE_URL` | Dedicated login/database on the existing PostgreSQL server |
| `authorizer-tls` | `ca.crt`, `tls.crt`, `tls.key` | Per-project local mTLS trust; server SAN `authorizer` |
| `auth-client` | `ca.crt`, `tls.crt`, `tls.key` | Same project's gateway client certificate, CN `auth-egress` |
| `actor-id-ca-certs` | `ca.crt` | Platform Actor identity verification trust |
| `egress-mitm-ca-pool` | `pool` | Platform sdsmint issuer pool |

Platform PodCertificate and ClusterTrustBundle projections supply the other identities and PostgreSQL trust. Operations clients of `/internal/*` need this project's client trust and CN `ext-proc`. Rotate local mTLS/database settings with a controlled replacement; application Secret projection updates are detected without restart.

Only the custom authorizer mounts `application-credentials`. Do not mount it into workers, Agent images or Envoy. The Secret document supports entries such as `{"api-token":{"type":"bearer","token":"..."}}` and OAuth client credentials; real values belong in Vault/CI, not these YAML files. Create the private `auth_bridge` schema before startup; the authorizer creates its own tables.

`EgressPolicy` supplies coarse origins; the ConfigMap supplies complete method/path/destination grants and credential references. Existing Sessions pick up policy updates eventually through watches. No scheduled policy/Secret API polling is added. The shared example ServiceAccount can be selected by Sessions belonging to different Agents in this project. Add further identities/policies when permissions differ; policies may also check the Agent binding.

Render with `--bundle namespace`, apply the rendered file, deliver Secrets, then wait for `deployment/agent-egress` and the WorkerPool. The project API accepts only bindings belonging to this namespace. Different projects retain separate credential caches, database logins and local mTLS trust.
