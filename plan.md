# MVP: kagent 1.x + agentgateway + Go External Request Processor

Implementation status: the MVP is deployed and tested locally using unchanged upstream binaries. The bridge uses kagent's existing PostgreSQL server with a private schema, event-driven ServiceAccount policy watches, Vault KV v2 credentials with explicit cache invalidation, and asynchronous Session cleanup. The extension adds an enrolled command Pod, its dedicated gateway, cross-namespace HTTP mock, and additive permission sets on one ServiceAccount. See [README.md](README.md) for the current architecture, configuration, test evidence, and benchmark. The design discussion below predates source validation; the possible upstream patches mentioned there proved unnecessary for this demo.

The goal is to keep Substrate's existing transparent HTTPS interception and add your own Go service as a programmable request-processing layer.

You won't modify agent code, install a sidecar into each sandbox, or implement an HTTPS proxy in Go.

Your Go service will eventually be responsible for:

- Identifying the agent using its verified session identity.
- Deciding whether the request is allowed.
- Adding `Authorization`, `Cookie`, or other headers.
- Optionally inspecting or modifying request bodies.
- Returning an explicit rejection when a request is forbidden.

## 1. Architecture diagram

Logical MVP architecture. The Go service is an external processor, not a second outbound proxy. Trusted Actor identity forwarding is the integration point that still needs validation.

Two existing components do the hard networking work:

`atunnel` intercepts outbound TCP and transports it via mTLS CONNECT with an Actor-specific certificate. `agentgateway` terminates supported HTTPS, evaluates Substrate policy and forwards permitted HTTP requests. This behavior is documented in the Substrate egress contract and appears in the current `atenet-egress.yaml` Helm template.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub

+1



Your Go service connects through the standard Envoy-compatible `ext_proc` gRPC interface supported by agentgateway. It doesn't need to forward the actual external HTTP connection.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://agentgateway.dev\&sz=32)

Agent Connectivity Solved



## 2. Which components need to be changed?

For the MVP, the smallest possible change set is:

| Component                         | Action                                                  | Rebuild image?          |
| --------------------------------- | ------------------------------------------------------- | ----------------------- |
| kagent Agent / Sandbox            | Nothing                                                 | No                      |
| Substrate `ateworker` / `atunnel` | Nothing                                                 | No                      |
| Substrate API server              | Nothing                                                 | No                      |
| `atenet-egress` agentgateway      | Extend configuration                                    | Initially no            |
| Go external processor             | New Deployment + Service                                | Build new image         |
| Session → Agent registry          | Implement in Go                                         | Part of Go service      |
| Trusted Actor identity export     | Verify; potentially patch agentgateway                  | Maybe                   |
| kagent egress policy compiler     | Support unrestricted HTTPS attempts for selected Agents | Likely yes for full MVP |

The existing Substrate Helm chart generates agentgateway's configuration inside `atenet-egress.yaml`. Its values currently expose resource configuration but not an arbitrary `extProc` policy override. Therefore the practical deployment approach is to customize the Substrate chart or use a Helm post-renderer.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub

+1



## 3. The configuration change

Edit:

