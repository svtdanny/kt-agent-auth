# Deployment bundles

One GitLab repository is one project namespace. Each project has one dedicated proxy/authorizer and a WorkerPool reused by its Agents. Agent identity and ServiceAccount permissions remain attached to each Session.

| Bundle | Owner / frequency | Contents |
| --- | --- | --- |
| [cluster](cluster/README.md) | Platform team, once; update assignments on project onboarding | Upstream Helm configuration, reviewer ClusterRole, namespace-to-gateway map and Substrate API overlay |
| [namespace](namespace/README.md) | DevOps/project automation, once per GitLab project | Namespace, shared WorkerPool, dedicated proxy, Services, RBAC, EgressPolicy, ConfigMaps and Secret references |
| [agent](agent/README.md) | Agent team, per Agent | Agent and Harness referencing the shared project resources |

All runtime resources run in Kubernetes. Vault and GitLab may run outside the cluster. The namespace and Agent bundles are real YAML templates used by the local demo installer too; each folder documents its inputs and ownership. Docker build recipes remain in `deploy/`.

## Render and deploy

Run from the repository root. Copy [deployment.example.yaml](deployment.example.yaml) to an environment file outside Git, replace image references and origins, and provide a PEM trust bundle containing the upstream CAs required by that environment. The file contains non-secret renderer inputs, not Helm values. Keep `GATEWAYS` cumulative across all enrolled project namespaces.

```bash
cp k8s/deployment.example.yaml .cache/project-a.yaml
# Edit .cache/project-a.yaml for the environment.
python3 k8s/render.py --config .cache/project-a.yaml --output .cache/k8s
```

The renderer creates `cluster.yaml`, `namespace.yaml`, `agent.yaml` and `substrate-api-patch.yaml`. It does not connect to Kubernetes or provision credentials. Apply rendered files explicitly; source YAML contains `${...}` variables and also includes Helm/CI/configuration files that are not Kubernetes objects.

1. Install the platform using [cluster instructions](cluster/README.md).
2. Render/apply the cluster resources and API patch; wait for every API replica to load the gateway assignments.
3. Provision project database/certificates, apply the namespace bundle, and deliver its application Secret through GitLab/Vault. Wait for the gateway and WorkerPool to become ready.
4. Apply the Agent bundle. For another Agent, change only `AGENT` and `AGENT_IMAGE`, render `--bundle agent`, then apply that file. Do not reapply shared namespace resources from an Agent-only pipeline.

```bash
kubectl --context CONTEXT apply -f .cache/k8s/cluster.yaml
kubectl --context CONTEXT -n ate-system patch deployment ate-api-server \
  --type strategic --patch-file .cache/k8s/substrate-api-patch.yaml
kubectl --context CONTEXT -n ate-system rollout status deployment/ate-api-server
kubectl --context CONTEXT apply -f .cache/k8s/namespace.yaml
# Deliver the Secrets described in namespace/README.md before waiting for readiness.
kubectl --context CONTEXT -n project-a rollout status deployment/agent-egress
kubectl --context CONTEXT apply -f .cache/k8s/agent.yaml
```

Shared workers are currently shared within a project namespace. The pinned kagent WorkerPool reference is namespace-local. Gateway routing remains the existing Substrate namespace overlay; no new kagent or worker patch is introduced.

Use [the production task](../docs/devops-production-task.md) for readiness, availability and rollout requirements. These bundles preserve the tested single-replica/Recreate baseline; organizing YAML does not establish production HA.
