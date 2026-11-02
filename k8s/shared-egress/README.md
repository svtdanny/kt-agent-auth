# Generic namespace authorizer routing

These are additions to the pinned Substrate gateway configuration. They contain no project list:

```text
verified Actor namespace N → authorizer-extproc.N.svc:50052
```

[namespace-router.yaml](namespace-router.yaml) adds a custom `ext_proc` filter, an internal HTTP/2 listener and two clusters. [namespace-router.lua](namespace-router.lua) validates the certificate-derived Actor identity and constructs the Service DNS name. The processor sends that identity as its own gRPC metadata, independently of application headers. The internal listener has no socket and cannot be called through the Pod IP. Only the processor RPC takes this internal routing step; application HTTP continues through the native upstream route.

[namespace_egress_overlay.py](../../scripts/namespace_egress_overlay.py) parses upstream's Envoy ConfigMap and inserts the additions after native policy processing, before dynamic forwarding. Native listeners, route guards, upstream clusters, certificate settings and TCP passthrough are retained. It rejects conflicting resource names, repeated application and unexpected filter-chain changes. It does not read the copied dedicated-gateway YAML or change Substrate/kagent source.

Apply it with Substrate's `additionalExtprocService` unset. This overlay supplies that processor wiring itself and rejects an already enabled additional processor, avoiding duplicate authorization calls.

To apply it to a manifest produced by the upstream installer:

```sh
python3 scripts/namespace_egress_overlay.py \
  --input /path/to/upstream-rendered.yaml \
  --output /path/to/with-namespace-routing.yaml
```

Without `--input`, it reads the original `atenet-egress.yaml` from the pinned `v0.6.0-alpha3` Go module. That output is a reviewable upstream template: the installer must still resolve its image placeholders. Use the upstream Envoy dataplane image, including its native dynamic modules. This overlay changes only the Envoy ConfigMap; deployment and certificate provisioning remain explicit.

The gateway needs one shared mTLS client identity mounted read-only at `/auth-router`:

| File | Requirement |
| --- | --- |
| `tls.crt`, `tls.key` | Gateway certificate with CN `auth-egress`, matching the existing authorizer peer check |
| `ca.crt` | Common CA bundle for all namespace authorizer server certificates |

Each authorizer must trust that gateway client identity, and its server certificate must include `authorizer-extproc.<namespace>.svc` as a DNS SAN. Issuance of gateway client certificates must remain controlled by the platform. The overlay adds filesystem SDS resources for certificate/trust reloads; it does not create Secrets or mounts. Application source credentials and auth-state encryption keys remain namespace-local and are never mounted into shared Envoy. The current dedicated project CAs and `authorizer.<namespace>.svc` certificates do not satisfy these prerequisites unchanged.

After this one-time gateway configuration and PKI setup, a new namespace needs its authorizer Service and correctly issued certificates. Its first request triggers DNS resolution; there is no per-project gateway rule, cluster, mount or rollout. Namespace selection does not grant application access: the namespace authorizer still checks the Actor, Session binding and project permissions. Missing services and TLS errors fail closed. This configuration does not define production per-project connection/rate budgets.

Validation:

```sh
python3 scripts/test_namespace_egress_overlay.py
AUTH_DEMO_ENVOY_OVERLAY_TEST=1 go test -v ./scripts/envoytest
```

The first command verifies compatibility with the pinned native manifest, including that removing only our additions restores the exact original bootstrap. The second runs Envoy `v1.38.6` and the existing Go `extproc.Server` against synthetic project policies and a common test CA. It checks certificate-based routing, spoofed headers, arbitrary paths, adding a third backend after startup while a response stream stays active, and rejection of malformed identities, unavailable backends and wrong DNS SANs. Local host records emulate Kubernetes Service DNS. It isolates the added filter/routing path; it does not exercise native CONNECT, kagent Sessions, PostgreSQL or a production load profile. The running cluster is unchanged.
