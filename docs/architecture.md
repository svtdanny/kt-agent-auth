# Architecture

The current dedicated bundle and communications are described in the [README](../README.md). The [Mermaid source](architecture-current.mmd) is editable.

Each authorizer serves one project namespace containing multiple Agents. The project has one reusable WorkerPool and one dedicated proxy bundle. Native Actor transport and HTTP policy checks both apply. `ext_proc` receives request metadata over gRPC; response decisions and header mutations return to Envoy, which forwards the application request.

Credentials are delivered through mounted Kubernetes Secrets populated by GitLab/Vault. The authorizer reads the single projected JSON file locally and refreshes its own OAuth cache when that document changes.
