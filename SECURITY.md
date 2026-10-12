# Security

This project is an Open WebUI Workspace Tool that makes **authenticated Open Terminal requests for real shell commands and file modifications**. Installing or enabling it grants powerful capabilities to tool-enabled models.

## Security boundaries

- **Container isolation is mandatory:** `WORKSPACE_ROOT` is only a lexical path check and protects neither a shell command nor a symlink target. Enforce actual restrictions with the Open Terminal container: unprivileged user, limited mounts, no Docker socket, no privileged mode, dedicated workspace and a private network.
- **Authenticated HTTP requests:** Requests use an API token configured in Open WebUI Tool Valves. The client now **rejects HTTP redirects** so the token cannot be forwarded automatically to another origin via Python's default redirect behaviour. HTTP 3xx responses must be resolved by an administrator configuring the correct base URL, not followed automatically.
- **Sensitive credentials:** There is no bundled API key. Never commit a key, share it in prompts or logs, or expose Open Terminal publicly. Rotate any key known to have been published previously.
- **Tool authority:** `dev_run_command` executes arbitrary commands with the permissions of Open Terminal. A user-supplied or webpage-supplied instruction can cause destructive or unauthorised commands if the model treats it as trusted. Restrict tool access to trusted users, confirm sensitive operations and distrust retrieved content.
- **Remote output:** Command results, file content and HTTP error bodies may be returned to the calling model. Do not include secrets unless you intentionally allow that model/provider to see them.
- **Transport:** The default Docker service URL uses plain HTTP on the private Compose network. If traffic crosses untrusted networks, use a secure and authenticated transport and verify the destination.

## Checks

Run `python -m unittest discover -s toolkit_tests -v` in the repo with Pydantic installed. The repository's GitHub Actions workflow runs syntax and unit tests, including a local cross-origin redirect regression. These tests do **not** audit the Open Terminal server, validate all tool side effects or constitute a malware scan.

## Reporting security issues

Use GitHub issues for non-sensitive reports. Do not publish credentials, private workspace contents or a working exploit against a real system; request a private reporting channel first.

No guarantee of complete security or freedom from vulnerabilities is provided.
