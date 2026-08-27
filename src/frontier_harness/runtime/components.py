"""Immutable, live-switchable component generations for a Flourite run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..errors import FrontierError
from ..locking import RunLock
from ..util import atomic_write_text, canonical_json, sha256_bytes, utc_now

STEP_PROTOCOL = "flourite-step/v1"


@dataclass(frozen=True)
class ComponentBinding:
    """One immutable implementation selected at an activity boundary."""

    generation: int
    digest: str
    slot: str
    activated_at: str


class ComponentRegistry:
    """Atomic pointer from one durable run to replaceable implementation code."""

    REGISTRY_FILE = "components.json"
    COMPONENT_DIR = "components"
    RECEIPTS_FILE = "component-receipts.jsonl"

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.path = self.run_dir / self.REGISTRY_FILE
        self.component_dir = self.run_dir / self.COMPONENT_DIR
        self.lock = RunLock(self.component_dir / ".registry.lock")

    @staticmethod
    def current_source() -> Path:
        return Path(__file__).resolve().parents[1]

    @staticmethod
    def _package_dir(source: Path) -> Path:
        resolved = source.expanduser().resolve()
        candidates = (
            resolved,
            resolved / "frontier_harness",
            resolved / "src" / "frontier_harness",
        )
        for candidate in candidates:
            if candidate.name == "frontier_harness" and (candidate / "__init__.py").is_file():
                return candidate
        raise FrontierError(
            f"component source must contain the frontier_harness package: {resolved}"
        )

    @staticmethod
    def _files(package_dir: Path) -> list[Path]:
        return sorted(
            (
                path
                for path in package_dir.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            ),
            key=lambda path: path.relative_to(package_dir).as_posix(),
        )

    @classmethod
    def _digest(cls, package_dir: Path) -> str:
        material = bytearray()
        for path in cls._files(package_dir):
            relative = path.relative_to(package_dir).as_posix().encode("utf-8")
            data = path.read_bytes()
            material.extend(len(relative).to_bytes(4, "big"))
            material.extend(relative)
            material.extend(len(data).to_bytes(8, "big"))
            material.extend(data)
        return sha256_bytes(bytes(material))

    def _read(self) -> dict[str, Any]:
        try:
            decoded = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise FrontierError(f"invalid component registry: {self.path}") from exc
        if not isinstance(decoded, dict):
            raise FrontierError(f"invalid component registry: {self.path}")
        data: dict[str, Any] = decoded
        if data.get("protocol") != STEP_PROTOCOL or not isinstance(data.get("active"), dict):
            raise FrontierError(f"unsupported component registry: {self.path}")
        return data

    @staticmethod
    def _binding(data: dict[str, Any]) -> ComponentBinding:
        try:
            return ComponentBinding(
                generation=int(data["generation"]),
                digest=str(data["digest"]),
                slot=str(data["slot"]),
                activated_at=str(data["activated_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FrontierError("invalid component binding") from exc

    def active(self) -> ComponentBinding:
        return self._binding(self._read()["active"])

    def history(self) -> list[ComponentBinding]:
        data = self._read()
        return [self._binding(item) for item in data.get("history", [])]

    def slot_path(self, binding: ComponentBinding) -> Path:
        slot = (self.run_dir / binding.slot).resolve()
        try:
            slot.relative_to(self.component_dir.resolve())
        except ValueError as exc:
            raise FrontierError("component slot escapes its run") from exc
        if not (slot / "frontier_harness" / "__init__.py").is_file():
            raise FrontierError(f"component generation is missing: {binding.generation}")
        return slot

    def initialize(self, source: Path | None = None) -> ComponentBinding:
        if self.path.exists():
            return self.active()
        return self.bind(source or self.current_source())

    def bind(self, source: Path) -> ComponentBinding:
        package_dir = self._package_dir(source)
        digest = self._digest(package_dir)
        with self.lock:
            data = self._read() if self.path.exists() else None
            if data is not None:
                active = self._binding(data["active"])
                if active.digest == digest:
                    return active

            slot = self.component_dir / digest
            package_slot = slot / "frontier_harness"
            if not package_slot.exists():
                temporary = self.component_dir / f".{digest}.{os.getpid()}.tmp"
                shutil.rmtree(temporary, ignore_errors=True)
                temporary.mkdir(parents=True, exist_ok=False)
                shutil.copytree(
                    package_dir,
                    temporary / "frontier_harness",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )
                copied_digest = self._digest(temporary / "frontier_harness")
                if copied_digest != digest:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise FrontierError("component source changed while it was being captured")
                try:
                    temporary.replace(slot)
                except FileExistsError:
                    shutil.rmtree(temporary, ignore_errors=True)
            elif self._digest(package_slot) != digest:
                raise FrontierError(f"component snapshot digest mismatch: {slot}")

            self._validate(slot)
            previous = list(data.get("history", [])) if data is not None else []
            generation = max((int(item["generation"]) for item in previous), default=0) + 1
            binding = ComponentBinding(
                generation=generation,
                digest=digest,
                slot=str(slot.relative_to(self.run_dir)),
                activated_at=utc_now(),
            )
            history = [*previous, asdict(binding)]
            atomic_write_text(
                self.path,
                json.dumps(
                    {
                        "protocol": STEP_PROTOCOL,
                        "active": asdict(binding),
                        "history": history,
                    },
                    indent=2,
                    sort_keys=True,
                ),
            )
            return binding

    def rollback_if_current(self, failed: ComponentBinding) -> ComponentBinding | None:
        with self.lock:
            data = self._read()
            active = self._binding(data["active"])
            if active.generation != failed.generation:
                return None
            history = [self._binding(item) for item in data.get("history", [])]
            previous = next(
                (item for item in reversed(history[:-1]) if item.digest != failed.digest),
                None,
            )
            if previous is None:
                return None
            replacement = ComponentBinding(
                generation=max(item.generation for item in history) + 1,
                digest=previous.digest,
                slot=previous.slot,
                activated_at=utc_now(),
            )
            data["active"] = asdict(replacement)
            data["history"] = [*data.get("history", []), asdict(replacement)]
            atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True))
            return replacement

    def record_receipt(self, payload: dict[str, Any]) -> None:
        line = canonical_json({"timestamp": utc_now(), **payload}) + "\n"
        path = self.run_dir / self.RECEIPTS_FILE
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate(slot: Path) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(slot)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from frontier_harness.runtime.step_worker import STEP_PROTOCOL; "
                    f"assert STEP_PROTOCOL == {STEP_PROTOCOL!r}"
                ),
            ],
            cwd=slot,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip()
            raise FrontierError(f"component does not implement {STEP_PROTOCOL}: {detail}")
