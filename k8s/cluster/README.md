# Cluster bundle

Owner: platform team. Install the platform once; update project gateway assignments when onboarding a namespace.

| File | Purpose |
| --- | --- |
| `install.yaml` | kagent CLI configuration; installs the five separate kagent/Substrate Helm releases |
| `kagent-values.yaml`, `substrate-values.yaml` | Pinned platform baseline values |
| `reviewer-rbac.yaml` | Shared TokenReview/SubjectAccessReview ClusterRole |
| `gateway-assignments.yaml` | Protected cumulative namespace → dedicated gateway ConfigMap |
| `substrate-api-patch.yaml` | Strategic merge patch selecting the patched API image and mounting the assignments |
| `local/minikube-values.yaml` | Local registry override used only by `scripts/up.py` |

From repository root, the pinned installer entrypoint is:

```bash
kagent --config k8s/cluster/install.yaml install --profile minimal
```

It orchestrates `kagent-crds`, `substrate-crds`, `substrate-podcert`, `substrate`, and `kagent`, including its database/certificate prerequisites. Configure existing production database and object storage through the supported platform installation settings; the baseline installer can provision its bundled local dependencies. Registry access, the supported sandbox runtime, PodCertificate/ClusterTrustBundle features and native sidecars must work before Agent rollout.

Build our Substrate API image with [the pinned overlay](../../upstream/substrate.patch). Set its digest in `SUBSTRATE_API_IMAGE` and list all enrolled projects in `GATEWAYS` in the renderer input. For example, `project-a: agent-egress.project-a.svc:443`.

Render with `--bundle cluster`; apply `cluster.yaml`, then use `kubectl patch --type strategic --patch-file` for the separately rendered API patch. Wait for all API replicas to roll before starting project Sessions. The map loads at startup. Removing an assignment selects the platform default gateway, so keep an authorized project route while its Agents run.

Platform shared infrastructure includes PostgreSQL, the snapshot object store, image registry access and signers/trust bundles. It does not include a shared custom application proxy. Other cluster workloads keep their existing behavior.
