"""Which file declares which type, by package (Java/Kotlin) or namespace (C#), for resolving imports to files.

A file links to another when it imports that file's type by its full name, or when it can see the type's package or
namespace (a wildcard import, `using`, its own package) and names the type in its code."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record|struct|object)\s+([A-Z_]\w*)")
WORD = re.compile(r"\b[A-Za-z_]\w*")


@dataclass
class TypeIndex:
    by_pkg: dict[str, dict[str, int]] = field(default_factory=dict)   # package -> {type: file index}
    fq: dict[str, int] = field(default_factory=dict)                  # package.Type -> file index
    pkg_of: dict[int, str] = field(default_factory=dict)              # file index -> its package
    types_of: dict[int, list[str]] = field(default_factory=dict)      # file index -> types it declares
    consts: dict[str, str] = field(default_factory=dict)              # Type.NAME -> string constant (route paths)

    def add_consts(self, types: list[str], values: dict[str, str]) -> None:
        for t in types:
            for name, v in values.items():
                self.consts.setdefault(f"{t}.{name}", v)

    def add(self, idx: int, pkg: str, types: list[str]) -> None:
        self.pkg_of[idx] = pkg
        self.types_of[idx] = types
        for t in types:
            self.by_pkg.setdefault(pkg, {}).setdefault(t, idx)
            self.fq.setdefault(f"{pkg}.{t}" if pkg else t, idx)

    def find(self, name: str, pkgs: list[str]) -> int | None:
        """The file declaring a type named `name` in the first of `pkgs` that has one."""
        for p in pkgs:
            i = self.by_pkg.get(p, {}).get(name)
            if i is not None:
                return i
        return None

    def visible(self, code: str, own: int, pkgs: list[str]) -> list[int]:
        """Files declaring a type of one of `pkgs` that the code names."""
        names = set(WORD.findall(code))
        out = []
        for p in pkgs:
            for t, i in self.by_pkg.get(p, {}).items():
                if i != own and t in names:
                    out.append(i)
        return out


def declared_types(code: str) -> list[str]:
    return list(dict.fromkeys(TYPE_DECL.findall(code)))
