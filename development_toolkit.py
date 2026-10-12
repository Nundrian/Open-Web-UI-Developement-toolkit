"""
title: Development Toolkit
author: Nundrian
version: 1.1.2
description: Open WebUI workspace tools backed by Open Terminal for file editing, command execution, searches, process control, and diagnostics. Use with a restricted Docker workspace.
"""

import asyncio
import json
import posixpath
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from pydantic import BaseModel, Field


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Reject redirects so an API bearer token cannot be forwarded elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Tools:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="http://open-terminal:8000",
            description="Open Terminal base URL as reachable from the Open WebUI container.",
        )
        API_KEY: str = Field(
            default="",
            description="Bearer API key for Open Terminal; configure in Open WebUI Valves (no built-in key).",
        )
        WORKSPACE_ROOT: str = Field(
            default="/workspace",
            description="Root directory this toolkit should use for project files.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=60,
            ge=5,
            le=600,
            description="Default HTTP timeout for calls to Open Terminal.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _safe_path(self, path: str) -> str:
        root = posixpath.normpath(self.valves.WORKSPACE_ROOT or "/workspace")
        if not path or path == ".":
            candidate = root
        elif path.startswith("/"):
            candidate = posixpath.normpath(path)
        else:
            candidate = posixpath.normpath(posixpath.join(root, path))

        if candidate != root and not candidate.startswith(root.rstrip("/") + "/"):
            raise ValueError(
                f"Path '{path}' is outside the allowed workspace root '{root}'."
            )
        return candidate

    def _request_sync(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ):
        if not self.valves.API_KEY:
            raise RuntimeError(
                "Development Toolkit API_KEY is not configured in the tool Valves."
            )

        base = self.valves.BASE_URL.rstrip("/")
        url = f"{base}{endpoint}"

        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
            query = urllib.parse.urlencode(clean_params, doseq=True)
            if query:
                url = f"{url}?{query}"

        data = None
        headers = {
            "Authorization": f"Bearer {self.valves.API_KEY}",
            "Accept": "application/json",
        }

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url=url,
            data=data,
            headers=headers,
            method=method,
        )

        effective_timeout = timeout or self.valves.REQUEST_TIMEOUT_SECONDS

        try:
            # urllib's default redirect handler can forward Authorization headers
            # to another origin. Never follow redirects for authenticated calls.
            opener = urllib.request.build_opener(_RejectRedirects())
            with opener.open(request, timeout=effective_timeout) as response:
                raw = response.read()
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    return json.loads(raw.decode("utf-8"))
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Open Terminal returned HTTP {exc.code}: {details}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach Open Terminal at {base}: {exc.reason}"
            ) from exc

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ):
        return await asyncio.to_thread(
            self._request_sync,
            method,
            endpoint,
            params,
            body,
            timeout,
        )

    @staticmethod
    def _render(value) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    async def diagnose_runtime_config(self) -> str:
        """
        Report the runtime configuration without revealing the API key
        or its fingerprint.

        This function does NOT contact Open Terminal.
        """
        result = {
            "toolkit_version": "1.1.2",
            "base_url": self.valves.BASE_URL,
            "workspace_root": self.valves.WORKSPACE_ROOT,
            "request_timeout_seconds": self.valves.REQUEST_TIMEOUT_SECONDS,
            "api_key_configured": bool(self.valves.API_KEY),
        }

        return self._render(result)

    async def test_open_terminal_auth(self) -> str:
        """
        Test authentication to Open Terminal using the exact runtime
        BASE_URL and API_KEY loaded by Open WebUI.

        This calls only GET /api/config and does not read or modify
        project files, run shell commands, or change any state.
        """

        result = await self._request(
            "GET",
            "/api/config",
        )

        return self._render(result)

    async def test_open_terminal_execute(self) -> str:
        """
        Diagnostic only.

        Calls POST /execute with a fixed harmless command using the same
        internal HTTP path as dev_run_command. Takes no model-supplied
        command or path arguments and does not touch project files.
        """
        result = await self._request(
            "POST",
            "/execute",
            params={
                "wait": 30,
                "tail": 50,
            },
            body={
                "command": "echo TOOLKIT_EXECUTE_TEST",
                "cwd": self._safe_path("."),
            },
            timeout=max(
                self.valves.REQUEST_TIMEOUT_SECONDS,
                45,
            ),
        )

        return self._render(result)

    async def dev_run_command(
        self,
        command: str,
        cwd: str = ".",
        wait: float = 30,
        tail: int = 200,
    ) -> str:
        """
        Run a shell command in Open Terminal. Directory validation checks only
        the starting CWD; command access must be constrained by the server/container.
        Use this for git, python, node, npm, tests, builds, package inspection,
        and development commands. Prefer one bounded command at a time.
        :param command: Shell command to execute.
        :param cwd: Working directory, relative to WORKSPACE_ROOT or an absolute path within it.
        :param wait: Seconds to wait for completion, from 0 to 300.
        :param tail: Maximum number of recent output entries to return.
        """
        safe_cwd = self._safe_path(cwd)
        wait = max(0, min(float(wait), 300))
        tail = max(1, int(tail))
        result = await self._request(
            "POST",
            "/execute",
            params={"wait": wait, "tail": tail},
            body={"command": command, "cwd": safe_cwd},
            timeout=max(self.valves.REQUEST_TIMEOUT_SECONDS, wait + 15),
        )
        return self._render(result)

    async def dev_list_files(self, directory: str = ".") -> str:
        """
        List files and directories in the development workspace.
        :param directory: Directory to list, relative to /workspace or an absolute /workspace path.
        """
        result = await self._request(
            "GET",
            "/files/list",
            params={"directory": self._safe_path(directory)},
        )
        return self._render(result)

    async def dev_read_file(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> str:
        """
        Read a text file from the development workspace.
        Use line ranges for large files instead of loading the entire file.
        :param path: File path relative to /workspace or an absolute /workspace path.
        :param start_line: Optional first line, 1-indexed.
        :param end_line: Optional last line, 1-indexed.
        """
        result = await self._request(
            "GET",
            "/files/read",
            params={
                "path": self._safe_path(path),
                "start_line": start_line,
                "end_line": end_line,
            },
        )
        return self._render(result)

    async def dev_write_file(self, path: str, content: str) -> str:
        """
        Create or overwrite a text file inside /workspace.
        Use only when the intended file content is known.
        :param path: File path relative to /workspace or an absolute /workspace path.
        :param content: Complete text content to write.
        """
        result = await self._request(
            "POST",
            "/files/write",
            body={"path": self._safe_path(path), "content": content},
        )
        return self._render(result)

    async def dev_replace_file_content(
        self,
        path: str,
        target: str,
        replacement: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        allow_multiple: bool = False,
    ) -> str:
        """
        Make a precise find-and-replace edit in one file.
        Prefer this over rewriting a whole existing file when changing a small section.
        :param path: File path inside /workspace.
        :param target: Exact existing text to replace.
        :param replacement: Replacement text.
        :param start_line: Optional first line to search.
        :param end_line: Optional last line to search.
        :param allow_multiple: Replace every match if true; otherwise fail on ambiguity.
        """
        replacement_item = {
            "target": target,
            "replacement": replacement,
            "allow_multiple": allow_multiple,
        }
        if start_line is not None:
            replacement_item["start_line"] = start_line
        if end_line is not None:
            replacement_item["end_line"] = end_line

        result = await self._request(
            "POST",
            "/files/replace",
            body={
                "path": self._safe_path(path),
                "replacements": [replacement_item],
            },
        )
        return self._render(result)

    async def dev_grep_search(
        self,
        query: str,
        path: str = ".",
        regex: bool = False,
        case_insensitive: bool = False,
        include: Optional[List[str]] = None,
        max_results: int = 50,
    ) -> str:
        """
        Search file contents inside /workspace and return matching files, lines and line numbers.
        Use this to locate code before reading or editing it.
        :param query: Literal text or regex pattern.
        :param path: Directory or file to search.
        :param regex: Treat query as regex when true.
        :param case_insensitive: Ignore case when true.
        :param include: Optional filename glob filters such as ['*.py', '*.ts'].
        :param max_results: Maximum matches to return.
        """
        result = await self._request(
            "GET",
            "/files/grep",
            params={
                "query": query,
                "path": self._safe_path(path),
                "regex": str(bool(regex)).lower(),
                "case_insensitive": str(bool(case_insensitive)).lower(),
                "include": include,
                "match_per_line": "true",
                "max_results": max(1, min(int(max_results), 500)),
            },
        )
        return self._render(result)

    async def dev_glob_search(
        self,
        pattern: str,
        path: str = ".",
        exclude: Optional[List[str]] = None,
        max_results: int = 50,
    ) -> str:
        """
        Find files and directories by glob pattern inside /workspace.
        Use this to discover project structure without loading file contents.
        :param pattern: Glob such as '*.py', '**/*.tsx', or 'package.json'.
        :param path: Directory to search.
        :param exclude: Optional glob patterns to exclude.
        :param max_results: Maximum results to return.
        """
        result = await self._request(
            "GET",
            "/files/glob",
            params={
                "pattern": pattern,
                "path": self._safe_path(path),
                "exclude": exclude,
                "type": "any",
                "max_results": max(1, min(int(max_results), 500)),
            },
        )
        return self._render(result)

    async def dev_get_process_status(
        self,
        process_id: str,
        wait: float = 1,
        offset: int = 0,
        tail: int = 200,
    ) -> str:
        """
        Poll a background command started by run_command and retrieve new output.
        :param process_id: Process ID returned by dev_run_command.
        :param wait: Seconds to wait for more output or completion.
        :param offset: Output offset from a previous status response.
        :param tail: Maximum recent output entries to return.
        """
        wait = max(0, min(float(wait), 300))
        result = await self._request(
            "GET",
            f"/execute/{urllib.parse.quote(process_id, safe='')}/status",
            params={
                "wait": wait,
                "offset": max(0, int(offset)),
                "tail": max(1, int(tail)),
            },
            timeout=max(self.valves.REQUEST_TIMEOUT_SECONDS, wait + 15),
        )
        return self._render(result)

    async def dev_kill_process(self, process_id: str, force: bool = False) -> str:
        """
        Stop a background command previously started with dev_run_command.
        :param process_id: Process ID returned by dev_run_command.
        :param force: Send a forceful kill when true.
        """
        result = await self._request(
            "DELETE",
            f"/execute/{urllib.parse.quote(process_id, safe='')}",
            params={"force": str(bool(force)).lower()},
        )
        return self._render(result)
