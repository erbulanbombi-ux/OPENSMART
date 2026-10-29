import ast
import asyncio
import ipaddress
import json
import logging
import operator
import os
import re
import socket
import tempfile
import threading
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for current information, news, documentation, or facts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Precise search query."}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": "Evaluate restricted Python for arithmetic, basic data analysis, and string manipulation.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Restricted Python statements or expressions."}},
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": "Read or write a persistent key-value note for this chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write"]},
                    "key": {"type": "string", "description": "Memory key or category."},
                    "value": {"type": "string", "description": "Value to store; required for write."},
                },
                "required": ["action", "key"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_goal",
            "description": "Create or manage long-term goals with desired results, milestones, status, and progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "get", "report", "advance", "pause", "resume", "complete"]},
                    "title": {"type": "string", "description": "Goal title; required for create."},
                    "details": {"type": "string", "description": "Optional description for a new goal."},
                    "desired_result": {"type": "string", "description": "Specific, observable outcome the user wants."},
                    "milestones": {"type": "array", "items": {"type": "string"}, "description": "Ordered steps toward the desired result."},
                    "deadline": {"type": "string", "description": "Optional ISO date or datetime deadline."},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "goal_id": {"type": "string", "description": "Goal ID for report, advance, get or complete."},
                    "report": {"type": "string", "description": "Progress update to append to the goal."},
                    "milestone_id": {"type": "string", "description": "Milestone ID to complete; defaults to the current milestone."},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_notes",
            "description": "Create, search, list, view, or edit this chat's structured notes with tags and dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "search", "get", "edit"]},
                    "text": {"type": "string", "description": "Note text for create or edit."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "List of tags/topics."},
                    "date": {"type": "string", "description": "Optional date associated with note (e.g. YYYY-MM-DD)."},
                    "query": {"type": "string", "description": "Keyword or tag search."},
                    "note_id": {"type": "string", "description": "Note ID for get or edit."},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_reminder",
            "description": "Create, list, or complete a pending reminder for this chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "complete"]},
                    "text": {"type": "string", "description": "Reminder text."},
                    "due_at": {"type": "string", "description": "Optional ISO date or datetime."},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "reminder_id": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local date and time with timezone.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url_content",
            "description": "Download a public web page and return its cleaned readable text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public HTTP or HTTPS URL."}
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a basic arithmetic expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic expression, for example (12 + 8) / 4.",
                    }
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    },
]
TOOL_SCHEMAS = TOOLS

_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}
_COMPARISON_OPERATORS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}
_SAFE_METHODS = {
    "capitalize", "casefold", "count", "endswith", "find", "index", "join",
    "lower", "lstrip", "replace", "rstrip", "split", "startswith", "strip",
    "swapcase", "title", "upper",
}
_MAX_COLLECTION_ITEMS = 10_000
_MAX_OUTPUT_CHARS = 20_000
_MEMORY_LOCK = threading.RLock()
_MEMORY_PATH = Path(
    os.getenv("OPENSMART_MEMORY_PATH", str(Path(__file__).with_name("memory.json")))
)
_MAX_PAGE_BYTES = 2_000_000
_MAX_PAGE_CHARS = 12_000


class _ReadableHTML(HTMLParser):
    _ignored = {"script", "style", "noscript", "svg", "header", "footer", "nav", "aside"}
    _breaks = {"br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._ignored:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._breaks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._breaks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


async def _validate_public_url(url: str) -> None:
    if len(url) > 2048:
        raise ValueError("URL is too long.")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP and HTTPS URLs are allowed.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise ValueError("The URL host could not be resolved.") from error
    if not addresses or any(
        not ipaddress.ip_address(address[4][0].split("%")[0]).is_global
        for address in addresses
    ):
        raise ValueError("Private and local network addresses are not allowed.")


async def fetch_url_content(url: str) -> dict[str, str]:
    """Fetch a public text page without following redirects or reading large bodies."""
    await _validate_public_url(url)
    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "OpenSmartBot/1.0"},
    ) as client:
        async with client.stream("GET", url) as response:
            if 300 <= response.status_code < 400:
                return {"error": "The page redirected; redirects are disabled for safety."}
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            is_html = "html" in content_type or "xhtml" in content_type
            if not is_html and "text/plain" not in content_type:
                return {"error": "The URL did not return an HTML or plain-text page."}

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_PAGE_BYTES:
                    return {"error": "The page is too large to process safely."}
            text = bytes(body).decode(response.encoding or "utf-8", errors="replace")

    if is_html:
        parser = _ReadableHTML()
        parser.feed(text)
        text = "".join(parser.parts)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return {"url": url, "text": cleaned[:_MAX_PAGE_CHARS] or "No readable text found."}


