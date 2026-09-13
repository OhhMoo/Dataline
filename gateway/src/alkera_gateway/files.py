from pathlib import Path

from .config import GatewayConfig
from .records import Observation, ToolError


class Workspace:
    def __init__(self, config: GatewayConfig):
        self.root = config.workspace
        self.max_bytes = config.max_payload_bytes

    def path(self, name: str) -> Path:
        if name == "/workspace" or name.startswith("/workspace/"):
            name = name[len("/workspace") :].lstrip("/")
        candidate = self.root / name
        path = candidate.resolve()
        if not path.is_relative_to(self.root) or ".git" in path.relative_to(self.root).parts:
            raise ToolError("path_denied", "Path must stay inside workspace and outside .git")
        return path

    def name(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def read(self, path: str, observation: Observation):
        resolved = self.path(path)
        observation.targets = observation.read_targets = [self.name(resolved)]
        with resolved.open("rb") as stream:
            content = stream.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise ToolError("file_too_large", "File exceeds the configured read limit")
        return {"path": self.name(resolved), "content": content.decode("utf-8")}

    def write(self, path: str, content: str, observation: Observation):
        resolved = self.path(path)
        observation.op_class = "write"
        observation.targets = [self.name(resolved)]
        data = content.encode("utf-8")
        if len(data) > self.max_bytes:
            raise ToolError("file_too_large", "Content exceeds the configured write limit")
        if resolved.exists():
            observation.flags.append("overwrite")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(data)
        return {"path": self.name(resolved), "bytes_written": len(data)}

    def list(self, path: str, recursive: bool, observation: Observation):
        resolved = self.path(path)
        observation.targets = observation.read_targets = [self.name(resolved)]
        candidates = resolved.rglob("*") if recursive else resolved.iterdir()
        entries = []
        for item in sorted(candidates):
            if ".git" in item.relative_to(self.root).parts or item.is_symlink():
                continue
            if not item.resolve().is_relative_to(self.root):
                continue
            entries.append(
                {"path": self.name(item), "type": "directory" if item.is_dir() else "file"}
            )
            if len(entries) == 1001:
                break
        return {"entries": entries[:1000], "truncated": len(entries) > 1000}

    def search(self, query: str, limit: int, observation: Observation):
        docs = self.path("docs")
        observation.targets = observation.read_targets = ["docs"]
        matches = []
        for path in sorted(docs.rglob("*")):
            if path.suffix not in {".md", ".txt"} or not path.is_file():
                continue
            try:
                content = self.read(self.name(path), Observation())["content"]
            except (ToolError, UnicodeError):
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append({"path": self.name(path), "line": number, "text": line})
                    if len(matches) == limit:
                        return {"matches": matches, "limit_reached": True}
        return {"matches": matches, "limit_reached": False}
