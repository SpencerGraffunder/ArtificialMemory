"""Versioned prompt files (server/prompts/*.md).

Format: YAML-ish front-matter with `version:` and optional `description:`, then the
template body. Placeholders use {{name}} (not str.format, so JSON braces in few-shot
examples are safe). The loaded set's content hash is stored with extractions for
reproducibility.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class Prompt:
    name: str
    version: int
    body: str
    description: str = ""

    @property
    def ref(self) -> str:
        return f"{name_version(self.name, self.version)}"

    def render(self, **vars_: object) -> str:
        missing = set()

        def sub(m: re.Match) -> str:
            k = m.group(1)
            if k not in vars_:
                missing.add(k)
                return m.group(0)
            return str(vars_[k])

        out = _PLACEHOLDER.sub(sub, self.body)
        if missing:
            raise KeyError(f"prompt {self.name} missing vars: {sorted(missing)}")
        return out


def name_version(name: str, version: int) -> str:
    return f"{name}@v{version}"


class PromptSet:
    def __init__(self, prompts: dict[str, Prompt]):
        self._prompts = prompts

    def __getitem__(self, name: str) -> Prompt:
        return self._prompts[name]

    def get(self, name: str, default: Prompt | None = None) -> Prompt | None:
        return self._prompts.get(name, default)

    def refs(self) -> dict[str, str]:
        return {n: p.ref for n, p in self._prompts.items()}

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for name in sorted(self._prompts):
            p = self._prompts[name]
            h.update(f"{p.ref}\n{p.body}\n".encode())
        return h.hexdigest()[:16]

    @classmethod
    def load(cls, directory: str | Path) -> "PromptSet":
        d = Path(directory)
        prompts: dict[str, Prompt] = {}
        if not d.is_dir():
            return cls(prompts)
        for f in sorted(d.glob("*.md")):
            text = f.read_text()
            version, description, body = 1, "", text
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
            if m:
                fm = m.group(1)
                body = text[m.end():]
                vm = re.search(r"^version:\s*(\d+)", fm, flags=re.MULTILINE)
                if vm:
                    version = int(vm.group(1))
                dm = re.search(r"^description:\s*(.+)$", fm, flags=re.MULTILINE)
                if dm:
                    description = dm.group(1).strip()
            prompts[f.stem] = Prompt(name=f.stem, version=version, body=body.strip(),
                                     description=description)
        return cls(prompts)