def _calculate(expression: str) -> int | float:
    if not expression or len(expression) > 256:
        raise ValueError("Expression must contain between 1 and 256 characters.")
    tree = ast.parse(expression, mode="eval")

    def evaluate(node: ast.expr) -> int | float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            result = _UNARY_OPERATORS[type(node.op)](evaluate(node.operand))
            if isinstance(result, bool):
                raise ValueError("Boolean values are not valid arithmetic results.")
            return result
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent must be between -10 and 10.")
            result = _BINARY_OPERATORS[type(node.op)](left, right)
            if type(result) not in (int, float) or abs(result) > 1e100:
                raise ValueError("Result is outside the supported range.")
            return result
        raise ValueError("Only basic arithmetic is supported.")

    return evaluate(tree.body)


class _RestrictedPython:
    """Evaluate a small expression language without exposing Python execution or imports."""

    def __init__(self) -> None:
        self.variables: dict[str, Any] = {}
        self.output: list[str] = []
        self._nodes_seen = 0
        self.functions: dict[str, Callable[..., Any]] = {
            "abs": abs,
            "all": all,
            "any": any,
            "bool": bool,
            "enumerate": enumerate,
            "float": float,
            "int": int,
            "len": len,
            "max": max,
            "min": min,
            "range": self._safe_range,
            "round": round,
            "sorted": sorted,
            "str": str,
            "sum": sum,
            "zip": zip,
            "print": self._print,
        }

    @staticmethod
    def _safe_range(*args: int) -> range:
        result = range(*args)
        if len(result) > _MAX_COLLECTION_ITEMS:
            raise ValueError("range is limited to 10,000 items.")
        return result

    def _print(self, *values: Any, sep: str = " ", end: str = "\n") -> None:
        rendered = sep.join(str(value) for value in values) + end
        if sum(map(len, self.output)) + len(rendered) > _MAX_OUTPUT_CHARS:
            raise ValueError("Output exceeds 20,000 characters.")
        self.output.append(rendered)

    def _checked(self, value: Any) -> Any:
        if type(value) is int and value.bit_length() > 1024:
            raise ValueError("Integer result is too large.")
        if type(value) is float and (value != value or abs(value) > 1e100):
            raise ValueError("Numeric result is outside the supported range.")
        if isinstance(value, (str, list, tuple, dict, set)) and len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("Collections are limited to 10,000 items.")
        return value

    def evaluate(self, node: ast.AST) -> Any:
        self._nodes_seen += 1
        if self._nodes_seen > 300:
            raise ValueError("Code is too complex to evaluate.")

        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                raise ValueError("This literal type is not supported.")
            return self._checked(node.value)
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ValueError("Private names are not allowed.")
            if node.id in self.variables:
                return self.variables[node.id]
            if node.id in self.functions:
                return self.functions[node.id]
            raise ValueError(f"Unknown name: {node.id}")
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self.evaluate(item) for item in node.elts]
            if len(values) > _MAX_COLLECTION_ITEMS:
                raise ValueError("Collections are limited to 10,000 items.")
            if isinstance(node, ast.List):
                return values
            if isinstance(node, ast.Tuple):
                return tuple(values)
            return set(values)
        if isinstance(node, ast.Dict):
            if len(node.keys) > _MAX_COLLECTION_ITEMS:
                raise ValueError("Collections are limited to 10,000 items.")
            return {
                self.evaluate(key): self.evaluate(value)
                for key, value in zip(node.keys, node.values)
                if key is not None
            }
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left = self.evaluate(node.left)
            right = self.evaluate(node.right)
            if isinstance(node.op, ast.Pow) and (
                type(right) not in (int, float) or abs(right) > 100
            ):
                raise ValueError("Exponent magnitude is limited to 100.")
            if isinstance(node.op, ast.Mult):
                if isinstance(left, (str, list, tuple)) and type(right) is int:
                    if len(left) * abs(right) > _MAX_COLLECTION_ITEMS:
                        raise ValueError("Repeated values exceed the collection limit.")
                if isinstance(right, (str, list, tuple)) and type(left) is int:
                    if len(right) * abs(left) > _MAX_COLLECTION_ITEMS:
                        raise ValueError("Repeated values exceed the collection limit.")
            return self._checked(_BINARY_OPERATORS[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return self._checked(_UNARY_OPERATORS[type(node.op)](self.evaluate(node.operand)))
        if isinstance(node, ast.BoolOp):
            values = [self.evaluate(value) for value in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
            raise ValueError("This boolean operation is not supported.")
        if isinstance(node, ast.Compare):
            left = self.evaluate(node.left)
            for comparison, comparator in zip(node.ops, node.comparators):
                if type(comparison) not in _COMPARISON_OPERATORS:
                    raise ValueError("This comparison is not supported.")
                right = self.evaluate(comparator)
                if not _COMPARISON_OPERATORS[type(comparison)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self.evaluate(node.test) else node.orelse
            return self.evaluate(branch)
        if isinstance(node, ast.Subscript):
            value = self.evaluate(node.value)
            if isinstance(node.slice, ast.Slice):
                index = slice(
                    self.evaluate(node.slice.lower) if node.slice.lower else None,
                    self.evaluate(node.slice.upper) if node.slice.upper else None,
                    self.evaluate(node.slice.step) if node.slice.step else None,
                )
            else:
                index = self.evaluate(node.slice)
            return self._checked(value[index])
        if isinstance(node, ast.Call):
            arguments = [self.evaluate(argument) for argument in node.args]
            keywords = {item.arg: self.evaluate(item.value) for item in node.keywords}
            if isinstance(node.func, ast.Name):
                function = self.evaluate(node.func)
                if node.func.id == "print" and set(keywords) - {"sep", "end"}:
                    raise ValueError("print only supports sep and end keyword arguments.")
                result = function(*arguments, **keywords)
                return self._checked(result)
            if isinstance(node.func, ast.Attribute):
                if node.func.attr not in _SAFE_METHODS:
                    raise ValueError("This method is not allowed.")
                instance = self.evaluate(node.func.value)
                if not isinstance(instance, str):
                    raise ValueError("Only approved string methods are available.")
                result = getattr(instance, node.func.attr)(*arguments, **keywords)
                return self._checked(result)
            raise ValueError("This function call is not allowed.")
        raise ValueError(f"Unsupported Python syntax: {type(node).__name__}")

    def run(self, code: str) -> str:
        if not code.strip() or len(code) > 4000:
            raise ValueError("Code must contain between 1 and 4,000 characters.")
        tree = ast.parse(code, mode="exec")
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    raise ValueError("Only simple variable assignments are allowed.")
                name = statement.targets[0].id
                if name.startswith("_") or name in self.functions:
                    raise ValueError("This variable name is not allowed.")
                self.variables[name] = self._checked(self.evaluate(statement.value))
            elif isinstance(statement, ast.Expr):
                result = self.evaluate(statement.value)
                if result is not None and not callable(result):
                    self._print(result)
            else:
                raise ValueError("Only expressions and simple assignments are allowed; imports and control flow are disabled.")
        return "".join(self.output).strip() or "(no output)"


async def search_web(query: str) -> dict[str, Any]:
    try:
        query = query.strip()
        if not query or len(query) > 500:
            raise ValueError("Query must contain between 1 and 500 characters.")

        def search() -> list[dict[str, str]]:
            from ddgs import DDGS

            with DDGS(timeout=4.5) as client:
                results = client.text(query, max_results=5)
                return [
                    {
                        "title": str(result.get("title", ""))[:300],
                        "url": str(result.get("href", ""))[:1000],
                        "snippet": str(result.get("body", ""))[:1200],
                    }
                    for result in results
                ]

        results = await asyncio.wait_for(asyncio.to_thread(search), timeout=5)
        return {"results": results}
    except Exception as error:
        logger.exception("Web search failed")
        return {"error": f"Web search failed: {error}"}


async def execute_python(code: str) -> dict[str, str]:
    try:
        result = _RestrictedPython().run(code)
        return {"output": result[:_MAX_OUTPUT_CHARS]}
    except Exception as error:
        logger.exception("Restricted Python evaluation failed")
        return {"error": f"{type(error).__name__}: {error}"[:500]}


def _read_memory_file() -> dict[str, dict[str, str]]:
    if not _MEMORY_PATH.exists():
        return {}
    if _MEMORY_PATH.stat().st_size > 1_000_000:
        raise ValueError("Memory file exceeds the 1 MB limit.")
    with _MEMORY_PATH.open("r", encoding="utf-8") as memory_file:
        data = json.load(memory_file)
    if not isinstance(data, dict):
        raise ValueError("Memory file must contain a JSON object.")
    return data


def _write_memory_file(data: dict[str, dict[str, str]]) -> None:
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=_MEMORY_PATH.parent,
            prefix=f"{_MEMORY_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, _MEMORY_PATH)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


async def manage_memory(
    action: str,
    key: str,
    value: str | None = None,
    *,
    scope: str = "default",
) -> dict[str, Any]:
    try:
        key = key.strip()
        if not key or len(key) > 100:
            raise ValueError("Memory key must contain between 1 and 100 characters.")
        if action not in {"read", "write"}:
            raise ValueError("Action must be 'read' or 'write'.")
        if action == "write" and (
            not isinstance(value, str) or len(value) > 4000
        ):
            raise ValueError("Write requires a string value of at most 4,000 characters.")
        if not scope or len(scope) > 100:
            raise ValueError("Invalid memory scope.")

        def operate() -> dict[str, Any]:
            with _MEMORY_LOCK:
                data = _read_memory_file()
                notes = data.get(scope, {})
                if not isinstance(notes, dict):
                    raise ValueError("Memory scope data is invalid.")
                if action == "read":
                    return {"key": key, "value": notes.get(key)}
                if key not in notes and len(notes) >= 500:
                    raise ValueError("This chat has reached the 500-note limit.")
                notes[key] = value
                data[scope] = notes
                _write_memory_file(data)
                return {"key": key, "stored": True}

        return await asyncio.to_thread(operate)
    except Exception as error:
        logger.exception("Memory operation failed")
        return {"error": f"{type(error).__name__}: {error}"[:500]}


async def manage_notes(
    action: str,
    text: str | None = None,
    tags: list[str] | None = None,
    date: str | None = None,
    query: str | None = None,
    note_id: str | None = None,
    *,
    scope: str = "default",
) -> dict[str, Any]:
    try:
        if action not in {"create", "list", "search", "get", "edit"}:
            raise ValueError("Action must be create, list, search, get, or edit.")
        if not scope or len(scope) > 100:
            raise ValueError("Invalid memory scope.")
        if text is not None and (not text.strip() or len(text) > 4000):
            raise ValueError("Note text must contain 1 to 4,000 characters.")
        clean_tags = list(dict.fromkeys(tag.strip().lstrip("#")[:40] for tag in tags or [] if tag.strip()))
        if len(clean_tags) > 20:
            raise ValueError("A note can have at most 20 tags.")

        def operate() -> dict[str, Any]:
            with _MEMORY_LOCK:
                data = _read_memory_file()
                memory = data.setdefault(scope, {})
                raw_notes = memory.get("_opensmart_notes", "[]")
                notes = json.loads(raw_notes) if isinstance(raw_notes, str) else raw_notes
                if not isinstance(notes, list) or any(not isinstance(note, dict) for note in notes):
                    raise ValueError("Stored note data is invalid.")

                if action == "list":
                    return {"notes": notes}
                if action == "search":
                    search_text = (query or "").strip().casefold()
                    search_tags = {tag.strip().lstrip("#").casefold() for tag in (tags or [])}
                    matches = [
                        note for note in notes
                        if (not search_text or search_text in str(note.get("text", "")).casefold()
                            or search_text in str(note.get("id", "")).casefold()
                            or search_text in str(note.get("date", "")).casefold()
                            or any(search_text in tag.casefold() for tag in note.get("tags", [])))
                        and (not search_tags or search_tags.issubset(
                            {str(tag).casefold() for tag in note.get("tags", [])}
                        ))
                    ]
                    return {"notes": matches}
                if action == "create":
                    if not text:
                        raise ValueError("Creating a note requires text.")
                    if len(notes) >= 1000:
                        raise ValueError("This chat has reached the 1,000-note limit.")
                    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
                    note = {
                        "id": uuid.uuid4().hex[:8],
                        "text": text.strip(),
                        "tags": clean_tags,
                        "date": (date or "").strip() or datetime.now().astimezone().strftime("%Y-%m-%d"),
                        "created_at": now_iso,
                    }
                    notes.append(note)
                    result = {"created": note}
                else:
                    selected = next((note for note in notes if note.get("id") == note_id), None)
                    if selected is None:
                        raise ValueError("Note not found; provide a valid note ID.")
                    if action == "get":
                        return {"note": selected}
                    if not text:
                        raise ValueError("Editing a note requires replacement text.")
                    selected["text"] = text.strip()
                    if tags is not None:
                        selected["tags"] = clean_tags
                    if date is not None:
                        selected["date"] = date.strip()
                    selected["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    result = {"updated": selected}

                memory["_opensmart_notes"] = json.dumps(notes, ensure_ascii=False)
                _write_memory_file(data)
                return result

        return await asyncio.to_thread(operate)
    except Exception as error:
        logger.exception("Note operation failed")
        return {"error": f"{type(error).__name__}: {error}"[:500]}


async def search_forget_items(query: str, *, scope: str) -> list[dict[str, str]]:
    """Find structured notes, memories, and goals without deleting them."""
    normalized = query.strip().casefold()
    if not normalized:
        return []

    # Strip prefixes if query has them
    clean_query = normalized
    for prefix in ("note:", "memory:", "goal:", "reminder:"):
        if clean_query.startswith(prefix):
            clean_query = clean_query.removeprefix(prefix)
            break

    def search() -> list[dict[str, str]]:
        with _MEMORY_LOCK:
            data = _read_memory_file()
            memory = data.get(scope, {})
            if not isinstance(memory, dict):
                return []
            results: list[dict[str, str]] = []

            # 1. Structured notes
            raw_notes = memory.get("_opensmart_notes", "[]")
            notes = json.loads(raw_notes) if isinstance(raw_notes, str) else raw_notes
            for note in (notes if isinstance(notes, list) else []):
                if not isinstance(note, dict):
                    continue
                note_id = str(note.get("id", "")).casefold()
                note_text = str(note.get("text", ""))
                tags = [str(t) for t in note.get("tags", [])]
                date = str(note.get("date") or note.get("created_at", "")[:10])
                if (
                    clean_query == note_id
                    or clean_query in note_text.casefold()
                    or any(clean_query in t.casefold() for t in tags)
                    or clean_query in date
                ):
                    tag_str = f" (#{', #'.join(tags)})" if tags else ""
                    results.append({
                        "id": f"note:{note.get('id')}",
                        "kind": "Note",
                        "text": f"[{date}] {note_text[:140]}{tag_str}",
                    })

            # 2. Key-value memories
            for key, value in memory.items():
                if key.startswith("_") or not isinstance(value, str):
                    continue
                if clean_query in key.casefold() or clean_query in value.casefold():
                    results.append({
                        "id": f"memory:{key}",
                        "kind": "Memory",
                        "text": f"{key}: {value}"[:160],
                    })

            # 3. Goals
            raw_goals = memory.get("_opensmart_goals", "[]")
            goals = json.loads(raw_goals) if isinstance(raw_goals, str) else raw_goals
            for goal in (goals if isinstance(goals, list) else []):
                if not isinstance(goal, dict):
                    continue
                gid = str(goal.get("id", "")).casefold()
                gtitle = str(goal.get("title", ""))
                if clean_query == gid or clean_query in gtitle.casefold():
                    results.append({
                        "id": f"goal:{goal.get('id')}",
                        "kind": "Goal",
                        "text": f"\"{gtitle}\" (progress: {goal.get('progress_percent', 0)}%)",
                    })

            return results[:20]

    return await asyncio.to_thread(search)


async def forget_item(identifier: str, *, scope: str) -> bool:
    def remove() -> bool:
        with _MEMORY_LOCK:
            data = _read_memory_file()
            memory = data.get(scope, {})
            if not isinstance(memory, dict):
                return False
            raw_id = identifier.strip()

            if raw_id.startswith("memory:"):
                key = raw_id.removeprefix("memory:")
                if key not in memory or key.startswith("_"):
                    return False
                del memory[key]
            elif raw_id.startswith("note:"):
                note_id = raw_id.removeprefix("note:")
                raw_notes = memory.get("_opensmart_notes", "[]")
                notes = json.loads(raw_notes) if isinstance(raw_notes, str) else raw_notes
                remaining = [note for note in (notes if isinstance(notes, list) else []) if note.get("id") != note_id]
                if len(remaining) == len(notes):
                    return False
                memory["_opensmart_notes"] = json.dumps(remaining, ensure_ascii=False)
            elif raw_id.startswith("goal:"):
                goal_id = raw_id.removeprefix("goal:")
                raw_goals = memory.get("_opensmart_goals", "[]")
                goals = json.loads(raw_goals) if isinstance(raw_goals, str) else raw_goals
                remaining = [g for g in (goals if isinstance(goals, list) else []) if g.get("id") != goal_id]
                if len(remaining) == len(goals):
                    return False
                memory["_opensmart_goals"] = json.dumps(remaining, ensure_ascii=False)
            else:
                # Try matching bare note_id or key
                raw_notes = memory.get("_opensmart_notes", "[]")
                notes = json.loads(raw_notes) if isinstance(raw_notes, str) else raw_notes
                remaining_notes = [note for note in (notes if isinstance(notes, list) else []) if note.get("id") != raw_id]
                if len(remaining_notes) < len(notes):
                    memory["_opensmart_notes"] = json.dumps(remaining_notes, ensure_ascii=False)
                elif raw_id in memory and not raw_id.startswith("_"):
                    del memory[raw_id]
                else:
                    return False

            data[scope] = memory
            _write_memory_file(data)
            return True

    return await asyncio.to_thread(remove)


async def manage_reminder(
    action: str,
    text: str | None = None,
    due_at: str | None = None,
    priority: int = 3,
    reminder_id: str | None = None,
    *,
    scope: str = "default",
) -> dict[str, Any]:
    try:
        if action not in {"create", "list", "complete"}:
            raise ValueError("Action must be create, list, or complete.")
        if not 1 <= priority <= 5:
            raise ValueError("Priority must be between 1 and 5.")
        if due_at:
            datetime.fromisoformat(due_at.replace("Z", "+00:00"))

        def operate() -> dict[str, Any]:
            with _MEMORY_LOCK:
                data = _read_memory_file()
                memory = data.setdefault(scope, {})
                raw_reminders = memory.get("_opensmart_reminders", "[]")
                reminders = json.loads(raw_reminders) if isinstance(raw_reminders, str) else raw_reminders
                if not isinstance(reminders, list) or any(not isinstance(item, dict) for item in reminders):
                    raise ValueError("Stored reminder data is invalid.")
                if action == "list":
                    return {"reminders": [item for item in reminders if item.get("status") == "pending"]}
                if action == "create":
                    if not text or not text.strip() or len(text) > 1000:
                        raise ValueError("Creating a reminder requires text of at most 1,000 characters.")
                    reminder = {
                        "id": uuid.uuid4().hex[:10],
                        "text": text.strip(),
                        "due_at": due_at,
                        "priority": priority,
                        "status": "pending",
                        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                    reminders.append(reminder)
                    result = {"created": reminder}
                else:
                    selected = next((item for item in reminders if item.get("id") == reminder_id), None)
                    if selected is None:
                        raise ValueError("Reminder not found; provide a valid reminder ID.")
                    selected["status"] = "completed"
                    selected["completed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    result = {"completed": selected}
                memory["_opensmart_reminders"] = json.dumps(reminders, ensure_ascii=False)
                _write_memory_file(data)
                return result

        return await asyncio.to_thread(operate)
    except Exception as error:
        logger.exception("Reminder operation failed")
        return {"error": f"{type(error).__name__}: {error}"[:500]}


async def manage_goal(
    action: str,
    title: str | None = None,
    details: str | None = None,
    desired_result: str | None = None,
    milestones: list[str] | None = None,
    deadline: str | None = None,
    priority: int = 3,
    goal_id: str | None = None,
    report: str | None = None,
    milestone_id: str | None = None,
    *,
    scope: str = "default",
) -> dict[str, Any]:
    """Persist a chat-scoped goal and its progress reports in the existing memory file."""
    try:
        if action not in {"create", "list", "get", "report", "advance", "pause", "resume", "complete"}:
            raise ValueError("Action must be create, list, get, report, advance, pause, resume, or complete.")
        if not scope or len(scope) > 100:
            raise ValueError("Invalid memory scope.")
        if title is not None and len(title.strip()) > 120:
            raise ValueError("Goal title must be 120 characters or fewer.")
        if details is not None and len(details) > 1000:
            raise ValueError("Goal details must be 1,000 characters or fewer.")
        if desired_result is not None and len(desired_result) > 1000:
            raise ValueError("Desired result must be 1,000 characters or fewer.")
        if report is not None and len(report) > 2000:
            raise ValueError("Progress report must be 2,000 characters or fewer.")
        if not 1 <= priority <= 5:
            raise ValueError("Priority must be between 1 and 5.")
        if milestones is not None and (
            len(milestones) > 30
            or any(not isinstance(step, str) or not step.strip() or len(step) > 160 for step in milestones)
        ):
            raise ValueError("Milestones must contain 1 to 160 character titles, with at most 30 steps.")
        if deadline:
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))

        def operate() -> dict[str, Any]:
            with _MEMORY_LOCK:
                data = _read_memory_file()
                notes = data.get(scope, {})
                if not isinstance(notes, dict):
                    raise ValueError("Memory scope data is invalid.")
                raw_goals = notes.get("_opensmart_goals", "[]")
                goals = json.loads(raw_goals) if isinstance(raw_goals, str) else raw_goals
                if not isinstance(goals, list) or any(not isinstance(goal, dict) for goal in goals):
                    raise ValueError("Stored goal data is invalid.")

                for goal in goals:
                    if goal.get("status") == "active":
                        goal["status"] = "in_progress"
                    goal.setdefault("desired_result", goal.get("details", ""))
                    goal.setdefault("milestones", [])
                    goal.setdefault("current_milestone", None)
                    goal.setdefault("progress_percent", 0)
                    goal.setdefault("next_recommended_step", "Define the next step.")
                    goal.setdefault("priority", 3)
                    goal.setdefault("history", [])
                    for step in goal.get("milestones", []):
                        if isinstance(step, dict):
                            step.setdefault("status", "not_started")

                if action == "list":
                    return {"goals": goals}

                now = datetime.now().astimezone().isoformat(timespec="seconds")
                if action == "create":
                    if not title or not title.strip():
                        raise ValueError("Creating a goal requires a title.")
                    clean_result = (desired_result or details or "").strip()
                    if not clean_result or clean_result.casefold() == title.strip().casefold():
                        return {
                            "clarification_required": True,
                            "question": (
                                f"To make the goal \"{title.strip()}\" specific and achievable, let's clarify your desired result!\n\n"
                                "🎯 What specific, measurable outcome would you like to achieve?"
                            ),
                            "suggested_milestones": [
                                "1. Define baseline level and success criteria",
                                "2. Create a weekly action plan and schedule",
                                "3. Start execution and establish a steady habit",
                                "4. Review milestone progress and adjust approach",
                                "5. Achieve the target outcome and record results",
                            ],
                        }
                    active_count = sum(goal.get("status") in {"not_started", "in_progress"} for goal in goals)
                    if active_count >= 20:
                        raise ValueError("This chat already has 20 active goals.")
                    step_titles = milestones or [
                        "Define starting point and success criteria",
                        "Complete the first scheduled action step",
                        "Establish a regular practice habit",
                        "Review intermediate progress",
                        "Achieve target outcome",
                    ]
                    goal_steps = [
                        {"id": uuid.uuid4().hex[:8], "title": step.strip(), "status": "not_started"}
                        for step in step_titles if step.strip()
                    ]
                    if not goal_steps:
                        raise ValueError("A goal requires at least one milestone.")
                    goal_steps[0]["status"] = "in_progress"
                    goal = {
                        "id": uuid.uuid4().hex[:8],
                        "title": title.strip(),
                        "details": (details or "").strip(),
                        "desired_result": clean_result,
                        "milestones": goal_steps,
                        "current_milestone": goal_steps[0]["id"],
                        "progress_percent": 0,
                        "status": "not_started",
                        "next_recommended_step": goal_steps[0]["title"],
                        "priority": priority,
                        "deadline": deadline,
                        "created_at": now,
                        "reports": [],
                        "history": [],
                    }
                    goals.append(goal)
                    result: dict[str, Any] = {"created": goal}
                else:
                    target_goals = goals if action == "get" else [
                        goal for goal in goals if goal.get("status") in {"not_started", "in_progress", "paused"}
                    ]
                    selected = next(
                        (goal for goal in target_goals if goal_id and (goal.get("id") == goal_id or goal.get("id") == goal_id.removeprefix("goal:"))),
                        None,
                    )
                    if selected is None and title:
                        title_key = title.strip().casefold()
                        matches = [goal for goal in target_goals if goal.get("title", "").casefold() == title_key]
                        if len(matches) == 1:
                            selected = matches[0]
                    if selected is None and not goal_id and not title and len(target_goals) == 1:
                        selected = target_goals[0]
                    if selected is None:
                        raise ValueError("Specify goal ID or exact title.")

                    if action == "get":
                        return {"goal": selected}

                    if action == "report":
                        if not report or not report.strip():
                            raise ValueError("Recording progress requires a report.")
                        reports = selected.setdefault("reports", [])
                        reports.append({"at": now, "text": report.strip()})
                        selected["reports"] = reports[-20:]
                        if selected.get("status") == "not_started":
                            selected["status"] = "in_progress"
                        result = {"updated": selected}
                    elif action == "advance":
                        steps = selected.get("milestones", [])
                        current = next(
                            (step for step in steps if step.get("id") == (milestone_id or selected.get("current_milestone"))),
                            None,
                        )
                        if current is None:
                            raise ValueError("Current milestone not found for this goal.")
                        current["status"] = "completed"
                        current["completed_at"] = now

                        # Record to history
                        history = selected.setdefault("history", [])
                        history.append({
                            "milestone_id": current["id"],
                            "title": current["title"],
                            "completed_at": now,
                        })
                        selected["history"] = history[-30:]

                        next_step = next((step for step in steps if step.get("status") == "not_started"), None)
                        completed_count = sum(step.get("status") == "completed" for step in steps)
                        selected["progress_percent"] = round(100 * completed_count / len(steps))
                        selected["status"] = "in_progress"
                        if next_step:
                            next_step["status"] = "in_progress"
                            selected["current_milestone"] = next_step["id"]
                            selected["next_recommended_step"] = next_step["title"]
                        else:
                            selected["status"] = "completed"
                            selected["progress_percent"] = 100
                            selected["current_milestone"] = None
                            selected["next_recommended_step"] = "Goal fully achieved! Congratulations!"
                            selected["completed_at"] = now
                        result = {"updated": selected}
                    elif action == "pause":
                        selected["status"] = "paused"
                        result = {"updated": selected}
                    elif action == "resume":
                        selected["status"] = "in_progress"
                        result = {"updated": selected}
                    else:
                        selected["status"] = "completed"
                        selected["progress_percent"] = 100
                        history = selected.setdefault("history", [])
                        for step in selected.get("milestones", []):
                            if step.get("status") != "completed":
                                step["status"] = "completed"
                                step["completed_at"] = now
                                history.append({
                                    "milestone_id": step.get("id"),
                                    "title": step.get("title", ""),
                                    "completed_at": now,
                                })
                        selected["history"] = history[-30:]
                        selected["current_milestone"] = None
                        selected["next_recommended_step"] = "Goal completed successfully!"
                        selected["completed_at"] = now
                        result = {"completed": selected}

                notes["_opensmart_goals"] = json.dumps(goals, ensure_ascii=False)
                data[scope] = notes
                _write_memory_file(data)
                return result

        return await asyncio.to_thread(operate)
    except Exception as error:
        logger.exception("Goal operation failed")
        return {"error": f"{type(error).__name__}: {error}"[:500]}


async def list_active_goal_chats() -> list[tuple[int, list[dict[str, Any]]]]:
    """Return active goal summaries for numeric Telegram chat scopes."""
    def read_active() -> list[tuple[int, list[dict[str, Any]]]]:
        with _MEMORY_LOCK:
            data = _read_memory_file()
            result: list[tuple[int, list[dict[str, Any]]]] = []
            for scope, notes in data.items():
                try:
                    chat_id = int(scope)
                except (TypeError, ValueError):
                    continue
                if not isinstance(notes, dict):
                    continue
                raw_goals = notes.get("_opensmart_goals", "[]")
                try:
                    goals = json.loads(raw_goals) if isinstance(raw_goals, str) else raw_goals
                except json.JSONDecodeError:
                    logger.exception("Invalid stored goal JSON for chat %s", chat_id)
                    continue
                if isinstance(goals, list):
                    active = [
                        {"id": goal.get("id"), "title": goal.get("title", "")}
                        for goal in goals
                        if isinstance(goal, dict)
                        and goal.get("status") in {"active", "not_started", "in_progress"}
                    ]
                    if active:
                        result.append((chat_id, active))
            return result

    try:
        return await asyncio.to_thread(read_active)
    except Exception:
        logger.exception("Could not read active goal chats")
        return []


async def get_daily_briefing(scope: str) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        with _MEMORY_LOCK:
            data = _read_memory_file()
            memory = data.get(scope, {})
            if not isinstance(memory, dict):
                return {"goals": [], "reminders": [], "notes": []}

            def records(key: str) -> list[dict[str, Any]]:
                raw = memory.get(key, "[]")
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                return parsed if isinstance(parsed, list) else []

            goals = [goal for goal in records("_opensmart_goals") if isinstance(goal, dict)
                     and goal.get("status") in {"active", "not_started", "in_progress"}]
            reminders = [item for item in records("_opensmart_reminders") if isinstance(item, dict)
                         and item.get("status") == "pending"]
            notes = [note for note in records("_opensmart_notes") if isinstance(note, dict)]
            goals.sort(key=lambda goal: (
                goal.get("deadline") or "9999",
                -int(goal.get("priority", 3)),
            ))
            reminders.sort(key=lambda item: (
                item.get("due_at") or "9999",
                -int(item.get("priority", 3)),
            ))
            relevant_terms = " ".join(
                f"{goal.get('title', '')} {goal.get('desired_result', '')}" for goal in goals
            ).casefold()
            relevant_notes = [
                note for note in notes
                if any(term and term in relevant_terms for term in str(note.get("text", "")).casefold().split())
            ]
            if not relevant_notes:
                relevant_notes = notes[-3:]
            candidates = [
                {
                    "due_at": goal.get("deadline"),
                    "priority": int(goal.get("priority", 3)),
                    "action": f"🎯 Goal \"{goal.get('title', 'Goal')}\" — {goal.get('next_recommended_step', 'take next step')}",
                }
                for goal in goals
            ] + [
                {
                    "due_at": item.get("due_at"),
                    "priority": int(item.get("priority", 3)),
                    "action": f"⏰ Reminder: {item.get('text', 'Reminder')}",
                }
                for item in reminders
            ]
            candidates.sort(key=lambda item: (item.get("due_at") or "9999", -item["priority"]))
            first_action = candidates[0]["action"] if candidates else None
            return {
                "goals": goals[:10],
                "reminders": reminders[:10],
                "notes": relevant_notes[-3:],
                "first_action": first_action,
            }

    try:
        return await asyncio.to_thread(build)
    except Exception as error:
        logger.exception("Daily briefing aggregation failed")
        return {"error": f"{type(error).__name__}: {error}"[:500], "goals": [], "reminders": [], "notes": []}


async def list_daily_briefing_chats() -> list[tuple[int, dict[str, Any]]]:
    def get_scopes() -> list[str]:
        with _MEMORY_LOCK:
            return [scope for scope in _read_memory_file() if scope.isdigit()]

    try:
        scopes = await asyncio.to_thread(get_scopes)
        results = []
        for scope in scopes:
            briefing = await get_daily_briefing(scope)
            if briefing.get("goals") or briefing.get("reminders") or briefing.get("notes"):
                results.append((int(scope), briefing))
        return results
    except Exception:
        logger.exception("Could not list chats for daily briefing")
        return []


async def get_current_time() -> dict[str, str]:
    return {"datetime": datetime.now().astimezone().isoformat(timespec="seconds")}


async def calculate(expression: str) -> dict[str, int | float]:
    return {"result": _calculate(expression)}


_TOOL_FUNCTIONS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "search_web": search_web,
    "fetch_url_content": fetch_url_content,
    "execute_python": execute_python,
    "manage_memory": manage_memory,
    "manage_goal": manage_goal,
    "manage_notes": manage_notes,
    "manage_reminder": manage_reminder,
    "get_current_time": get_current_time,
    "calculate": calculate,
}


async def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    scope: str = "default",
) -> dict[str, Any]:
    try:
        function = _TOOL_FUNCTIONS.get(name)
        if function is None:
            raise ValueError(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        if name in {"manage_memory", "manage_goal", "manage_notes", "manage_reminder"}:
            result = await function(**arguments, scope=scope)
        else:
            result = await function(**arguments)
        if not isinstance(result, dict):
            raise TypeError("Tool returned an unsupported result type.")
        return result
    except Exception as error:
        logger.exception("Tool execution failed: %s", name)
        return {"error": str(error)}


async def execute_tool_call(name: str, arguments: str, *, scope: str = "default") -> str:
    try:
        parsed_arguments = json.loads(arguments or "{}")
        result = await execute_tool(name, parsed_arguments, scope=scope)
    except Exception as error:
        logger.exception("Invalid tool call: %s", name)
        result = {"error": str(error)}
    return json.dumps(result, ensure_ascii=False)


def describe_tools() -> str:
    return "\n".join(
        f"{schema['function']['name']} — {schema['function']['description']}"
        for schema in TOOLS
    )