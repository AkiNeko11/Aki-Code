"""Read inline Markdown links and report missing local targets."""

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import unquote


LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^\s)]+)\)")
EXTERNAL_PREFIXES = ("https://", "http://", "mailto:")


@dataclass(frozen=True)
class LinkResult:
    label: str
    target: str
    line: int
    exists: bool


def check_file(markdown_path: str | Path) -> list[LinkResult]:
    """Check file existence for each local inline link in a document."""
    source = Path(markdown_path)
    text = source.read_text(encoding="utf-8")
    results = []
    for match in LINK.finditer(text):
        label, target = match.groups()
        if target.startswith("#") or target.lower().startswith(EXTERNAL_PREFIXES):
            continue
        local_path = unquote(target.split("#", 1)[0])
        target_path = Path(local_path)
        results.append(LinkResult(
            label=label,
            target=target,
            line=text.count("\n", 0, match.start()) + 1,
            exists=target_path.is_file(),
        ))
    return results
