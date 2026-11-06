"""Owned run archives and conservative raw-cache retention.

Research runs are permanent. Live cleanup removes only hash-verified raw/working copies
registered by this module in old completed live runs. Original inputs, manifests,
configuration, compact audit artifacts, pending runs and the latest live result
are never deleted. There is deliberately no recursive delete operation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import uuid4

from .data import DataError, load_json


ROOT_MARKER = ".stocking-archive.json"
RUN_MANIFEST = ".stocking-run.json"
_RUN_ID = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{12}")
_LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DataError("Retention time must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return _now(parsed)
    except (TypeError, ValueError) as exc:
        raise DataError("Invalid archive timestamp") from exc


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _checked(path, *, within=None, exists=False):
    """Verify the final absolute target and every existing parent before writes/deletes."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    for component in [*reversed(absolute.parents), absolute]:
        try:
            if _linked(component):
                raise DataError(f"Archive paths cannot traverse links/reparse points: {component}")
        except FileNotFoundError:
            continue
    resolved = absolute.resolve(strict=exists)
    if resolved != absolute:
        raise DataError("Archive target does not resolve to its declared absolute path")
    if within is not None:
        root = Path(within)
        if resolved == root or not resolved.is_relative_to(root):
            raise DataError("Archive target escapes the intended root")
    return resolved


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _new_json(path, value, root):
    target = _checked(path, within=root)
    with target.open("xb") as stream:
        stream.write(_json_bytes(value))


def _replace_json(path, value, root):
    target = _checked(path, within=root, exists=True)
    temporary = _checked(target.with_name(target.name + "." + uuid4().hex + ".tmp"), within=root)
    with temporary.open("xb") as stream:
        stream.write(_json_bytes(value))
    # Recheck containment immediately before replacing a manifest, not any user file.
    _checked(target, within=root, exists=True)
    os.replace(temporary, target)


def _root(root, create=False):
    root = _checked(root)
    if root == Path(root.anchor):
        raise DataError("Filesystem root cannot be a managed archive")
    marker = root / ROOT_MARKER
    if create:
        root.mkdir(parents=True, exist_ok=True)
        _checked(root, exists=True)
        if not marker.exists():
            _new_json(marker, {"schema_version": 1, "kind": "stocking_owned_archive",
                               "archive_id": uuid4().hex, "runs": []}, root)
    _checked(marker, within=root, exists=True)
    payload = load_json(marker)
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or payload.get("kind") != "stocking_owned_archive"
            or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("archive_id", "")))
            or not isinstance(payload.get("runs"), list)
            or any(not isinstance(run, str) or not _RUN_ID.fullmatch(run) for run in payload["runs"])
            or len(payload["runs"]) != len(set(payload["runs"]))):
        raise DataError("Invalid managed archive registry; refusing cleanup")
    return root, payload


def _run(run_dir):
    run_dir = _checked(run_dir, exists=True)
    root, registry = _root(run_dir.parent)
    if run_dir.name not in registry["runs"]:
        raise DataError("Unregistered run directory")
    manifest_path = _checked(run_dir / RUN_MANIFEST, within=root, exists=True)
    manifest = load_json(manifest_path)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("archive_id") != registry["archive_id"]
            or manifest.get("run_id") != run_dir.name
            or manifest.get("mode") not in ("research", "live")
            or manifest.get("status") not in ("pending", "complete")
            or not isinstance(manifest.get("files"), list)):
        raise DataError("Invalid run manifest")
    return root, run_dir, manifest


def _sources(mapping):
    if not isinstance(mapping, dict):
        raise DataError("Archive sources must map short labels to individual file paths")
    result = []
    for label, source in mapping.items():
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise DataError("Archive source labels require 1-64 letters, digits, underscores or hyphens")
        source = _checked(source, exists=True)
        if not stat.S_ISREG(source.stat().st_mode):
            raise DataError("Only explicit regular files can be archived; directories are not recursive inputs")
        result.append((label, source))
    return result


