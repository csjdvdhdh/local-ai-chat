# Railway API references

This implementation uses the Railway GraphQL Public API. Verify current API behavior and workspace permissions in Railway before production use.

- [Public API overview](https://docs.railway.com/integrations/api): GraphQL endpoint, token types and scopes. The control plane expects a Workspace token and keeps it only in its own environment.
- [Manage projects](https://docs.railway.com/integrations/api/manage-projects): `projectCreate` supports `workspaceId`, `defaultEnvironmentName`, and `isPublic`; `projectDelete` removes a project.
- [Manage environments](https://docs.railway.com/integrations/api/manage-environments): query `environments(projectId)` to find a project's default environment.
- [Manage services](https://docs.railway.com/integrations/api/manage-services): `serviceCreate` supports Docker image sources; `serviceInstanceUpdate` configures `startCommand` and restart behavior; `serviceInstanceDeployV2` triggers deployment.
- [Manage variables](https://docs.railway.com/integrations/api/manage-variables): `variableCollectionUpsert` sets service-level variables. `skipDeploys` can defer the deployment until service settings are ready.
- [Manage deployments](https://docs.railway.com/integrations/api/manage-deployments): query runtime logs, inspect latest deployment status, and stop a deployment.
- [API cookbook](https://docs.railway.com/integrations/api/api-cookbook): end-to-end GraphQL examples and the endpoint `https://backboard.railway.com/graphql/v2`.

The code creates a separate project for each bot, then a service from the official `python:3.12-slim` image. It updates the service command and variables and explicitly triggers a deployment. These operations have not been exercised against the user's Railway workspace because no Railway token, workspace ID, database connection or public service URL was provided in this task.
