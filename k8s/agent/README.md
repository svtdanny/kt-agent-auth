# Agent bundle

Owner: agent team. Apply once per Agent after the cluster and project namespace bundles are ready.

| File | Purpose |
| --- | --- |
| `agent.yaml` | Agent definition referencing a Harness and the shared `transport` EgressPolicy |
| `harness.yaml` | Agent image/entrypoint, non-secret environment, shared WorkerPool and Agent-specific snapshot prefix |

Set `NAMESPACE` to the project's namespace, choose `AGENT` and an immutable `AGENT_IMAGE` ending in `@sha256:<64 hex digits>`, and keep `WORKER_POOL` aligned with the namespace bundle. The example image contract is the Mastra BYO image with `/app/adapter`; update workload settings for another supported Harness. No project application credentials enter this image.

```bash
python3 k8s/render.py --config .cache/agent-a.yaml --bundle agent --output .cache/agent-a
kubectl --context CONTEXT apply -f .cache/agent-a/agent.yaml
```

For a second Agent, change the name and optionally the image, render/apply only this bundle. Both Agents use the same WorkerPool and gateway; no additional proxy, database or project Secret is created. Extra Agent-specific configuration belongs alongside these files, while common transport and permissions remain in the namespace bundle.

Create Sessions through the project's authorizer Service:

```json
{"agent":{"namespace":"project-a","name":"mastra-http"},"serviceAccount":"agent-caller","requestId":"a-unique-launch-id"}
```

The caller must be permitted to use the selected ServiceAccount. The resulting immutable binding keeps the actual Agent identity, even though authorization infrastructure is shared within the project. Request IDs are unique per authenticated caller within the project.
