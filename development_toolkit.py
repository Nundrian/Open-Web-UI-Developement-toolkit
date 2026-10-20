"""
title: Development Toolkit
author: Nundrian
version: 1.2.3
description: File editing, code search, shell commands and process management with lexical workspace path checks in Open Terminal. For trusted Open WebUI users and models.
"""

import asyncio
import fnmatch
import html
import http.client
import json
import math
import posixpath
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Union

from pydantic import BaseModel, Field

TOOLKIT_VERSION = "1.2.3"

# Hard ceiling on what we will read back from Open Terminal in one response.
# The per-call limit that protects the model's context is the MAX_OUTPUT_CHARS Valve.
MAX_HTTP_RESPONSE_BYTES = 2_000_000
ERROR_DETAIL_CHARS = 600
MAX_LINE_NUMBER = 100_000_000
MAX_PATTERNS = 25

# Command text must be fully visible to the approver, so these are rejected outright.
# Raw control characters (including ESC and lone carriage returns) and invisible or
# bidirectional Unicode can make displayed text differ from what actually runs.
_COMMAND_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|\r(?!\n)")
_INVISIBLE = re.compile(
    "[\x00\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

_BINARY_PREFIXES = ("image/", "audio/", "video/", "font/")
_BINARY_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-tar",
}

# Paths whose contents can later be executed or can change how code runs. A write to one
# of these is approved by the user by default, because a harmless-looking command such as
# `bash build.sh` would otherwise run content the user never saw.
_HARMLESS_DOTFILES = {
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".dockerignore",
    ".prettierignore",
    ".eslintignore",
    ".npmignore",
}
_SENSITIVE_NAMES = {
    "cmakelists.txt",
    "makefile",
    "gnumakefile",
    "justfile",
    "dockerfile",
    "jenkinsfile",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "conftest.py",
    "sitecustomize.py",
    "usercustomize.py",
    "build.rs",
    "rakefile",
    "gemfile",
    "pipfile",
    "crontab",
}
_SENSITIVE_PATTERNS = (
    "*.sh",
    "*.bash",
    "*.zsh",
    "*.fish",
    "*.ksh",
    "*.csh",
    "*.ps1",
    "*.bat",
    "*.cmd",
    "*.service",
    "*.timer",
    "*.pth",
    "*.mk",
    "*.desktop",
    "dockerfile.*",
    "*.dockerfile",
    # Source files can be executed by otherwise innocuous-looking shell commands.
    "*.py",
    "*.pyw",
    "*.js",
    "*.mjs",
    "*.cjs",
    "*.ts",
    "*.tsx",
    "*.jsx",
    "*.rb",
    "*.php",
    "*.pl",
    "*.lua",
    "*.go",
    "*.rs",
    "*.java",
    "*.c",
    "*.cc",
    "*.cpp",
    "*.cs",
    "*.swift",
    "*.kt",
    "*.ipynb",
    "*.svelte",
    "*.vue",
    "*.gradle",
    "*.cmake",
    "*.toml",
    "*.yaml",
    "*.yml",
    "*.ini",
    "*.cfg",
    "docker-compose*.y*ml",
    "compose*.y*ml",
    "requirements*.txt",
)


