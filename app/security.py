from pathlib import Path
import re

SECRET_PATTERNS = (re.compile(r"(?i)(app[_-]?secret|access[_-]?token|authorization)\s*[:=]\s*['\"][^<*$]"),
                   re.compile(r"(?i)x-signature\s*[:=]\s*['\"][^<*$]"))


def scan_repository(root: str | Path) -> list[str]:
    findings = []
    for path in Path(root).rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", ".pytest_tmp", "__pycache__", "tests"} for part in path.parts): continue
        if path.name == ".env.example": continue
        try: text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError): continue
        if path.name == ".env": findings.append(str(path))
        if path.suffix == ".log": findings.append(str(path))
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            findings.append(str(path))
    return sorted(set(findings))
