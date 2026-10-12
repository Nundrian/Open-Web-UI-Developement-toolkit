# Open WebUI Development Toolkit

**File editing, code search, shell commands and process control for LLMs using Open WebUI and Open Terminal.**

This is a Python **Workspace Tool** for [Open WebUI](https://github.com/open-webui/open-webui). It sends authenticated requests to a separate [Open Terminal](https://github.com/open-webui/open-terminal) server so a tool-enabled language model can work with files and execute development commands in a configured workspace.

**Status:** v1.1.2 public-source edition, adapted from the original v1.1.1 script. The bundled example API key and its fingerprint diagnostic have been removed; configuration now requires an explicit secret. This is **not** an official Open WebUI or OpenAI product.

## Functions

| Area | Functions | Purpose |
| --- | --- | --- |
| Diagnostics | `diagnose_runtime_config`, `test_open_terminal_auth`, `test_open_terminal_execute` | Inspect non-secret settings, check authentication and exercise a fixed harmless command |
| Commands | `dev_run_command` | Execute shell commands in the Open Terminal environment |
| Files | `dev_list_files`, `dev_read_file`, `dev_write_file`, `dev_replace_file_content` | List, read, create, overwrite and precisely edit files |
| Search | `dev_grep_search`, `dev_glob_search` | Find content and file paths |
| Processes | `dev_get_process_status`, `dev_kill_process` | Check or stop long-running commands |

All functions use the Open Terminal HTTP API. The toolkit has no separate database and does not run Open Terminal itself.

## Requirements

- A running **Open WebUI** instance with permission to manage Workspace Tools.
- An accessible **Open Terminal** server with an API key.
- Both containers on a shared Docker network (recommended) or another private network route.
- The file `development_toolkit.py`, which uses the Python standard library and `pydantic` (already part of Open WebUI).
- A trusted user and model: this tool deliberately allows powerful file and command operations.

## Setup: Docker / Open WebUI

### 1. Provide a restricted Open Terminal workspace

If Open Terminal is already running, **keep that instance** and skip to step 2. Verify that its container can access the directory you want to work in and that Open WebUI can reach its HTTP endpoint.

For a new installation, add the following **service** to the same `docker-compose.yml` that already runs Open WebUI (do not replace your existing service). Create an empty `workspace` directory next to the compose file first.

```yaml
services:
  open-terminal:
    image: ghcr.io/open-webui/open-terminal:latest
    restart: unless-stopped
    working_dir: /workspace
    environment:
      OPEN_TERMINAL_API_KEY: ${OPEN_TERMINAL_API_KEY}
      OPEN_TERMINAL_FILE_BROWSER_ROOT: /workspace
    volumes:
      - ./workspace:/workspace
    mem_limit: 2g
    cpus: 2.0
    # No 'ports' entry: reachable only by other services on the Compose network.
```

Set a **unique, randomly generated secret** in a local `.env` file next to the compose file:

```dotenv
OPEN_TERMINAL_API_KEY=REPLACE_WITH_A_LONG_RANDOM_SECRET
```

Do not commit that `.env` file. If your existing compose file uses explicit networks, put both Open WebUI and Open Terminal on the **same private network**. Start the service:

```bash
docker compose up -d open-terminal
```

For other deployment options, see the [official Open Terminal installation guide](https://docs.openwebui.com/features/open-terminal/setup/installation/).

**Note:** The file browser root setting is for the interface, **not** a security boundary. Limit host mounts and Docker permissions to protect the host.

### 2. Add this tool in Open WebUI

1. Open **Workspace → Tools** as a trusted administrator or authorised editor.
2. Create a new tool named **Development Toolkit** and paste the entire contents of [`development_toolkit.py`](./development_toolkit.py) into its Python editor; save it.
3. Open the tool's **Valves** settings and set the values below. Depending on your version, the Valves control may appear in the editor or the tool's settings panel.

| Valve | Value |
| --- | --- |
| `BASE_URL` | `http://open-terminal:8000` (if services share a Compose network) |
| `API_KEY` | The exact secret configured on the Open Terminal server; **no default is provided** |
| `WORKSPACE_ROOT` | `/workspace` (or the actual workspace path inside Open Terminal) |
| `REQUEST_TIMEOUT_SECONDS` | `60` to start |

If Open WebUI and Open Terminal run as **separate Docker containers on different networks**, a container-local URL such as `http://open-terminal:8000` may not resolve. Use a verified private network address instead; do **not** use `localhost` in Open WebUI to refer to a different container.

### 3. Test the connection in order

Enable the toolkit for a model with dependable function calling, either from the chat's **Tools** picker or **Workspace → Models** settings. Then ask the model to call:

1. `diagnose_runtime_config` — confirm `api_key_configured` is `true` and the URL/workspace are correct. This does not contact Open Terminal.
2. `test_open_terminal_auth` — check the configured API key against `GET /api/config`.
3. `test_open_terminal_execute` — execute the fixed `echo TOOLKIT_EXECUTE_TEST` command remotely.
4. `dev_list_files` with `directory="."` — verify the intended workspace is reachable. Try `dev_run_command` with `command="pwd"` before permitting broader commands.

The exact response shape for command execution depends on the Open Terminal server version. A successful response is **not** proof that every subsequent command is safe.

## Security and scope

**This toolkit exposes real shell execution, writing and deletion-capable workflows.** The `_safe_path()` method lexically validates requested file paths and command starting directories against `WORKSPACE_ROOT`, but it does **not** constrain what a shell command itself can access. It also does not prevent symlink traversal or implement a secure server-side file sandbox.

- Run Open Terminal in an isolated Docker container with only the workspace files it needs. Never mount your entire home directory or sensitive data just for convenience.
- **Do not mount `/var/run/docker.sock`**, give the container privileged mode or expose the terminal API publicly.
- Keep the Open Terminal API key out of source code, logs, prompts and Git; configure it through the Open WebUI Tool Valves. Rotate any key previously reused as a hardcoded example or shared in project source.
- Give access only to trusted users and models. Files or web pages read by an LLM can contain malicious instructions that try to induce tool use (prompt injection).
- Back up important work. `dev_write_file` can overwrite files, and `dev_run_command` can execute destructive commands.
- Make sure the server/container actually enforces the resource and filesystem restrictions you require; the client-side wrapper alone cannot.

Open WebUI explicitly warns that Workspace Tools run Python code inside its server process; inspect source before installing and limit who can edit tools. Read the [Open WebUI Tools documentation](https://docs.openwebui.com/features/extensibility/plugin/tools/) and [Open Terminal security guidance](https://docs.openwebui.com/features/open-terminal/advanced/security/).

## Testing and limitations

- The original v1.1.1 tool was used in development-environment checks; a separate Magazine audit recorded tool selection and calls, but **not** complete proof of all intended on-disk side effects.
- This repository includes an **offline unit smoke test** (`toolkit_tests/test_development_toolkit.py`) for API-key defaults, path checks and representative request formation. It does not replace testing against a running Open Terminal server.
- The public v1.1.2 adaptation must be configured with an API key; it does not silently reuse a development credential.

Run the included local tests with Python and Pydantic available:

```bash
python -m unittest discover -s toolkit_tests -v
```

## Project files

- [`development_toolkit.py`](./development_toolkit.py) — Open WebUI tool source.
- [`toolkit_tests/test_development_toolkit.py`](./toolkit_tests/test_development_toolkit.py) — offline unit smoke tests.

The Open Terminal server is a **separate dependency** and is not included in this repository.

## Licence

MIT. See [LICENSE](LICENSE). Personal and commercial use, modification and redistribution are permitted subject to the MIT licence. Third-party dependencies retain their own licences.