class ToolkitError(Exception):
    """An error with a stable code and a message written for the model to act on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward Open Terminal bearer credentials through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Tools:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="http://open-terminal:8000",
            description="Open Terminal URL reachable from the Open WebUI container.",
        )
        API_KEY: str = Field(
            default="",
            description="Open Terminal bearer key. Set privately in the tool Valves.",
            json_schema_extra={"input": {"type": "password"}},
        )
        WORKSPACE_ROOT: str = Field(
            default="/workspace",
            description="Allowed lexical root for file operations and initial command CWD.",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=60,
            ge=5,
            le=600,
            description="HTTP timeout. Commands that use a longer wait extend it automatically.",
        )
        MAX_OUTPUT_CHARS: int = Field(
            default=10000,
            ge=2000,
            le=50000,
            description="Maximum characters returned to the model per tool call.",
        )
        CONFIRM_SHELL_COMMANDS: bool = Field(
            default=True,
            description="Ask the user before EVERY shell command. Disable only for explicitly authorised unattended workflows.",
        )
        MAX_COMMAND_CHARS: int = Field(
            default=4000,
            ge=200,
            le=20000,
            description="Longest command accepted while approval is on. The full command is always shown for approval, so longer ones are refused; the model is told to put them in a script file.",
        )
        FILE_WRITE_APPROVAL: str = Field(
            default="sensitive",
            description="Approval for writes and replacements: 'sensitive' (source code, scripts, build/config files, dotfiles, shebangs), 'always', or 'never'. Unknown values fail closed as 'always'.",
        )
        EXTRA_SENSITIVE_PATTERNS: str = Field(
            default="",
            description="Extra comma-separated glob patterns (matched against file names and workspace-relative paths) that count as sensitive, e.g. '*.py,scripts/*'.",
        )
        MAX_APPROVAL_CONTENT_CHARS: int = Field(
            default=8000,
            ge=500,
            le=50000,
            description="Largest file content shown in full for approval. Larger approved writes are refused rather than partly shown.",
        )
        CONFIRM_TIMEOUT_SECONDS: int = Field(
            default=60,
            ge=10,
            le=300,
            description="Maximum seconds to wait for interactive approval.",
        )
        EMIT_STATUS: bool = Field(
            default=True,
            description="Show brief status updates during shell command execution.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------
    # Argument coercion. Local models often send "true", "30" or "null".
    # ------------------------------------------------------------------

    @staticmethod
    def _as_bool(value) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            value = value.strip().lower()
            if value in ("true", "1", "yes", "y", "on"):
                return True
            if value in ("false", "0", "no", "n", "off", "", "null", "none"):
                return False
            raise ValueError("Boolean must be true or false.")
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError("Boolean must be true or false.")

    @staticmethod
    def _as_list(value) -> Optional[List[str]]:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.lower() in ("null", "none", "undefined"):
                return None
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                # A real JSON array of strings is a list of patterns. Anything else,
                # such as the glob '[abc].py', is one pattern.
                if isinstance(parsed, list) and all(
                    isinstance(item, str) for item in parsed
                ):
                    value = parsed
                else:
                    value = [text]
            else:
                value = [text]
        if not isinstance(value, (list, tuple)) or len(value) > MAX_PATTERNS:
            raise ValueError(
                f"Patterns must be a list of at most {MAX_PATTERNS} strings."
            )
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError("Each pattern must be a nonempty string.")
        return list(value)

    @staticmethod
    def _as_int(value, minimum: int, maximum: int, name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{name} must be an integer from {minimum} to {maximum}."
            ) from exc
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
        if not minimum <= number <= maximum:
            raise ValueError(f"{name} must be from {minimum} to {maximum}.")
        return number

    @classmethod
    def _as_line(cls, value, name: str) -> Optional[int]:
        """Optional 1-indexed line number. null, none and empty text mean 'not set'; 0 is invalid."""
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in (
            "",
            "null",
            "none",
            "undefined",
        ):
            return None
        return cls._as_int(value, 1, MAX_LINE_NUMBER, name)

    @staticmethod
    def _as_float(value, minimum: float, maximum: float, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a number, not a boolean.")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{name} must be between {minimum} and {maximum}."
            ) from exc
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}.")
        return number

    def _safe_path(self, path: str) -> str:
        """Lexical path validation ONLY; server mounts enforce real isolation (symlinks are not resolved)."""
        root = posixpath.normpath(self.valves.WORKSPACE_ROOT or "/workspace")
        if (
            not root.startswith("/")
            or root == "/"
            or root.startswith("//")
            or any(ord(c) < 32 or ord(c) == 127 for c in root)
            or _INVISIBLE.search(root)
        ):
            raise ValueError("WORKSPACE_ROOT must be an absolute, non-root directory.")
        if path is None or path == "." or path == "":
            candidate = root
        elif not isinstance(path, str):
            raise ValueError("Path must be a string within the workspace.")
        elif any(
            ord(c) < 32 or 127 <= ord(c) <= 159 for c in path
        ) or _INVISIBLE.search(path):
            raise ValueError("Path contains an invalid character.")
        elif path.startswith("/"):
            candidate = posixpath.normpath(path)
        else:
            candidate = posixpath.normpath(posixpath.join(root, path))
        if candidate != root and not candidate.startswith(root.rstrip("/") + "/"):
            raise ValueError(
                "Path is outside the workspace. Use a path relative to WORKSPACE_ROOT."
            )
        return candidate

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------

    def _safe_file_path(self, path: str) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError("path must name a workspace file.")
        result = self._safe_path(path)
        if result == posixpath.normpath(self.valves.WORKSPACE_ROOT or "/workspace"):
            raise ValueError("path must name a file, not the workspace root.")
        return result

    @staticmethod
    def _process_id(process_id: str) -> str:
        if not isinstance(process_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", process_id
        ):
            raise ValueError(
                "process_id must be an ID from dev_run_command or dev_list_processes."
            )
        return urllib.parse.quote(process_id, safe="")

    def _redact(self, value: str) -> str:
        text = str(value)
        secret = self.valves.API_KEY
        if secret:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _error(self, code: str, message: str) -> str:
        return self._render(
            {"ok": False, "error": code, "message": self._redact(message)[:900]}
        )

    @staticmethod
    def _compact(obj) -> str:
        return json.dumps(
            obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )

    @staticmethod
    def _clip_middle(text: str, limit: int) -> str:
        """Bound the entire preview, including the omission marker."""
        if len(text) <= limit:
            return text
        if limit < 40:
            return text[: max(0, limit)]
        # The largest possible marker reserves sufficient room in one pass.
        marker_room = len(f"...[{len(text)} characters omitted]...")
        kept = max(0, limit - marker_room)
        head = kept * 2 // 5
        tail = kept - head
        marker = f"...[{len(text) - kept} characters omitted]..."
        return text[:head] + marker + (text[-tail:] if tail else "")

    @classmethod
    def _shrink_text(cls, text: str, cap: int) -> str:
        total = len(text)
        header = (
            f"[toolkit_truncated: {total} characters in total; showing start and end. "
            "Read smaller line ranges or narrow the request.]\n"
        )
        room = max(0, cap - len(header) - 50)
        head = int(room * 0.4)
        tail = room - head
        omitted = max(0, total - head - tail)
        suffix = text[-tail:] if tail else ""
        result = (
            header
            + text[:head]
            + f"\n[... {omitted} characters omitted ...]\n"
            + suffix
        )
        return result[:cap]

    @classmethod
    def _preview_json(cls, value, budget: int, depth: int = 0) -> str:
        """Return valid JSON within budget; visit at most 32 entries per level.

        Root process facts are handled separately by _shrink_structured. Nested
        previews retain both endpoints and never rewrite identity/status fields.
        """
        if budget < 4:
            return "null"
        if isinstance(value, str):
            # Encoding only a bounded slice avoids repeatedly encoding huge strings.
            preview = cls._clip_middle(value, max(0, budget - 2))
            encoded = cls._compact(preview)
            if len(encoded) <= budget:
                return encoded
            lo, hi = 0, min(len(value), budget - 2)
            best = '""'
            while lo <= hi:
                mid = (lo + hi) // 2
                encoded = cls._compact(cls._clip_middle(value, mid))
                if len(encoded) <= budget:
                    best, lo = encoded, mid + 1
                else:
                    hi = mid - 1
            return best
        if not isinstance(value, (dict, list)):
            encoded = cls._compact(value)
            return encoded if len(encoded) <= budget else "null"
        if depth >= 8:
            return '"[nested content omitted]"' if budget >= 26 else "null"
        if not value:
            return "{}" if isinstance(value, dict) else "[]"
        if isinstance(value, list):
            count = min(len(value), 32, max(1, budget // 160))
            if len(value) > 1 and budget >= 180:
                count = max(2, count)
            head = (count + 1) // 2
            indices = list(range(head)) + list(
                range(len(value) - count + head, len(value))
            )
            omitted = len(value) - count
            marker = (
                cls._compact({"toolkit_omitted_entries": omitted}) if omitted else None
            )
            room = budget - 2 - max(0, count - 1) - (len(marker) + 1 if marker else 0)
            per_item = room // count
            if per_item < 4:
                return "[]"
            pieces = [cls._preview_json(value[i], per_item, depth + 1) for i in indices]
            if marker:
                pieces.insert(head, marker)
            return "[" + ",".join(pieces) + "]"
        # Dictionaries are streamed by key: no repeated serialization of the
        # progressively growing response and no quadratic omitted-field loop.
        keys = list(value)
        protected = [
            k
            for k in (
                "id",
                "status",
                "exit_code",
                "next_offset",
                "truncated",
                "ok",
                "error",
            )
            if k in value
        ]
        ordered = protected + [k for k in keys if k not in protected]
        count = min(len(ordered), 16)
        selected = ordered[:count]
        omitted = len(keys) > count
        marker = '"toolkit_omitted":true'
        room = budget - 2 - (len(marker) + 1 if omitted else 0)
        pieces = []
        for index, key in enumerate(selected):
            prefix = cls._compact(str(key)) + ":"
            allocation = max(0, room // (count - index) - len(prefix) - 1)
            if allocation < 4:
                omitted = True
                continue
            if key in protected:
                encoded = cls._compact(value[key])
                if len(encoded) > allocation:
                    omitted = True
                    continue  # Omit an identity rather than silently corrupting it.
            else:
                encoded = cls._preview_json(value[key], allocation, depth + 1)
            piece = prefix + encoded
            if len(piece) + (1 if pieces else 0) <= room:
                pieces.append(piece)
                room -= len(piece) + 1
            else:
                omitted = True
        if omitted and len(marker) + (1 if pieces else 0) <= room:
            pieces.append(marker)
        return "{" + ",".join(pieces) + "}"

    def _shrink_structured(self, value: dict, cap: int) -> str:
        """Preserve exact process facts and emit bounded, valid content previews."""
        protected = (
            "id",
            "status",
            "exit_code",
            "next_offset",
            "truncated",
            "ok",
            "error",
        )
        result = {k: value[k] for k in protected if k in value}
        if "error" in result and len(self._compact(result["error"])) > cap // 4:
            result["error"] = json.loads(
                self._preview_json(result["error"], min(300, cap // 4))
            )
        result.update(
            {
                "toolkit_truncated": True,
                "toolkit_note": "Content shortened; read smaller ranges. Process facts are unchanged.",
            }
        )
        encoded = self._compact(result)
        if len(encoded) > cap - 100:
            return self._compact(
                {
                    "ok": False,
                    "error": "RESPONSE_METADATA_TOO_LARGE",
                    "message": "Server metadata exceeds the output limit; content omitted.",
                }
            )
        # Paths are kept whole whenever possible; a clipped path is not a usable path.
        for key in ("cwd", "log_path"):
            if key in value:
                piece = self._compact(key) + ":" + self._compact(value[key])
                if len(encoded) + len(piece) + 1 < cap // 2:
                    encoded = encoded[:-1] + "," + piece + "}"
                    result[key] = value[key]
        keys = [
            k
            for k in value
            if k not in result and k not in ("toolkit_note", "toolkit_truncated")
        ]
        priority = [
            k
            for k in (
                "output",
                "stdout",
                "stderr",
                "message",
                "content",
                "matches",
                "entries",
                "processes",
                "items",
            )
            if k in keys
        ]
        keys = priority + [k for k in keys if k not in priority]
        # Reserve an explicit omission count and cap work for wide objects.
        remaining = cap - len(encoded) - 65
        pieces = []
        omitted = 0
        for index, key in enumerate(keys[:32]):
            prefix = self._compact(key) + ":"
            slots = min(32, len(keys)) - index
            share = remaining if slots == 1 else max(80, remaining // slots)
            if key in priority:
                share = max(share, remaining * 3 // 4)
            budget = min(share, remaining) - len(prefix) - 1
            if budget < 40:
                omitted += 1
                continue
            preview = self._preview_json(value[key], budget)
            piece = prefix + preview
            if len(piece) + 1 <= remaining:
                pieces.append(piece)
                remaining -= len(piece) + 1
            else:
                omitted += 1
        omitted += max(0, len(keys) - 32)
        if omitted:
            pieces.append('"toolkit_omitted_field_count":' + str(omitted))
        return encoded[:-1] + ("," + ",".join(pieces) if pieces else "") + "}"

    def _redact_structure(self, value):
        """Redact secrets *before* encoding so redaction never breaks JSON syntax."""
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, list):
            return [self._redact_structure(x) for x in value]
        if isinstance(value, dict):
            return {
                self._redact(str(k)): self._redact_structure(v)
                for k, v in value.items()
            }
        return value

    def _render(self, value) -> str:
        cap = int(self.valves.MAX_OUTPUT_CHARS)
        if isinstance(value, str):
            text = self._redact(value)
            return text if len(text) <= cap else self._shrink_text(text, cap)
        try:
            value = self._redact_structure(value)
            text = self._compact(value)
            if len(text) <= cap:
                return text
            if isinstance(value, list):
                value = {"items": value}
            if isinstance(value, dict):
                return self._shrink_structured(value, cap)
            return self._compact(
                {"toolkit_truncated": True, "toolkit_note": "Large scalar omitted."}
            )
        except (RecursionError, ValueError, TypeError):
            return self._compact(
                {
                    "ok": False,
                    "error": "SERVER_ERROR",
                    "message": "Server data cannot be rendered as bounded JSON.",
                }
            )

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    @staticmethod
    def _http_detail(raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict) and "detail" in parsed:
            detail = parsed["detail"]
            text = (
                detail
                if isinstance(detail, str)
                else json.dumps(detail, ensure_ascii=False)
            )
        return text  # Redact BEFORE applying the presentation limit.

    @staticmethod
    def _http_error(code: int, detail: str) -> ToolkitError:
        extra = f" {detail}" if detail else ""
        if code in (401, 403):
            return ToolkitError(
                "AUTH_FAILED",
                "Open Terminal rejected the API key. Check API_KEY in the toolkit Valves against the terminal container.",
            )
        if code == 404:
            return ToolkitError(
                "NOT_FOUND",
                f"Not found.{extra} Check the path with dev_list_files, or the process ID with dev_list_processes.",
            )
        if code == 415:
            return ToolkitError(
                "BINARY_FILE", f"Open Terminal cannot return this file as text.{extra}"
            )
        if code in (400, 409, 422):
            return ToolkitError(
                "INVALID_ARGUMENT", f"Open Terminal rejected the request.{extra}"
            )
        if code >= 500:
            return ToolkitError(
                "SERVER_ERROR", f"Open Terminal failed internally (HTTP {code}).{extra}"
            )
        return ToolkitError("TOOL_ERROR", f"Open Terminal returned HTTP {code}.{extra}")

    @staticmethod
    def _recovery_hint(method: str, endpoint: str) -> str:
        if method == "POST" and endpoint == "/execute":
            return (
                " The command may STILL be running. Do not run it again yet: "
                "call dev_list_processes, then dev_get_process_status or dev_kill_process."
            )
        if method == "POST":
            return " The change may have been applied; re-read the file before trying again."
        if method == "DELETE":
            return " Termination may have happened; check dev_get_process_status before retrying."
        return " Request a smaller range or check the connection before retrying."

    @classmethod
    def _timeout_error(cls, method: str, endpoint: str, seconds: float) -> ToolkitError:
        return ToolkitError(
            "TIMEOUT",
            f"Open Terminal did not respond within {seconds:.0f}s."
            + cls._recovery_hint(method, endpoint),
        )

    @staticmethod
    def _validate_process(value) -> None:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not value["id"]
            or len(value["id"]) > 256
            or value.get("status") not in ("running", "done", "killed")
            or "exit_code" not in value
            or (value["exit_code"] is not None and type(value["exit_code"]) is not int)
        ):
            raise ToolkitError(
                "SERVER_ERROR", "Open Terminal returned malformed process metadata."
            )
        if "next_offset" in value and (
            type(value["next_offset"]) is not int
            or not 0 <= value["next_offset"] <= 2**63 - 1
        ):
            raise ToolkitError(
                "SERVER_ERROR", "Open Terminal returned an invalid output offset."
            )
        if "output" in value and not isinstance(value["output"], list):
            raise ToolkitError(
                "SERVER_ERROR", "Open Terminal returned invalid process output."
            )

    @classmethod
    def _validate_response(cls, value, method: str, endpoint: str):
        """Validate endpoint envelopes, leaving server content and failures intact."""
        if isinstance(value, dict) and (value.get("ok") is False or value.get("error")):
            return value  # Never turn an explicit server failure into success.
        if endpoint == "/execute" and method == "GET":
            if not isinstance(value, list):
                raise ToolkitError(
                    "SERVER_ERROR", "Open Terminal returned an unexpected process list."
                )
            for row in value:
                cls._validate_process(row)
        elif endpoint == "/execute" or endpoint.endswith("/status"):
            cls._validate_process(value)
        elif method == "DELETE":
            if not isinstance(value, dict) or value.get("status") != "killed":
                raise ToolkitError(
                    "SERVER_ERROR", "Open Terminal did not confirm termination."
                )
        else:
            fields = {
                "/files/list": {"dir": str, "entries": list},
                "/files/read": {"path": str, "content": str, "total_lines": int},
                "/files/write": {"path": str, "size": int},
                "/files/replace": {"path": str, "size": int},
                "/files/grep": {"matches": list},
                "/files/glob": {"matches": list},
            }
            expected = fields.get(endpoint, {})
            if not isinstance(value, dict) or any(
                type(value.get(k)) is not t for k, t in expected.items()
            ):
                raise ToolkitError(
                    "SERVER_ERROR",
                    "Open Terminal returned an unexpected response shape.",
                )
            if "size" in expected and value["size"] < 0:
                raise ToolkitError(
                    "SERVER_ERROR", "Open Terminal returned an invalid file size."
                )
        return value

    @staticmethod
    def _reject_json_constant(value):
        raise ValueError("Non-finite JSON number.")

    def _request_sync(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ):
        if not self.valves.API_KEY:
            raise ToolkitError(
                "AUTH_FAILED",
                "Open Terminal API key is missing. Set API_KEY in tool Valves.",
            )
        if not self.valves.API_KEY.isascii() or any(
            ord(c) <= 32 or ord(c) >= 127 for c in self.valves.API_KEY
        ):
            raise ValueError(
                "API_KEY must contain visible ASCII token characters without spaces."
            )
        base = self.valves.BASE_URL.rstrip("/")
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in base):
            raise ValueError(
                "BASE_URL must not contain whitespace or control characters."
            )
        parsed = urllib.parse.urlsplit(base)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("BASE_URL has an invalid port.") from exc
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in base
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError(
                "BASE_URL must be an http(s) URL without credentials, query or fragment."
            )
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError("Invalid Open Terminal endpoint.")
        url = base + endpoint
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        headers = {
            "Authorization": f"Bearer {self.valves.API_KEY}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url=url, data=data, headers=headers, method=method
        )
        seconds = (
            timeout if timeout is not None else self.valves.REQUEST_TIMEOUT_SECONDS
        )
        try:
            opener = urllib.request.build_opener(_RejectRedirects())
            with opener.open(request, timeout=seconds) as response:
                # read1 returns available bytes, allowing a total response deadline
                # to be checked even when a peer dribbles a large response slowly.
                chunks, size = [], 0
                deadline = time.monotonic() + seconds
                read = getattr(response, "read1", response.read)
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    chunk = read(min(65536, MAX_HTTP_RESPONSE_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_HTTP_RESPONSE_BYTES:
                        raise ToolkitError(
                            "RESPONSE_TOO_LARGE",
                            "Open Terminal response exceeded 2 MB. Request smaller results.",
                        )
                raw = b"".join(chunks)
                kind = (
                    response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if kind == "application/json" or kind.endswith("+json"):
                    try:
                        value = json.loads(
                            raw.decode("utf-8"),
                            parse_constant=self._reject_json_constant,
                        )
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise ToolkitError(
                            "SERVER_ERROR",
                            "Open Terminal returned invalid or excessively nested JSON.",
                        ) from exc
                    return self._validate_response(value, method, endpoint)
                if method == "GET" and endpoint == "/files/read":
                    if kind.startswith(_BINARY_PREFIXES) or kind in _BINARY_TYPES:
                        return {
                            "binary": True,
                            "content_type": kind,
                            "bytes": len(raw),
                            "message": "Binary content is not shown.",
                        }
                    # Older servers may return a text file directly.
                    if kind.startswith("text/plain"):
                        return raw.decode("utf-8", errors="replace")
                raise ToolkitError(
                    "SERVER_ERROR",
                    "Open Terminal returned non-JSON API data; check BASE_URL.",
                )
        except urllib.error.HTTPError as exc:
            try:
                # Read enough for a useful detail; never print authentication replies.
                raw = exc.read(8193)
                detail = (
                    "Error response was too large; details omitted."
                    if len(raw) > 8192
                    else self._redact(self._http_detail(raw))[:ERROR_DETAIL_CHARS]
                )
            finally:
                exc.close()
            error = self._http_error(exc.code, detail)
            if exc.code >= 500 and method != "GET":
                error.message += self._recovery_hint(method, endpoint)
            raise error from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise self._timeout_error(method, endpoint, seconds) from exc
            raise ToolkitError(
                "UNREACHABLE",
                "Open Terminal connection failed."
                + self._recovery_hint(method, endpoint),
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise self._timeout_error(method, endpoint, seconds) from exc
        except ToolkitError as exc:
            if method != "GET":
                raise ToolkitError(
                    exc.code, exc.message + self._recovery_hint(method, endpoint)
                ) from exc
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise ToolkitError(
                "CONNECTION_ERROR",
                "Open Terminal connection ended without a complete response."
                + self._recovery_hint(method, endpoint),
            ) from exc

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ):
        # Cancelling an awaiting task cannot stop urllib's worker thread or a
        # command already accepted by the server. Never automatically retry.
        seconds = (
            timeout if timeout is not None else self.valves.REQUEST_TIMEOUT_SECONDS
        )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._request_sync, method, endpoint, params, body, timeout
                ),
                timeout=seconds,
            )
        except asyncio.TimeoutError as exc:
            raise self._timeout_error(method, endpoint, seconds) from exc

    # ------------------------------------------------------------------
    # Status events and approval
    # ------------------------------------------------------------------

    async def _status(self, emitter, message: str, done: bool = False):
        if emitter is not None and self.valves.EMIT_STATUS:
            try:
                await emitter(
                    {"type": "status", "data": {"description": message, "done": done}}
                )
            except Exception:
                # Status events are advisory; never claim work failed because
                # the browser closed or a status notification could not deliver.
                pass

    async def _confirm(
        self, event_call, message: str, title: str = "Approve development action"
    ) -> bool:
        if not callable(event_call):
            return False
        if self.valves.API_KEY and self.valves.API_KEY in message:
            return False  # Never send the configured credential to the browser.
        # ConfirmDialog parses Markdown then sanitizes HTML. Escape the complete
        # payload inside a static pre/code block; give long reviews their own
        # scrolling area because the host dialog itself has a fixed height.
        display = (
            '<pre style="max-height:50vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere">'
            "<code>" + html.escape(message) + "</code></pre>"
        )
        try:
            response = await asyncio.wait_for(
                event_call(
                    {
                        "type": "confirmation",
                        "data": {"title": title, "message": display},
                    }
                ),
                timeout=self.valves.CONFIRM_TIMEOUT_SECONDS,
            )
        except Exception:
            return False  # Cancellation is BaseException: propagate it.
        if isinstance(response, dict):
            return "error" not in response and response.get("confirmed") is True
        return response is True

    @staticmethod
    def _approval_denied() -> str:
        return (
            "The user did not approve this action, or interactive approval is unavailable in this context. "
            "Do not retry the same action; ask the user how to proceed."
        )

    def _check_command(self, command) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a nonempty string.")
        if _COMMAND_CONTROL.search(command) or _INVISIBLE.search(command):
            raise ValueError(
                "command contains control or invisible characters that cannot be shown for approval. "
                "Use printf escapes such as \\x1b instead of raw control characters."
            )
        limit = (
            self.valves.MAX_COMMAND_CHARS
            if self.valves.CONFIRM_SHELL_COMMANDS
            else 100000
        )
        if len(command) > limit:
            raise ValueError(
                f"command is {len(command)} characters; the limit is {limit}. "
                "Write long scripts to a file with dev_write_file and run that file instead."
            )
        return command

    # ------------------------------------------------------------------
    # File write approval policy
    # ------------------------------------------------------------------

    def _extra_patterns(self) -> List[str]:
        return [
            p.strip().lower()
            for p in (self.valves.EXTRA_SENSITIVE_PATTERNS or "").split(",")
            if p.strip()
        ]

    def _is_sensitive_path(self, safe_path: str, content: Optional[str] = None) -> bool:
        root = posixpath.normpath(self.valves.WORKSPACE_ROOT or "/workspace")
        relative = posixpath.relpath(safe_path, root)
        parts = [part for part in relative.split("/") if part and part != "."]
        if not parts:
            return False
        base = parts[-1].lower()
        if any(part.startswith(".") for part in parts[:-1]):
            return True
        if base.startswith(".") and base not in _HARMLESS_DOTFILES:
            return True
        if base in _SENSITIVE_NAMES:
            return True
        if any(fnmatch.fnmatchcase(base, pattern) for pattern in _SENSITIVE_PATTERNS):
            return True
        relative_lower = "/".join(part.lower() for part in parts)
        for pattern in self._extra_patterns():
            if fnmatch.fnmatchcase(base, pattern) or fnmatch.fnmatchcase(
                relative_lower, pattern
            ):
                return True
        if content is not None and content.lstrip("\ufeff").startswith("#!"):
            return True
        return False

    def _file_approval_reason(
        self, safe_path: str, content: Optional[str] = None
    ) -> Optional[str]:
        """Return why approval is needed, or None when the write may proceed without asking."""
        mode = str(self.valves.FILE_WRITE_APPROVAL or "").strip().lower()
        if mode == "never":
            return None
        if mode == "sensitive":
            return (
                "sensitive path or content (source code, script, build or config file)"
                if self._is_sensitive_path(safe_path, content)
                else None
            )
        return "approval policy is set to ask for every file change"

    async def _file_change_reason(
        self, safe_path: str, content: str, target: Optional[str] = None
    ) -> Optional[str]:
        reason = self._file_approval_reason(safe_path, content)
        if (
            reason
            or str(self.valves.FILE_WRITE_APPROVAL).strip().lower() != "sensitive"
        ):
            return reason
        # A filename alone cannot classify an existing extensionless script. This
        # one read is only needed when the path/new content did not establish policy.
        try:
            result = await self._request(
                "GET",
                "/files/read",
                params={"path": safe_path, "start_line": 1, "end_line": 1},
            )
        except ToolkitError as exc:
            if exc.code == "NOT_FOUND":
                return None  # New file, no existing shebang.
            raise
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            first = result["content"]
        elif isinstance(result, str):
            first = result
        else:
            # A binary/error/malformed response cannot establish ordinary content.
            raise ToolkitError(
                "APPROVAL_REQUIRED",
                "Cannot inspect existing content safely; approval is required.",
            )
        prospective = first.replace(target, content) if target else content
        if self._is_sensitive_path(safe_path, first) or self._is_sensitive_path(
            safe_path, prospective
        ):
            return "existing or resulting shebang script"
        if (
            target
            and "\n" in target
            and first.endswith(target.split("\n", 1)[0] + "\n")
        ):
            # Removing/joining the first line can expose a shebang in unread text.
            # Approve the exact edit rather than fetching the whole file or guessing.
            return "replacement may change or reveal the first line of a script"
        return None

    async def _approve_file_change(
        self, event_call, reason: str, body: str, sizes: List[int]
    ) -> None:
        if any(size > self.valves.MAX_APPROVAL_CONTENT_CHARS for size in sizes):
            raise ToolkitError(
                "APPROVAL_REQUIRED",
                f"Change too large to show in full (limit {self.valves.MAX_APPROVAL_CONTENT_CHARS} characters per part). Split the change or ask the user how to proceed.",
            )
        if _INVISIBLE.search(body) or _COMMAND_CONTROL.search(body):
            raise ToolkitError(
                "INVALID_ARGUMENT",
                "Content contains invisible or control characters that cannot be shown for approval. Remove them.",
            )
        if not await self._confirm(
            event_call, f"Reason: {reason}\n\n{body}", title="Approve file change"
        ):
            raise ToolkitError("APPROVAL_REQUIRED", self._approval_denied())

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    @staticmethod
    def _code_for_error(exc: Exception) -> str:
        if isinstance(exc, ToolkitError):
            return exc.code
        if isinstance(exc, ValueError):
            return "INVALID_ARGUMENT"
        if isinstance(exc, TimeoutError):
            return "TIMEOUT"
        return "TOOL_ERROR"

    def _handle_error(self, exc: Exception) -> str:
        message = exc.message if isinstance(exc, ToolkitError) else str(exc)
        return self._error(
            self._code_for_error(exc),
            message or "Tool call failed. Inspect the connection and arguments.",
        )

    # ------------------------------------------------------------------
    # Diagnostics: private on purpose so local LLMs never see their schemas.
    # ------------------------------------------------------------------

    def _diagnostic_base_url(self) -> str:
        try:
            value = self.valves.BASE_URL
            parsed = urllib.parse.urlsplit(value)
            if (
                parsed.username is not None
                or parsed.password is not None
                or any(c.isspace() for c in value)
            ):
                return "[invalid BASE_URL; details omitted]"
            return self._redact(value)
        except ValueError:
            return "[invalid BASE_URL; details omitted]"

    async def _diagnose_runtime_config(self) -> str:
        return self._render(
            {
                "toolkit_version": TOOLKIT_VERSION,
                "base_url": self._diagnostic_base_url(),
                "workspace_root": self.valves.WORKSPACE_ROOT,
                "request_timeout_seconds": self.valves.REQUEST_TIMEOUT_SECONDS,
                "max_output_chars": self.valves.MAX_OUTPUT_CHARS,
                "api_key_configured": bool(self.valves.API_KEY),
                "confirm_shell_commands": self.valves.CONFIRM_SHELL_COMMANDS,
                "max_command_chars": self.valves.MAX_COMMAND_CHARS,
                "file_write_approval": self.valves.FILE_WRITE_APPROVAL,
            }
        )

    async def _test_open_terminal_auth(self) -> str:
        # /api/config has no authentication, so it cannot prove the key works.
        # /files/list requires a valid key.
        try:
            return self._render(
                await self._request(
                    "GET", "/files/list", params={"directory": self._safe_path(".")}
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def _test_open_terminal_execute(self) -> str:
        # Diagnostics obey exactly the same policy as a model-visible command.
        return await self.dev_run_command("echo TOOLKIT_EXECUTE_TEST", wait=30, tail=50)

    # ------------------------------------------------------------------
    # Public tools
    # ------------------------------------------------------------------

    async def dev_run_command(
        self,
        command: str,
        cwd: str = ".",
        wait: float = 30,
        tail: int = 200,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Run one bounded shell command in Open Terminal, not in Open WebUI.
        Commands can access anything their container can access; use caution.
        If it times out, do not rerun it: use dev_list_processes then dev_get_process_status.
        :param command: Shell command to execute (avoid destructive commands).
        :param cwd: Starting folder inside WORKSPACE_ROOT; not a shell sandbox.
        :param wait: Wait up to 300 seconds for a response.
        :param tail: Number of output entries to return.
        """
        status_message = None  # None means no status event is open
        try:
            command = self._check_command(command)
            safe_cwd = self._safe_path(cwd)
            seconds = self._as_float(wait, 0, 300, "wait")
            count = self._as_int(tail, 1, 1000, "tail")
            if self.valves.CONFIRM_SHELL_COMMANDS:
                approved = await self._confirm(
                    __event_call__,
                    f"Working directory: {safe_cwd}\nLength: {len(command)} characters\n\n{command}",
                    title="Approve shell command",
                )
                if not approved:
                    return self._error("APPROVAL_REQUIRED", self._approval_denied())
            status_message = "Command not started."
            await self._status(__event_emitter__, "Running command in Open Terminal…")
            status_message = "Command interrupted. It may still be running; check dev_list_processes."
            result = await self._request(
                "POST",
                "/execute",
                params={"wait": seconds, "tail": count},
                body={"command": command, "cwd": safe_cwd},
                timeout=max(self.valves.REQUEST_TIMEOUT_SECONDS, seconds + 15),
            )
            status_message = "Command response received."
            return self._render(result)
        except Exception as exc:
            if status_message is not None:
                status_message = "Command request ended; check dev_list_processes if its outcome is uncertain."
            return self._handle_error(exc)
        finally:
            # Also runs when the chat is cancelled, so the status never stays 'running'
            # and never claims success it did not see.
            if status_message is not None:
                await self._status(__event_emitter__, status_message, done=True)

    async def dev_list_files(self, directory: str = ".") -> str:
        """List files in one workspace directory. :param directory: Relative workspace folder."""
        try:
            return self._render(
                await self._request(
                    "GET",
                    "/files/list",
                    params={"directory": self._safe_path(directory)},
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dev_read_file(
        self,
        path: str,
        start_line: Optional[Union[int, str]] = None,
        end_line: Optional[Union[int, str]] = None,
    ) -> str:
        """
        Read a text file. Prefer a small line range for large files.
        :param path: File relative to the workspace.
        :param start_line: First line, starting at 1.
        :param end_line: Last line, inclusive.
        """
        try:
            start = self._as_line(start_line, "start_line")
            end = self._as_line(end_line, "end_line")
            if start is not None and end is not None and end < start:
                raise ValueError("end_line must not be before start_line.")
            return self._render(
                await self._request(
                    "GET",
                    "/files/read",
                    params={
                        "path": self._safe_file_path(path),
                        "start_line": start,
                        "end_line": end,
                    },
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dev_write_file(self, path: str, content: str, __event_call__=None) -> str:
        """
        Create or overwrite a workspace file. Read the file first before overwriting.
        Scripts and config files may need the user's approval.
        :param path: Target file relative to WORKSPACE_ROOT.
        :param content: Complete text to write as a string; serialize JSON to text first.
        """
        try:
            if not isinstance(content, str):
                raise ValueError(
                    "content must be a string. For JSON files, serialize the data to text first."
                )
            safe_path = self._safe_file_path(path)
            reason = await self._file_change_reason(safe_path, content)
            if reason:
                await self._approve_file_change(
                    __event_call__,
                    reason,
                    f"Create or overwrite: {safe_path}\nLength: {len(content)} characters\n\n--- content ---\n{content}\n--- end ---",
                    [len(content)],
                )
            return self._render(
                await self._request(
                    "POST",
                    "/files/write",
                    body={
                        "path": safe_path,
                        "content": content,
                    },
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dev_replace_file_content(
        self,
        path: str,
        target: str,
        replacement: str,
        start_line: Optional[Union[int, str]] = None,
        end_line: Optional[Union[int, str]] = None,
        allow_multiple: bool = False,
        __event_call__=None,
    ) -> str:
        """
        Replace exact existing text; read the file first. Defaults to one match.
        Scripts and config files may need the user's approval.
        :param path: Workspace file.
        :param target: Exact existing text.
        :param replacement: New text.
        :param start_line: Optional first line to search.
        :param end_line: Optional last line to search.
        :param allow_multiple: Replace every occurrence when true.
        """
        try:
            if not isinstance(target, str) or not target:
                raise ValueError("target must be nonempty text.")
            if not isinstance(replacement, str):
                raise ValueError("replacement must be text.")
            start = self._as_line(start_line, "start_line")
            end = self._as_line(end_line, "end_line")
            if start is not None and end is not None and end < start:
                raise ValueError("end_line must not be before start_line.")
            multiple = self._as_bool(allow_multiple)
            safe_path = self._safe_file_path(path)
            item = {
                "target": target,
                "replacement": replacement,
                "allow_multiple": multiple,
            }
            if start is not None:
                item["start_line"] = start
            if end is not None:
                item["end_line"] = end
            reason = await self._file_change_reason(safe_path, replacement, target)
            if reason:
                scope = "every match" if multiple else "one match"
                await self._approve_file_change(
                    __event_call__,
                    reason,
                    f"Edit: {safe_path}\nLines: {start or 1} through {end or 'end'}\nReplaces {scope}\n\n--- replace this ---\n{target}\n--- with this ---\n{replacement}\n--- end ---",
                    [len(target), len(replacement)],
                )
            try:
                result = await self._request(
                    "POST",
                    "/files/replace",
                    body={
                        "path": safe_path,
                        "replacements": [item],
                    },
                )
            except ToolkitError as exc:
                if exc.code == "INVALID_ARGUMENT":
                    raise ToolkitError(
                        exc.code,
                        exc.message
                        + " Re-read the file with dev_read_file; target must match exactly, "
                        "including whitespace. Set allow_multiple=true to replace every match.",
                    ) from exc
                raise
            return self._render(result)
        except Exception as exc:
            return self._handle_error(exc)

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
        Search file contents with line numbers. Start with a narrow search.
        :param query: Text or regex to search for.
        :param path: Workspace file or folder.
        :param regex: Interpret query as regex when true.
        :param case_insensitive: Ignore letter case when true.
        :param include: Optional filename pattern(s), e.g. ['*.py'].
        :param max_results: Maximum matches, up to 500.
        """
        try:
            if not isinstance(query, str) or not query:
                raise ValueError("query must be nonempty text.")
            return self._render(
                await self._request(
                    "GET",
                    "/files/grep",
                    params={
                        "query": query,
                        "path": self._safe_path(path),
                        "regex": str(self._as_bool(regex)).lower(),
                        "case_insensitive": str(
                            self._as_bool(case_insensitive)
                        ).lower(),
                        "include": self._as_list(include),
                        "match_per_line": "true",
                        "max_results": self._as_int(max_results, 1, 500, "max_results"),
                    },
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dev_glob_search(
        self,
        pattern: str,
        path: str = ".",
        exclude: Optional[List[str]] = None,
        max_results: int = 50,
    ) -> str:
        """
        Find paths matching a filename glob; use listing to inspect a folder.
        :param pattern: Glob, e.g. '**/*.py'.
        :param path: Workspace folder to search.
        :param exclude: Optional excluded filename patterns.
        :param max_results: Maximum matching paths, up to 500.
        """
        try:
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("pattern must be nonempty text.")
            return self._render(
                await self._request(
                    "GET",
                    "/files/glob",
                    params={
                        "pattern": pattern,
                        "path": self._safe_path(path),
                        "exclude": self._as_list(exclude),
                        "type": "any",
                        "max_results": self._as_int(max_results, 1, 500, "max_results"),
                    },
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dev_list_processes(self, status: str = "any", limit: int = 20) -> str:
        """
        List commands started with dev_run_command: id, command, status and exit code.
        Use it to recover a process ID after a timeout, then call dev_get_process_status.
        :param status: Filter: any, running, done or killed.
        :param limit: Most recent processes to show, 1 to 100.
        """
        try:
            wanted = "any" if status is None else str(status).strip().lower()
            if wanted in ("", "null", "none"):
                wanted = "any"
            if wanted not in ("any", "running", "done", "killed"):
                raise ValueError("status must be any, running, done or killed.")
            count = self._as_int(limit, 1, 100, "limit")
            result = await self._request("GET", "/execute")
            if not isinstance(result, list):
                raise ToolkitError(
                    "SERVER_ERROR", "Open Terminal returned an unexpected process list."
                )
            for row in result:
                self._validate_process(row)
            rows = result
            if wanted != "any":
                rows = [row for row in rows if row.get("status") == wanted]
            # Process IDs start with a timestamp, so reverse order is newest first.
            rows.sort(key=lambda row: str(row.get("id", "")), reverse=True)
            shown = [
                {
                    "id": row.get("id"),
                    "status": row.get("status"),
                    "exit_code": row.get("exit_code"),
                    "command": self._clip_middle(str(row.get("command", "")), 200),
                }
                for row in rows[:count]
            ]
            return self._render(
                {"total_matching": len(rows), "shown": len(shown), "processes": shown}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dev_get_process_status(
        self, process_id: str, wait: float = 1, offset: int = 0, tail: int = 200
    ) -> str:
        """
        Check status and new output of a command started by dev_run_command.
        :param process_id: Process ID returned by dev_run_command or dev_list_processes.
        :param wait: Wait up to 300 seconds for the command to finish.
        :param offset: Use next_offset from the previous response to read only new output.
        :param tail: Maximum returned entries.
        """
        try:
            quoted_id = self._process_id(process_id)
            seconds = self._as_float(wait, 0, 300, "wait")
            return self._render(
                await self._request(
                    "GET",
                    f"/execute/{quoted_id}/status",
                    params={
                        "wait": seconds,
                        "offset": self._as_int(offset, 0, MAX_LINE_NUMBER, "offset"),
                        "tail": self._as_int(tail, 1, 1000, "tail"),
                    },
                    timeout=max(self.valves.REQUEST_TIMEOUT_SECONDS, seconds + 15),
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def dev_kill_process(self, process_id: str, force: bool = False) -> str:
        """
        Stop a process previously started by dev_run_command.
        :param process_id: Open Terminal process ID.
        :param force: Forceful termination when true.
        """
        try:
            quoted_id = self._process_id(process_id)
            return self._render(
                await self._request(
                    "DELETE",
                    f"/execute/{quoted_id}",
                    params={"force": str(self._as_bool(force)).lower()},
                )
            )
        except Exception as exc:
            return self._handle_error(exc)