def _copy(run_dir, manifest, label, source, category):
    folder = category if category in ("raw", "working") else "audit"
    directory = _checked(run_dir / folder, within=run_dir)
    directory.mkdir(exist_ok=True)
    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        suffix = ".bin"
    relative = f"{directory.name}/{len(manifest['files']):04d}-{label}{suffix}"
    target = _checked(run_dir / relative, within=run_dir)
    source = _checked(source, exists=True)
    raw = source.read_bytes()
    with target.open("xb") as stream:
        stream.write(raw)
    manifest["files"].append({"path": relative, "label": label, "category": category,
                              "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                              "original_path": str(source), "cleaned_at": None})
    _replace_json(run_dir / RUN_MANIFEST, manifest, run_dir)


def archive_run(root, input_paths, mode="research", raw_paths=None, now=None):
    """Copy normalized inputs and explicit raw files into a new pending run.

    ``input_paths`` maps labels to files. Labels ``config`` and ``model_config``
    are permanent audit configuration; all other input copies are raw. Extra
    ``raw_paths`` is a list of individual provider files, never directories.
    Original files are read only and are never managed by cleanup.
    """
    if mode not in ("research", "live"):
        raise DataError("Archive mode must be research or live")
    clock = _now(now)
    inputs = _sources(input_paths)
    if raw_paths is not None and not isinstance(raw_paths, (list, tuple)):
        raise DataError("raw_paths must be an explicit list of individual files")
    raw = _sources({f"provider_{i:04d}": source for i, source in enumerate(raw_paths or [])})
    root, registry = _root(root, create=True)
    run_id = clock.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:12]
    run_dir = _checked(root / run_id, within=root)
    run_dir.mkdir()
    manifest = {"schema_version": 1, "archive_id": registry["archive_id"], "run_id": run_id,
                "mode": mode, "status": "pending", "created_at": clock.isoformat(),
                "completed_at": None, "files": [], "metadata": {}, "cleanup_history": [],
                "explicit_raw_file_count": len(raw),
                "raw_source_scope": "explicit_provider_files" if raw else "normalized_input_files_only"}
    _new_json(run_dir / RUN_MANIFEST, manifest, run_dir)
    registry["runs"].append(run_id)
    _replace_json(root / ROOT_MARKER, registry, root)
    for label, source in inputs:
        _copy(run_dir, manifest, label, source, "audit_config" if label in ("config", "model_config") else "raw")
    for label, source in raw:
        _copy(run_dir, manifest, label, source, "raw")
    return run_dir


def finalize_run(run_dir, artifacts=None, metadata=None, now=None, working_files=None):
    """Attach compact forecast/audit files and mark a run successful.

    ``artifacts`` is a label->path mapping (or list of files). These copies are
    retained permanently. Prefer compact CSV forecasts and exclusion summaries;
    do not pass a full raw-history baseline as an audit artifact for live runs.
    Instead, ``working_files`` maps labels to full baselines/queues/documents;
    these copies are eligible for the same retention cleanup as raw files.
    """
    root, run_dir, manifest = _run(run_dir)
    if manifest["status"] != "pending":
        raise DataError("Run is already finalized; create a new run instead of overwriting it")
    clock = _now(now)
    if clock < _timestamp(manifest["created_at"]):
        raise DataError("Run completion cannot precede creation")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise DataError("Run metadata must be a JSON object")
    _json_bytes(metadata)  # Fail before copying artifacts if metadata is invalid.
    if isinstance(artifacts, (list, tuple)):
        artifacts = {f"artifact_{i:04d}": path for i, path in enumerate(artifacts)}
    audit_sources = _sources(artifacts or {})
    working_sources = _sources(working_files or {})
    for label, source in audit_sources:
        _copy(run_dir, manifest, label, source, "audit")
    for label, source in working_sources:
        _copy(run_dir, manifest, label, source, "working")
    manifest.update(status="complete", completed_at=clock.isoformat(), metadata=metadata)
    _replace_json(run_dir / RUN_MANIFEST, manifest, root)
    return manifest


def _owned_raw(run_dir, record):
    """Only flat raw/working files emitted by _copy are eligible for deletion."""
    relative = record.get("path")
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise DataError("Invalid registered raw path")
    parts = PurePosixPath(relative).parts
    if (len(parts) != 2 or parts[0] != record.get("category")
            or parts[0] not in ("raw", "working") or parts[1] in (".", "..")
            or PurePosixPath(relative).is_absolute()
            or not re.fullmatch(r"\d{4,}-[A-Za-z0-9_-]+\.[a-z0-9]{1,12}", parts[1])):
        raise DataError("Registered path is outside its owned flat raw/working directory")
    target = _checked(run_dir / relative, within=run_dir, exists=True)
    if not stat.S_ISREG(target.stat().st_mode):
        raise DataError("Registered raw target is not a regular file")
    if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))):
        raise DataError("Invalid raw file checksum")
    if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
        raise DataError("Raw file was edited after archival; preserving it")
    return target


