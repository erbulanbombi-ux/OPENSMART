import re
from dataclasses import dataclass

_THINK_TAG = re.compile(
    r"<think\b[^>]*>(.*?)(?:</think\s*>|$)", re.IGNORECASE | re.DOTALL
)
_CODE_BLOCK = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_LATEX_COMMANDS = {
    "times": "*",
    "cdot": "*",
    "div": "/",
    "le": "<=",
    "leq": "<=",
    "ge": ">=",
    "geq": ">=",
    "neq": "!=",
    "ne": "!=",
    "to": "->",
    "rightarrow": "->",
    "pm": "+/-",
    "infty": "infinity",
    "pi": "pi",
    "alpha": "alpha",
    "beta": "beta",
    "theta": "theta",
    "lambda": "lambda",
    "sum": "sum",
    "prod": "product",
    "approx": "approximately",
}
_LATEX_COMMAND = re.compile(r"\\([A-Za-z]+)")
_LATEX_DELIMITER = re.compile(r"\\(?:\(|\)|\[|\])")
_LATEX_ENVIRONMENT = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
_LATEX_UNWRAP = re.compile(
    r"\\(?:text|mathrm|mathbf|mathit|operatorname|overline|underline|overbrace|"
    r"underbrace|vec|hat|bar|boldsymbol|mathcal|mathbb)\s*\{([^{}]*)\}"
)
_LATEX_FRACTION = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_LATEX_SQRT = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_LATEX_SPACING = re.compile(r"\\(?:[,;:!]|quad\b|qquad\b|enspace\b|displaystyle\b|left\b|right\b)")
_DOLLAR_MATH = re.compile(r"(?<!\\)(\${1,2})([^$\n]+?)\1")
_INLINE_MARKDOWN = re.compile(
    r"(?P<code>`[^`\n]*`)|(?P<bold>\*\*.+?\*\*)|(?P<bold_alt>__.+?__)|"
    r"(?P<strike>~~.+?~~)|(?P<italic>\*(?!\*)[^*\n]+\*)|"
    r"(?P<italic_alt>_(?!_)[^_\n]+_)|(?P<link>\[[^\]\n]+\]\(https?://[^)\s]+\))",
    re.DOTALL,
)
_MARKDOWN_V2_SPECIAL = re.compile(r"([_\*\[\]()~`>#+\-=|{}.!\\])")


@dataclass(frozen=True)
class ParsedAgentOutput:
    answer: str
    thoughts: str


def parse_agent_output(text: str) -> ParsedAgentOutput:
    thoughts = "\n".join(match.group(1).strip() for match in _THINK_TAG.finditer(text))
    answer = _THINK_TAG.sub("", text).strip()
    return ParsedAgentOutput(answer=answer, thoughts=thoughts)


def sanitize_latex(text: str) -> str:
    """Convert common LaTeX notation to plain text without modifying fenced code."""
    result: list[str] = []
    cursor = 0
    for match in _CODE_BLOCK.finditer(text):
        result.append(_sanitize_latex_text(text[cursor:match.start()]))
        result.append(match.group())
        cursor = match.end()
    result.append(_sanitize_latex_text(text[cursor:]))
    return "".join(result)


def _sanitize_latex_text(text: str) -> str:
    text = _DOLLAR_MATH.sub(r"\2", text)
    text = _LATEX_DELIMITER.sub("", text)
    text = _LATEX_ENVIRONMENT.sub("", text)
    for _ in range(8):
        updated = _LATEX_FRACTION.sub(r"(\1 / \2)", text)
        updated = _LATEX_SQRT.sub(r"sqrt(\1)", updated)
        updated = _LATEX_UNWRAP.sub(r"\1", updated)
        if updated == text:
            break
        text = updated
    text = _LATEX_SPACING.sub(" ", text)
    text = _LATEX_COMMAND.sub(lambda match: _LATEX_COMMANDS.get(match.group(1), match.group(1)), text)
    for symbol, replacement in (("×", "*"), ("÷", "/"), ("≤", "<="), ("≥", ">="), ("≠", "!=")):
        text = text.replace(symbol, replacement)
    return text.replace("{", "").replace("}", "")


def format_telegram_messages(text: str, max_length: int = 4000) -> list[str]:
    """Format Markdown as Telegram MarkdownV2; never include internal reasoning."""
    answer = sanitize_latex(parse_agent_output(text).answer) or "I couldn't produce a text response."
    raw_limit = max(1, max_length // 2)
    chunks: list[str] = []

    def append_text_chunks(source: str) -> None:
        while source:
            if len(source) <= raw_limit:
                chunks.append(_format_inline_markdown(source))
                return
            split_at = source.rfind("\n", 0, raw_limit)
            if split_at <= 0:
                split_at = raw_limit
            part, source = source[:split_at], source[split_at:].lstrip("\n")
            chunks.append(_format_inline_markdown(part))

    cursor = 0
    for match in _CODE_BLOCK.finditer(answer):
        append_text_chunks(answer[cursor:match.start()])
        language = re.sub(r"[^A-Za-z0-9_+#.-]", "", match.group(1))
        code = match.group(2)
        code_limit = max(1, max_length - len(language) - 10)
        while code:
            part, code = code[:code_limit], code[code_limit:]
            escaped_code = part.replace("\\", "\\\\").replace("`", "\\`")
            chunks.append(f"```{language}\n{escaped_code}```")
        cursor = match.end()
    append_text_chunks(answer[cursor:])

    merged: list[str] = []
    for chunk in chunks:
        if merged and len(merged[-1]) + len(chunk) <= max_length:
            merged[-1] += chunk
        else:
            merged.append(chunk)
    return merged


def _escape_markdown_v2(text: str) -> str:
    return _MARKDOWN_V2_SPECIAL.sub(r"\\\1", text)


def _format_inline_markdown(text: str) -> str:
    result: list[str] = []
    cursor = 0
    for match in _INLINE_MARKDOWN.finditer(text):
        result.append(_escape_markdown_v2(text[cursor:match.start()]))
        token = match.group()
        kind = match.lastgroup
        if kind == "code":
            code = token[1:-1].replace("\\", "\\\\").replace("`", "\\`")
            result.append(f"`{code}`")
        elif kind in {"bold", "bold_alt"}:
            result.append(f"*{_escape_markdown_v2(token[2:-2])}*")
        elif kind == "strike":
            result.append(f"~{_escape_markdown_v2(token[2:-2])}~")
        elif kind in {"italic", "italic_alt"}:
            result.append(f"_{_escape_markdown_v2(token[1:-1])}_")
        elif kind == "link":
            label, url = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", token).groups()
            safe_url = url.replace("\\", "\\\\").replace(")", "\\)")
            result.append(f"[{_escape_markdown_v2(label)}]({safe_url})")
        cursor = match.end()
    result.append(_escape_markdown_v2(text[cursor:]))
    return "".join(result)