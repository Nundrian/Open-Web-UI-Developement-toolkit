# Open WebUI Development Toolkit

**File editing, code search, shell commands and process control for LLMs using Open WebUI and Open Terminal.**

This is a Python **Workspace Tool** for [Open WebUI](https://github.com/open-webui/open-webui). It sends authenticated requests to a separate [Open Terminal](https://github.com/open-webui/open-terminal) server so a tool-enabled language model can work with files and execute development commands in a configured workspace.

**Status:** **v1.2.3**, independently checked against the submitted audit source and exercised in live Open WebUI/Open Terminal acceptance tests. This remains a community project, **not** an official Open WebUI or OpenAI product. The earlier v1.1.3 implementation is replaced in-place and remains available through Git history.

## Functions

| Area | Functions | Purpose |
| --- | --- | --- |
| Internal diagnostics | `_diagnose_runtime_config`, `_test_open_terminal_auth`, `_test_open_terminal_execute` | Private helpers; intentionally hidden from the model-visible public tool list |
| Commands | `dev_run_command` | Execute shell commands in the Open Terminal environment |
| Files | `dev_list_files`, `dev_read_file`, `dev_write_file`, `dev_replace_file_content` | List, read, create, overwrite and precisely edit files |
| Search | `dev_grep_search`, `dev_glob_search` | Find content and file paths |
| Processes | `dev_list_processes`, `dev_get_process_status`, `dev_kill_process` | Discover, inspect and stop running or completed commands |

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
| `CONFIRM_SHELL_COMMANDS` | `true` (default). Require user confirmation for every shell command. |
| `FILE_WRITE_APPROVAL` | `sensitive` (default), `always`, or `never`. The `always` policy is strongest for interactive file changes. |
| `MAX_APPROVAL_CONTENT_CHARS` | `8000` by default; may be raised to `30000` through Valves for longer source files (maximum `50000`). |
| `CONFIRM_TIMEOUT_SECONDS` | `60` seconds by default; raise it if confirmations need longer, keeping Open WebUI's own WebSocket event timeout in mind. |
| `MAX_OUTPUT_CHARS` | `10000` by default; lengthy tool output is summarised while retaining important process metadata. |

If Open WebUI and Open Terminal run as **separate Docker containers on different networks**, a container-local URL such as `http://open-terminal:8000` may not resolve. Use a verified private network address instead; do **not** use `localhost` in Open WebUI to refer to a different container.

### 3. Test the connection and confirmations

Enable the toolkit for a model with reliable function calling. Run small, **separate** calls and inspect the actual tool responses rather than relying only on the model's summary.

1. Call `dev_list_files(directory=".")` to verify that `/workspace` is reachable.
2. Call `dev_read_file(path="../outside.txt")` and verify that the path is rejected with `INVALID_ARGUMENT`.
3. Call `dev_run_command(command="echo APPROVAL_TEST", cwd="/workspace")`. **Decline** the first confirmation; expect `APPROVAL_REQUIRED` and no process ID.
4. Call the same command again and **Approve**; expect a process ID, `status: done`, exit code `0`, and the echoed text.
5. Try creating a uniquely named `.py` file via `dev_write_file`; verify that rejection leaves it absent and approval permits the write. Use `dev_read_file` to verify the contents.

For approval to work, the chat/browser must deliver an interactive confirmation event. If it is unavailable or times out, the toolkit should refuse the operation; it must not run without approval when confirmation is required.

**Do not embed API keys in the script or GitHub repository.** Set the Open Terminal token privately in the tool Valves.

## Security and scope

See [SECURITY.md](SECURITY.md) for the detailed trust boundaries, token redirect protection and reporting guidance.

**This toolkit exposes real shell execution, writing and deletion-capable workflows.** The `_safe_path()` method lexically validates requested file paths and command starting directories against `WORKSPACE_ROOT`, but it does **not** constrain what a shell command itself can access. It also does not prevent symlink traversal or implement a secure server-side file sandbox.

- Run Open Terminal in an isolated Docker container with only the workspace files it needs. Never mount your entire home directory or sensitive data just for convenience.
- **Do not mount `/var/run/docker.sock`**, give the container privileged mode or expose the terminal API publicly.
- Keep the Open Terminal API key out of source code, logs, prompts and Git; configure it through the Open WebUI Tool Valves. Rotate any key previously reused as a hardcoded example or shared in project source.
- Give access only to trusted users and models. Files or web pages read by an LLM can contain malicious instructions that try to induce tool use (prompt injection).
- Back up important work. `dev_write_file` can overwrite files, and `dev_run_command` can execute destructive commands.
- Make sure the server/container actually enforces the resource and filesystem restrictions you require; the client-side wrapper alone cannot.

Open WebUI explicitly warns that Workspace Tools run Python code inside its server process; inspect source before installing and limit who can edit tools. Read the [Open WebUI Tools documentation](https://docs.openwebui.com/features/extensibility/plugin/tools/) and [Open Terminal security guidance](https://docs.openwebui.com/features/open-terminal/advanced/security/).

## Testing and limitations

The repository includes an offline smoke-test suite and a redirect-security regression. Run them from the repository root with Python 3.11+ and Pydantic 2.x:

```bash
python -m pip install "pydantic>=2,<3"
python -m unittest discover -s toolkit_tests -v
```

The v1.2.3 audit additionally passed **59 standalone regression tests and 300 deterministic rendering stress cases** in a separate audit package; those full audit scripts are not part of this repository's small smoke-test suite. Live acceptance tested approval/denial, source editing, failure exit codes, output truncation, asynchronous process recovery and process termination.

These results do not make shell execution inherently safe. `WORKSPACE_ROOT` is a lexical restriction, **not** a filesystem sandbox, and a permitted command may reach anything accessible to Open Terminal. Container mounts, user permissions and network isolation are the real boundaries.

## Project files

- [`development_toolkit.py`](./development_toolkit.py) — Open WebUI tool source.
- [`toolkit_tests/test_development_toolkit.py`](./toolkit_tests/test_development_toolkit.py) — offline unit smoke tests.

The Open Terminal server is a **separate dependency** and is not included in this repository.

## Licence

MIT. See [LICENSE](LICENSE). Personal and commercial use, modification and redistribution are permitted subject to the MIT licence. Third-party dependencies retain their own licences.