def cleanup_runs(root, retention_days=7, dry_run=True, now=None):
    """Remove registered raw/working copies from old completed live runs.

    The latest complete live run is protected even if old. Research/pending runs
    and audit artifacts never expire. Every final target and all parents are
    checked for containment, symlinks and Windows reparse points before unlink.
    A dry run never writes manifests. Empty directories are intentionally kept.
    """
    if (isinstance(retention_days, bool) or not isinstance(retention_days, (int, float))
            or not math.isfinite(retention_days) or retention_days < 0):
        raise DataError("retention_days must be a finite nonnegative number")
    if not isinstance(dry_run, bool):
        raise DataError("dry_run must be boolean")
    clock = _now(now)
    root, registry = _root(root)
    report = {"root": str(root), "dry_run": dry_run, "retention_days": retention_days,
              "protected_latest_run": None, "eligible_files": [], "deleted_files": [],
              "skipped": [], "bytes_eligible": 0, "bytes_deleted": 0}
    live = []
    for run_id in registry["runs"]:
        try:
            _, directory, manifest = _run(root / run_id)
            if manifest["mode"] != "live" or manifest["status"] != "complete":
                report["skipped"].append({"run_id": run_id, "reason": "research_or_pending_run"})
                continue
            live.append((_timestamp(manifest["completed_at"]), run_id, directory, manifest))
        except (DataError, OSError, ValueError) as exc:
            report["skipped"].append({"run_id": run_id, "reason": str(exc)})
    latest = max(live, key=lambda item: (item[0], item[1]))[1] if live else None
    report["protected_latest_run"] = latest
    cutoff = clock - timedelta(days=retention_days)
    for completed, run_id, directory, manifest in live:
        if run_id == latest or completed > cutoff:
            report["skipped"].append({"run_id": run_id, "reason": "latest_or_within_retention"})
            continue
        cleaned = []
        for record in manifest["files"]:
            if (not isinstance(record, dict) or record.get("category") not in ("raw", "working")
                    or record.get("cleaned_at")):
                continue
            try:
                target = _owned_raw(directory, record)
                size = target.stat().st_size
                entry = {"run_id": run_id, "path": str(target), "bytes": size}
                report["eligible_files"].append(entry)
                report["bytes_eligible"] += size
                if not dry_run:
                    # Check the resolved final target again immediately before unlink.
                    _checked(target, within=directory, exists=True)
                    target.unlink()
                    record["cleaned_at"] = clock.isoformat()
                    cleaned.append(record["path"])
                    report["deleted_files"].append(entry)
                    report["bytes_deleted"] += size
            except (DataError, OSError, ValueError) as exc:
                report["skipped"].append({"run_id": run_id, "file": record.get("path"), "reason": str(exc)})
        if cleaned:
            manifest.setdefault("cleanup_history", []).append({"at": clock.isoformat(),
                "retention_days": retention_days, "deleted_cache_files": cleaned})
            _replace_json(directory / RUN_MANIFEST, manifest, root)
    return report
