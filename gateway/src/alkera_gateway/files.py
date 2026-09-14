import codecs
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

    def read(self, path: str, observation: Observation, encoding: str = "utf-8"):
        resolved = self.path(path)
        observation.targets = observation.read_targets = [self.name(resolved)]
        with resolved.open("rb") as stream:
            content = stream.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise ToolError("file_too_large", "File exceeds the configured read limit")
        return {
            "path": self.name(resolved),
            "content": self._decode(content, encoding),
            "encoding": encoding,
        }

    @staticmethod
    def _decode(content: bytes, encoding: str) -> str:
        """Decode strictly, and report the offending byte when it fails.

        Deliberately NOT errors="replace". Silently substituting U+FFFD would
        hand the agent plausible-looking wrong data with no signal, which is the
        worst outcome for a benchmark - it converts a findable defect into an
        unfindable one. A real toolchain raises here, so we raise here, and we
        say where, because that is the evidence the diagnosis needs.
        """
        try:
            return content.decode(encoding)
        except LookupError:
            raise ToolError("unknown_encoding", f"No codec named {encoding!r}") from None
        except UnicodeDecodeError as e:
            raise ToolError(
                "decode_error",
                f"Byte 0x{content[e.start]:02x} at offset {e.start} is not valid "
                f"{encoding}. Re-read with the correct 'encoding' if you know it.",
            ) from None

    def write(self, path: str, content: str, observation: Observation, encoding: str = "utf-8"):
        resolved = self.path(path)
        observation.op_class = "write"
        observation.targets = [self.name(resolved)]
        try:
            data = content.encode(encoding)
        except LookupError:
            raise ToolError("unknown_encoding", f"No codec named {encoding!r}") from None
        except UnicodeEncodeError as e:
            raise ToolError(
                "encode_error",
                f"{content[e.start : e.start + 1]!r} at offset {e.start} cannot be "
                f"represented in {encoding}",
            ) from None
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

    def search(self, query: str, limit: int, observation: Observation, encoding: str = "utf-8"):
        """Search the docs tree, and account for every document it could not read.

        The old body swallowed ToolError and UnicodeError with a bare `continue`
        and still returned limit_reached=false - an affirmative claim that the
        search was complete. A cp1252 export and an oversized runbook, both
        containing the query, vanished with nothing saying so. That is the same
        failure class `_decode` exists to close, one method away: it does not
        merely omit data, it hides the omission. Several archetypes plant the
        defect *in* the document, so the surface that quietly skips the corrupted
        file removes the planted defect from the agent's reach.

        So: a document is either searched, or named in `skipped` with the reason
        it could not be. `encoding` is symmetric with fs_read - diagnose the
        codec, then search the document in it.
        """
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise ToolError("unknown_encoding", f"No codec named {encoding!r}") from None
        docs = self.path("docs")
        observation.targets = observation.read_targets = ["docs"]
        matches, skipped = [], []
        for path in sorted(docs.rglob("*")):
            if path.suffix not in {".md", ".txt"} or not path.is_file():
                continue
            name = self.name(path)
            try:
                content = self.read(name, Observation(), encoding)["content"]
            except ToolError as exc:
                skipped.append({"path": name, "code": exc.code, "reason": str(exc)})
                continue
            except OSError as exc:
                skipped.append({"path": name, "code": "read_failed", "reason": str(exc)})
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append({"path": name, "line": number, "text": line})
                    if len(matches) == limit:
                        return self._search_result(matches, True, skipped, encoding)
        return self._search_result(matches, False, skipped, encoding)

    @staticmethod
    def _search_result(matches, limit_reached, skipped, encoding):
        return {
            "matches": matches,
            "limit_reached": limit_reached,
            # Never empty by accident: an empty list is the claim that nothing
            # was skipped, and it is only ever written here alongside the loop
            # that would have filled it.
            "skipped": skipped,
            "encoding": encoding,
        }
