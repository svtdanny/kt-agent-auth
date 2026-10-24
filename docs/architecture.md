# Deployed communication diagrams

The native kagent Session and the specific command Pod use dedicated gateways and the same processor/control plane. PostgreSQL persists both identity mappings. ServiceAccount watches update permission sets; enrolled Pod watches validate Pod identity. Vault supplies rule-selected credentials.

Only the opted-in command Pod receives the capture/firewall template. Other Pods keep their existing routes; the cross-namespace mock service has no new ingress restrictions.

## Complete architecture

```mermaid
flowchart TB
    USER["User / test client"]
    KUBE["Kubernetes API<br/>caller identity, RBAC, ServiceAccount policies"]
    VAULT["Vault KV v2 / HTTPS :8200<br/>application keys / persistent storage<br/>separate read-only bridge and mock roles"]
    OPS["Rotation / maintenance client"]

    subgraph CONTROL["Our control-plane Deployment"]
        CP["Session bridge + internal authorization API<br/>HTTPS :8443"]
        WATCH["Label-selected ServiceAccount + enrolled Pod informers<br/>no TTL / periodic resync"]
        CACHE["In-memory policy snapshots<br/>eventually consistent / retained during disconnect"]
        CRED["Vault credential provider<br/>coalesced cache-on-first-read / max 128<br/>explicit invalidation / no polling"]
        GC["Async Session cleanup every 5 min<br/>complete paginated scan + NotFound confirmation"]
        CP -->|"read snapshot; GET only on miss"| WATCH
        WATCH -->|"add / update / delete events"| CACHE
        CACHE -->|"policy version / UID"| CP
        CP -->|"read approved credential reference"| CRED
        GC -->|"revoke confirmed deletions; purge retained rows"| CP
    end

    subgraph PLATFORM["Unmodified upstream platform"]
        KG["kagent controller<br/>gRPC :8083"]
        subgraph POSTGRES["Existing PostgreSQL server + persistent volume<br/>same kagent database / max_connections 100"]
            PG[("Native kagent / Substrate schemas<br/>Sessions, Tasks, Actors")]
            BIND[("Our private auth_bridge schema<br/>Session bindings + launches + Pod UID bindings<br/>dedicated login / pool max 4")]
        end
        SUB["Substrate API + gVisor worker / atelet"]
        TUNNEL["atunnel on the Substrate worker<br/>transparent interception + Actor identity"]
        SNAP[("S3 snapshots<br/>RustFS")]
        KG -->|"Native SQL / pool max 8"| PG
        SUB -->|"Native SQL / main pool max 8 per API replica"| PG
        KG -->|"Actor lifecycle and A2A routing"| SUB
        SUB -->|"save / restore snapshots"| SNAP
        SUB -->|"Actor networking / certificates"| TUNNEL
    end

    subgraph ACTOR["Native Session Actor / gVisor sandbox"]
        ADAPTER["Go BYO adapter<br/>native A2A + TaskStore SDK"]
        MASTRA["Mastra / Node.js<br/>ordinary HTTP fetch"]
        ADAPTER -->|"loopback HTTP :3000 /invoke"| MASTRA
        MASTRA -->|"HTTPS request with placeholder Authorization"| TUNNEL
        ADAPTER -->|"private TaskStore RPC"| TUNNEL
    end

    GW["Dedicated atenet-egress / agentgateway<br/>TLS interception + native egress authorization"]
    EP["Our Go ext_proc Deployment<br/>gRPC :50051 / policy evaluation"]
    API["HTTPS catalog test API<br/>validates injected reader / writer credential"]
    MCP["mock-mcp.test-services.svc.cluster.local:8080<br/>other namespace / HTTP mock<br/>validates separate MCP credential"]
    LAUNCH["Trusted demo Pod launcher<br/>observe Pod UID / issue relay certificate / enroll"]
    PGW["Dedicated pod-egress / agentgateway<br/>HTTPS interception + fixed HTTP(S) routes"]
    OTHER["Unrelated Pods<br/>existing network access unchanged"]
    DNS["Existing cluster DNS / port 53"]

    subgraph POD["Specific demo/command-runner Pod / multi-reader ServiceAccount"]
        INIT["Capture init / NET_ADMIN<br/>rules in this Pod netns only"]
        COMMAND["Mastra command tool / UID 65532<br/>curl --noproxy '*' / no capabilities"]
        RELAY["Envoy relay / UID 1337<br/>original destination -> CONNECT<br/>Pod UID mTLS certificate"]
        INIT -->|"redirect TCP; restrict egress to gateway + cluster DNS"| COMMAND
        COMMAND -->|"transparent loopback TCP :15001"| RELAY
    end

    USER -->|"HTTPS + bearer token<br/>create / invoke / delete Session"| CP
    CP -->|"HTTPS TokenReview + SubjectAccessReview"| KUBE
    CP <-->|"Verified TLS + SCRAM<br/>reserve / bind / resolve / revoke<br/>no native table access"| BIND
    WATCH <-->|"HTTPS initial LIST or streaming sync + WATCH<br/>automatic recovery / relist<br/>managed=true accounts; egress=enforced Pods"| KUBE
    WATCH -->|"Coalesced GET only for uncached requested objects"| KUBE
    CRED <-->|"HTTPS Kubernetes login + scoped KV-v2 read<br/>verified CA / no root token"| VAULT
    VAULT -->|"TokenReview on Kubernetes auth login"| KUBE
    OPS -->|"write / rotate credential"| VAULT
    OPS -->|"HTTPS internal mTLS<br/>invalidate cached credential reference"| CP
    API -->|"own Vault Kubernetes role<br/>load private expected keys at startup"| VAULT
    CP -->|"gRPC + bridge JWT<br/>Create / Delete Session + SendMessage"| KG
    GC -->|"gRPC + bridge JWT<br/>ListSessions all pages; GetSession only for absent bindings"| KG
    SUB -->|"start / resume Actor and deliver A2A calls"| ADAPTER
    TUNNEL -->|"mTLS CONNECT<br/>Actor-specific certificate"| GW
    GW -->|"HTTPS platform mTLS<br/>Actor resolution + native egress policy lookup"| SUB
    GW -->|"native platform HTTP/gRPC route<br/>TaskStore traffic bypasses ext_proc"| KG
    GW -->|"gRPC mTLS<br/>HTTP headers + verified Actor SPIFFE attribute"| EP
    EP -->|"HTTPS mTLS<br/>/internal/context + /internal/credential<br/>exact request + policy version / one conflict retry"| CP
    EP -->|"allow / deny + header mutations"| GW
    GW -->|"allowed HTTPS request<br/>Authorization replaced with approved credential"| API
    GW -->|"explicit internal HTTP route<br/>native transport policy + ext_proc grant"| MCP
    LAUNCH -->|"create only opted-in Deployment / relay Secret"| KUBE
    LAUNCH -->|"HTTPS internal mTLS /internal/pods<br/>observed Pod UID -> actual ServiceAccount UID"| CP
    RELAY -->|"mTLS CONNECT :8443<br/>spiffe://auth-demo.local/ns/demo/pod/UID"| PGW
    PGW -->|"gRPC mTLS / headers + verified Pod SPIFFE"| EP
    EP -->|"allow / deny + selected credential"| PGW
    PGW -->|"allowed GET /catalog + catalog key"| API
    PGW -->|"allowed POST /mcp + separate MCP key"| MCP
    MCP -->|"own Vault role / private expected keys"| VAULT
    OTHER -->|"direct access preserved / no new target ingress restrictions"| MCP
    COMMAND -->|"DNS exception / UDP or TCP :53 only to resolver"| DNS
    RELAY -->|"resolve dedicated gateway Service"| DNS
```

## Policy and Vault credential flow

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

One Session binds to one ServiceAccount UID. That account can hold multiple complete grants: for example `GET /catalog` on the HTTPS catalog and `POST /mcp` on the HTTP mock in `test-services`. The processor grants a request if a complete permission matches; it never combines destination, method or credentials across separate grants. Removing a grant takes effect after its watch update arrives, without recreating the Session or Pod.

The command Pod redirects its TCP connections to Envoy in its own network namespace. Envoy authenticates to `pod-egress` using a Pod UID certificate; application containers cannot read that key. The Pod firewall allows only this gateway connection and cluster DNS outside loopback. It does not depend on CNI NetworkPolicy support.

The demo launcher handles certificate issuance and Pod enrollment. A replacement Pod requires re-enrollment; the old certificate cannot inherit its identity. The mock's `/mcp` endpoint tests HTTP authorization and key insertion, not MCP JSON-RPC tool-name filtering.
