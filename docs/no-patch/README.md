# No-patch architecture

The newer [generic Envoy configuration overlay](../../k8s/shared-egress/README.md) replaces the fixed namespace map with `authorizer-extproc.<verified-namespace>.svc:50052`. It composes with the pinned upstream manifest and requires no per-project gateway update. Its isolated real-Envoy test adds a third authorizer after startup while an existing response stream stays active. It needs a common gateway mTLS identity/trust setup; it has not replaced the running cluster deployment. The diagrams and rendered YAML below document the earlier hardcoded two-project experiment.

| View | SVG | PDF | PNG |
| --- | --- | --- | --- |
| Shared gateway, project namespaces and independent scaling | [Open](architecture-scaling.svg) | [Print](architecture-scaling.pdf) | [Preview](architecture-scaling.png) |
| Credential delivery, encryption, database isolation and table schemas | [Open](architecture-credentials.svg) | [Print](architecture-credentials.pdf) | [Preview](architecture-credentials.png) |
| Concurrent token refresh, local cache checks and failure recovery | [Open](refresh-protocol.svg) | [Print](refresh-protocol.pdf) | [Preview](refresh-protocol.png) |

One shared native Envoy gateway carries application traffic. Its routes select a namespace's `ext_proc` Service directly, using the authenticated Actor's atespace from `dev.ate.actor.identity`. The current kagent mapping makes that atespace the project namespace. Request headers cannot select another project. There is no dispatcher or second application Envoy hop.

The stock Substrate API receives one configuration value:

```text
--default-egress-gateway-address=shared-egress.ate-system.svc:443
```

The gateway's per-route `ExtProcPerRoute` overrides have these fixed targets:

| Actor atespace | Authorizer endpoint |
| --- | --- |
| `auth-agent-a` | `authorizer-extproc.auth-agent-a.svc:50052` |
| `auth-agent-b` | `authorizer-extproc.auth-agent-b.svc:50052` |

The exact Envoy configuration is available as [rendered YAML](envoy.yaml). Its source is [scripts/shared_egress.py](../../scripts/shared_egress.py):

| Configuration | Code |
| --- | --- |
| Fixed atespace → namespace Service DNS map | [Lines 15–18](../../scripts/shared_egress.py#L15-L18) |
| Trusted Actor identity match and per-route processor selection | [Lines 40–51](../../scripts/shared_egress.py#L40-L51) |
| Namespace upstream clusters, port 50052 and project mTLS | [Lines 52–73](../../scripts/shared_egress.py#L52-L73) |
| Fail-closed default processor | [Lines 74–77](../../scripts/shared_egress.py#L74-L77) |

Regenerate the YAML with `python3 scripts/shared_egress.py > docs/no-patch/envoy.yaml`. The [live test](../../scripts/test_shared_egress.py#L52-L102) mounts it as the gateway's ConfigMap and creates the namespace Services. The [API configuration step](../../scripts/test_shared_egress.py#L168-L177) selects the shared gateway address. This is a bootstrap configuration snapshot; its certificate references are file paths, with no private key material embedded.

The native gateway identity, destination routing and policy checks remain in place. Unknown projects, unavailable authorizers and project identity mismatches fail closed. Shared Envoy uses a separate project gateway client TLS identity for each authorizer cluster and validates that project's server certificate. These TLS keys are distinct from application credentials and token-storage keys.

Each project owns Agent definitions, a WorkerPool and a logical authorizer with multiple interchangeable replicas. Session creation still goes through that project's Session bridge to kagent; `auth_bridge.launches` makes creation retries idempotent. The diagrams focus on egress and storage dependencies.

The shared gateway, each project authorizer and each WorkerPool have independent proposed HPA targets. WorkerPool scaling uses the CR's `/scale` interface. Gateway scaling changes the whole native gateway Pod. Increasing Envoy CPU requires aligning `--concurrency`; more CPU alone does not change a fixed worker count. Authorizer capacity also depends on Go parallelism, SQL pool size and provider rate limits. Existing tunnels remain on their selected gateway; additional replicas serve new connections. HPA bounds must fit node, database and provider capacity.

PostgreSQL is shared infrastructure, with a separate database and LOGIN per project. All replicas of one project use the same role and encryption key; they share encrypted token state and SQL refresh leases. The [reference DDL](table-schemas.sql) matches the tables displayed in the credential view. Other project roles have no access to these auth schemas. The one-second local freshness rule applies only when a request needs a token; it is not background polling. Session binding checks separately read SQL twice per successful credentialed request. Redis is an unimplemented alternative, rather than an additional cache in this flow.

The [live routing report](../../artifacts/shared-egress-mvp.json) records nine passing checks and 23 requests using the stock API and untouched native egress source. That experiment used `shared-egress-mvp` and reused authorizers from the existing project bundles; their Envoys did not carry the application requests. It restored the previous deployment afterwards. Separate authorizer Deployments, independent gateway/authorizer HPAs, production load and scale-down/draining remain proposed. Vault/GitLab delivery is the intended input; the local experiment used mounted test Secrets.

Generate these diagrams with [render_no_patch_diagrams.py](../../scripts/render_no_patch_diagrams.py):

```sh
python3 scripts/render_no_patch_diagrams.py
python3 scripts/render_no_patch_diagrams.py --exports  # requires CairoSVG
```

SVGs contain only the architecture, without visible page titles or footers. The generator reuses the existing diagram primitives without changing the earlier diagrams.
