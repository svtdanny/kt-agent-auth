# Shared Envoy egress architecture

Native kagent sandboxes and enrolled custom Pods use one `atenet-egress` Deployment and Service. The native Actor listener is on Service port 443; the managed Pod listener is on 8443. Both call our external processor. Each source has two forwarding stages: its tunnel client and the shared Envoy.

```mermaid
flowchart TB
    subgraph CLUSTER["One Kubernetes cluster"]
        subgraph WORKER["kagent worker Pod"]
            A["Agent sandbox / gVisor or Kata"] -->|"Captured TCP"| T["atunnel"]
        end
        subgraph POD["Enrolled custom Pod"]
            B["Application / commands"] -->|"Captured TCP"| R["Envoy TCP relay"]
        end
        T -->|"mTLS CONNECT / Actor identity"| G
        R -->|"mTLS CONNECT / Pod identity"| G
        subgraph GATEWAY["SHARED atenet-egress Deployment / Service"]
            G["Inspecting Envoy / native and Pod listeners"]
            SDS["sdsmint / automatic destination certificates"]
            NATIVE["Substrate native Actor authorization"]
            G <-.->|"SDS / Unix socket"| SDS
            G <-.->|"Native listener only"| NATIVE
        end
        G -->|"Allowed request + injected key"| MCP["Internal MCP Service / other namespace"]
        G <-.->|"gRPC mTLS / headers, decision, mutations"| EP["OUR Go ext_proc"]
        EP <-.->|"HTTPS mTLS / context + exact request grant"| CP["OUR control plane"]
        CP <-->|"Immutable Session or Pod binding"| DB[("Existing PostgreSQL")]
        CP <-->|"Credential cache miss"| VAULT["Vault"]
        KUBE["Kubernetes API"] -->|"ServiceAccount / enrolled Pod watches"| CP
    end
    G -->|"Allowed HTTPS + injected key"| EXTERNAL["External API / outside Kubernetes"]

    USER["Authenticated user"] -->|"Create / invoke / delete Session"| CP
    CP -->|"TokenReview + SAR / account selection"| KUBE
    CP -->|"Native Session API / A2A"| KAGENT["kagent controller"]
    KAGENT -->|"Actor lifecycle"| SUB["Substrate API"]
    SUB -->|"Start / resume sandbox"| A
    NATIVE <-->|"Actor UID / state / native egress policy"| SUB
    LAUNCH["Trusted Pod launcher"] -->|"Create enrolled Pod / issue identity cert"| KUBE
    LAUNCH -->|"Internal mTLS / immutable Pod UID binding"| CP
    OTHER["Unrelated Pod"] -->|"Existing direct access preserved"| MCP
```

The native listener uses the existing Substrate Actor certificate checks and per-request egress policy helper. The Pod listener validates our Pod UID certificates through the durable ServiceAccount binding. The two certificate trust domains are separate; HTTP headers cannot choose a caller class or ServiceAccount.

`sdsmint` serves destination certificates through a local Unix socket. Our processor and the certificate issuer receive RPCs; application traffic is forwarded by Envoy. Hashable filter state keeps each identity and original destination isolated across internal connections. The gateway uses Envoy 1.38.6.

Internal services retain their existing ingress configuration. The external authentication fixture is a Docker HTTPS service outside Kubernetes, without a Kubernetes Service or Endpoint. Only enrolled Pod network namespaces receive capture/firewall rules. A narrow gateway ingress allowance admits these Pods to the managed listener where NetworkPolicy is enforced.

## Policy and credential flow

```mermaid
flowchart LR
    SA["Kubernetes ServiceAccounts in demo<br/>auth.demo.dev/managed=true<br/>policy type + configuration + Vault reference"]
    VAULT["Vault KV v2<br/>kv/data/auth-demo/demo/credential-reference"]
    ADMIN["Credential rotation client"]
    PODS["Only enrolled Pods in demo<br/>auth.demo.dev/egress=enforced<br/>actual Pod UID + serviceAccountName"]

    subgraph CONTROL["Deployed control plane / bridge"]
        WATCHER["Account + enrolled Pod informers / resync 0<br/>initial sync + reconnect / recovery relist"]
        POLICY["Policy snapshots<br/>no TTL / usable during reconnect"]
        AUTH["Context + exact request authorization<br/>Session or Pod binding / SA UID / policy version<br/>additive complete grants; conflicts deny"]
        KEYS["Credential provider<br/>Kubernetes auth / verified HTTPS<br/>in-memory cache / no polling"]
        MISS["Coalesced Vault read on miss<br/>scope and field validation<br/>failure denies / no stale fallback after invalidation"]
        WATCHER -->|"add / update / delete / label removal"| POLICY
        AUTH -->|"read"| POLICY
        AUTH -->|"approved credential reference only"| KEYS
        KEYS -->|"cache miss"| MISS
    end

    SA -->|"label-selected initial sync + WATCH"| WATCHER
    PODS -->|"label-selected initial sync + WATCH<br/>deletion / replacement / label removal revokes Pod identity"| WATCHER
    AUTH -->|"uncached requested account only / coalesced GET"| SA
    MISS <-->|"scoped KV-v2 read / short-lived Vault token"| VAULT
    ADMIN -->|"write / rotate / revoke"| VAULT
    ADMIN -->|"internal mTLS invalidation<br/>next approved request refetches"| KEYS
    EP["ext_proc / no Vault or Kubernetes access"] <-->|"HTTPS mTLS context / request grant / header mutations"| AUTH
    AUTH <-->|"binding primary-key read"| DB[("Existing PostgreSQL<br/>private auth_bridge schema")]
    GC["Async cleanup / 5 min<br/>all native Session pages + confirm NotFound<br/>retain suspended Sessions / abort on scan errors"] -->|"revoke / retain mappings 24 h<br/>launch tombstones 7 d"| DB
```

ServiceAccount policy changes arrive through watches. Each complete grant independently matches origin, port, method and path; multiple grants can belong to the same Session or Pod identity. Vault credentials are cached on first read and explicitly invalidated. PostgreSQL persists immutable bindings across control-plane restarts.
