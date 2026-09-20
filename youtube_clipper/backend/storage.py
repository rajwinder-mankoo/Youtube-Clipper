"""Conservative storage inventory: only recognized, disposable files qualify."""

import os
import re
import shutil
import time
from pathlib import Path


GROUP_NAMES = ("output", "cache", "input", "logs")


def _storage_roots(root, directories=None):
    """Resolve only the configured top-level storage roots.

    The Proxmox layout intentionally links these four directories to the data
    disk. The resolved targets are trusted, but links found inside them are not.
    """
    root = Path(root).resolve()
    configured = directories or {name: root / name for name in GROUP_NAMES}
    resolved = {}
    for name in GROUP_NAMES:
        path = Path(configured.get(name, root / name))
        if path.exists() and path.is_dir():
            resolved[name] = path.resolve()
    return root, resolved


def inventory(root, min_age_days=30, directories=None):
    if isinstance(min_age_days, bool) or not isinstance(min_age_days, int) or not 0 <= min_age_days <= 3650:
        raise ValueError("Age must be a whole number between 0 and 3650 days.")
    root, storage_roots = _storage_roots(root, directories)
    cutoff = time.time() - min_age_days * 86400
    groups = {name: {"bytes": 0, "files": 0} for name in GROUP_NAMES}
    candidates = []
    errors = []
    for name, totals in groups.items():
        base = storage_roots.get(name)
        if not base:
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            # Also exclude Windows junctions and other redirected directories.
            dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()
                       and (Path(directory) / d).resolve() == Path(directory) / d]
            for filename in files:
                path = Path(directory) / filename
                if path.is_symlink() or path.resolve() != path:
                    continue
                relative = Path(name) / path.relative_to(base)
                try:
                    stat = path.stat()
                    totals["bytes"] += stat.st_size
                    totals["files"] += 1
                    category = None
                    if name == "output" and re.fullmatch(r"short_\d+\.(?:source|clean)\.mp4", filename, re.I):
                        category = "masters"
                    elif name == "cache" and len(relative.parts) > 2 and relative.parts[1] == "render" and re.fullmatch(r"(?:cropped|fullframe)_\d+\.mp4", filename):
                        category = "scratch"
                    if category and stat.st_mtime <= cutoff:
                        candidates.append({"path": relative.as_posix(), "category": category,
                                           "bytes": stat.st_size, "mtime_ns": str(stat.st_mtime_ns)})
                except OSError as exc:
                    errors.append({"path": relative.as_posix(), "error": str(exc)})
    disk_root = storage_roots.get("output", root)
    disk = shutil.disk_usage(disk_root)
    return {"groups": groups, "disk": {"total": disk.total, "free": disk.free},
            "locations": {name: str(path) for name, path in storage_roots.items()},
            "candidates": sorted(candidates, key=lambda x: x["path"]), "errors": errors}


def cleanup(root, selected, min_age_days=30, directories=None):
    """Revalidate a reviewed snapshot; never accept arbitrary filesystem paths."""
    if not isinstance(selected, list) or not selected:
        raise ValueError("Select at least one file to clean up.")
    current = {
        item["path"]: item
        for item in inventory(root, min_age_days, directories)["candidates"]
    }
    validated = []
    seen = set()
    for item in selected:
        if not isinstance(item, dict) or item.get("path") not in current:
            raise ValueError("A selected file is no longer eligible. Refresh the storage preview.")
        fresh = current[item["path"]]
        if fresh != item:
            raise ValueError("A selected file has changed. Refresh the storage preview.")
        if item["path"] not in seen:
            validated.append(fresh)
            seen.add(item["path"])
    deleted, errors, freed = [], [], 0
    root, storage_roots = _storage_roots(root, directories)
    for item in validated:
        try:
            relative = Path(item["path"])
            if not relative.parts or relative.parts[0] not in storage_roots:
                raise ValueError("File location is not a configured storage root.")
            base = storage_roots[relative.parts[0]]
            path = base.joinpath(*relative.parts[1:])
            if path.resolve() != path or path.is_symlink():
                raise ValueError("File location changed.")
            path.relative_to(base)
            stat = path.stat()
            if str(stat.st_mtime_ns) != item["mtime_ns"] or stat.st_size != item["bytes"]:
                raise ValueError("File changed since preview.")
            path.unlink()
            deleted.append(item["path"])
            freed += item["bytes"]
        except (OSError, ValueError) as exc:
            errors.append({"path": item["path"], "error": str(exc)})
    return {"deleted": deleted, "freed_bytes": freed, "errors": errors}