[charts/substrate/templates/atenet-egress.yaml](https://github.com/kagent-dev/substrate/blob/main/charts/substrate/templates/atenet-egress.yaml?utm_source=chatgpt.com)

In the existing ConfigMap's `config.yaml`, find the HTTPS listener's route policy containing:

```
policies:
  substrateEgress:
    # Existing Substrate authorization configuration
```

Add the following `extProc` block as a sibling of `substrateEgress` in the same `policies` map:

```

extProc:
  host: go-extproc.security.svc.cluster.local:50051
  failureMode: failClosed
  processingOptions:
    requestHeaderMode: send
    requestBodyMode: none
    responseHeaderMode: skip
    responseBodyMode: none
    requestTrailerMode: skip
    responseTrailerMode: skip
  requestAttributes:
    debugSpiffeId: source.spiffeId
    requestMethod: request.method
    requestUri: request.uri

```

This uses agentgateway's documented standalone ExtProc configuration and CEL request attributes. The `debugSpiffeId` attribute is a diagnostic probe, not an identity mechanism that you should trust yet. It may be missing or may not represent the outer CONNECT identity. Validate the snippet against the exact agentgateway image pinned by your Substrate release.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://agentgateway.dev\&sz=32)

Agent Connectivity Solved

+2



For this first stage, configure header-only processing. Later, changing `requestBodyMode` to `buffered` enables full-body inspection within configured size limits.

For plain HTTP, add the same policy to the existing HTTP listener's route. Do not put `extProc` on the TCP/TLS passthrough route: that route doesn't carry decoded HTTP requests.

### First configuration test

Before editing, inspect the running config:

```
kubectl -n ate-system get configmap \
  atenet-egress-agentgateway-config \
  -o jsonpath='{.data.config\.yaml}'
```

The actual ConfigMap name depends on the chart's release naming configuration.

For a disposable test cluster, modifying that ConfigMap and restarting `atenet-egress` is enough to experiment. For repeatable deployment, commit the change to a customized chart and deploy with Helm.

No custom agentgateway image is needed provided the pinned binary already supports the ExtProc fields.

## 4. The critical integration: trusted Session ID

This is the biggest technical uncertainty, and it deserves its own MVP milestone.

I looked at agentgateway's actual Rust implementation.

At CONNECT acceptance, agentgateway resolves the authenticated Actor and preserves the resulting `ActorIdentity` object across the tunnel:

[proxy/gateway.rs](https://github.com/agentgateway/agentgateway/blob/main/crates/agentgateway/src/proxy/gateway.rs?utm_source=chatgpt.com)

The `SubstrateEgress::apply` function reads that `ActorIdentity` from request extensions. It contains the Actor name, atespace and Actor UID, which are already used for logging and policy enforcement.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub

+1



So the identity definitely exists in agentgateway. The missing piece is exporting it to your gRPC service.

### What Go should receive

I would use these three fields:

```
{
  "atespace": "production",
  "actor_name": "session-01a0d409-d249-728e-865c-ee58b55dff04",
  "actor_uid": "verified-substrate-uid"
}
```

This is a proposed payload shape in the Envoy `ProcessingRequest.attributes` map, not an existing guaranteed agentgateway payload.

From the Actor name, Go obtains the session ID and then performs your lookup:

```
(atespace, session_id)
      ↓
Session registry
      ↓
Agent name: "research-agent"
      ↓
Your authorization logic
```

kagent documents that an Actor's name is `session-` followed by its Session ID, and that its atespace corresponds to the owning Agent's namespace.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://kagent.dev\&sz=32)

kagent docs



### What if the trusted identity is not exposed?

Then I'd make a small agentgateway modification, rather than changing kagent or Substrate networking.

In the agentgateway HTTP processing integration:

1. Read the existing trusted `ActorIdentity` extension.
2. Copy the Actor name, atespace and UID into trusted `ext_proc` request attributes or metadata.
3. Ensure only the CONNECT-authenticated identity can populate them.
4. Fail closed if the identity is missing.

The relevant agentgateway source is [http/ext_proc.rs](https://github.com/agentgateway/agentgateway/blob/main/crates/agentgateway/src/http/ext_proc.rs?utm_source=chatgpt.com).

This is where I would expect the smallest code change if the configuration-only approach fails. Importantly, this would require building a compatible customized agentgateway image, not rebuilding the kagent controller.

## 5. MVP implementation plan

1

Build minimal Go external processor

Create a gRPC server implementing `envoy.service.ext_proc.v3.ExternalProcessor`.

Initially it should just log the incoming attributes, HTTP method, hostname and path; return a valid processing response; and optionally add a harmless test header.

Do not integrate real secrets yet.

Deliverable: Go image and a ClusterIP Service on port `50051`.

2

Connect agentgateway to Go

Apply the chart customization shown above to the HTTPS route. Preserve the existing `substrateEgress`, TLS, dynamic backend and credential-provider configuration.

Use an HTTPS destination already allowed by the test Agent's kagent `EgressPolicy`.

Deliverable: A request made by an ordinary sandbox `curl` reaches your Go gRPC processor, without any proxy settings in the sandbox.

3

Verify trusted identity forwarding

Run two different kagent Sessions against the same destination and inspect the attributes delivered to Go.

Confirm that the identities differ, match their real Actor names, and cannot be changed by injecting an `X-Agent-ID` header from the sandbox.

If the verified Actor identity is missing, implement the small agentgateway extension described above.

Deliverable: Go reliably receives `(atespace, Actor name, Actor UID)` for every intercepted HTTP request.

4

Implement authorization and credential injection

Add a session-to-Agent-name lookup, with a bounded cache, then evaluate your own rules.

Support three outcomes: allow unchanged, allow with header mutations, or reject. Use the external processing protocol's header mutation and `ImmediateResponse` messages.

Fetch real credentials from your chosen secret store only after the policy approves an exact upstream destination.

Deliverable: Agent A and Agent B can make the same HTTPS call but receive different policy decisions or upstream credentials.

5

Enable arbitrary HTTPS destinations

This is a separate change required for your original requirement.

kagent's normal `EgressPolicy` rejects an unrestricted origin, while native Substrate can represent an all-host HTTPS grant. The DNS relay also checks the native Actor policy before resolving unknown hosts.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://www.kagent.dev\&sz=32)

kagent docs

+1



For a disposable proof of concept, test a broad native grant on one Actor. For a durable MVP, introduce a privileged opt-in configuration in kagent's policy-generation path, or a supported equivalent integration, so selected Agents can reach arbitrary HTTPS destinations only through your mandatory Go authorization processor.

Deliverable: A previously undeclared HTTPS hostname can reach Go, where your code decides whether to allow it.

6

Hardening and integration tests

Verify that an unavailable Go processor causes denial, untrusted identity is rejected, redirects don't leak credentials, and agents cannot directly reach the secret store or bypass the egress gateway.

Protect the gRPC connection with mTLS or equivalent trusted service authentication and Kubernetes network restrictions.

Deliverable: A repeatable Helm deployment and a small security regression suite.

## 6. What is the expected integration effort?

| Workstream                      | Complexity                 | Main uncertainty                                         |
| ------------------------------- | -------------------------- | -------------------------------------------------------- |
| Go gRPC processor               | Low–medium                 | Correct streaming protocol implementation                |
| Add `extProc` to chart          | Low                        | Compatibility with pinned agentgateway image             |
| Session ID / Actor identity     | Medium, potentially higher | Whether the existing processor can read trusted identity |
| Dynamic policy + secrets        | Medium                     | Your business rules and secret management                |
| Arbitrary-host transport grants | Medium–high                | kagent controller's policy ownership                     |
| Body processing later           | Medium                     | Buffering, streaming and mutation semantics              |

The biggest risk isn't the Go processor. It's the intersection of trusted Actor identity forwarding and kagent's existing egress policy compilation.

## 7. MVP acceptance criteria

I'd consider the MVP complete only when these checks pass:

MVP validation checklist

0 / 8

An HTTPS request from the sandbox reaches Go automatically

Go receives a verified Actor/session identity

Two sessions are distinguishable even on the same destination

Go can deny an HTTP request before it reaches upstream

Go can inject a test Authorization or Cookie header

Real credentials are never placed in the sandbox

An unknown HTTPS hostname can reach Go for a policy decision

If Go is unavailable, protected requests fail closed

For the initial implementation, I would deliberately stop after milestone 3 and confirm the identity path before investing in the rest. That will determine whether this is primarily a Go service + Helm customization, or also requires a small agentgateway fork. The unrestricted-host feature is a separate, explicit kagent integration rather than something the `extProc` configuration alone can provide.

Recommended source files for the implementation:

- [Substrate's atenet-egress Helm template](https://github.com/kagent-dev/substrate/blob/main/charts/substrate/templates/atenet-egress.yaml?utm_source=chatgpt.com)
- [agentgateway's authenticated Actor policy implementation](https://github.com/agentgateway/agentgateway/blob/main/crates/agentgateway/src/http/substrate/egress.rs?utm_source=chatgpt.com)
- [agentgateway's external processing implementation](https://github.com/agentgateway/agentgateway/blob/main/crates/agentgateway/src/http/ext_proc.rs?utm_source=chatgpt.com)
- [agentgateway ExtProc configuration documentation](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/traffic-management/extproc/?utm_source=chatgpt.com)
