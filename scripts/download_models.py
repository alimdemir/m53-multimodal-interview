#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import fnmatch
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
MANIFEST_PATH = MODELS_DIR / "model-manifest.json"
LOCK_PATH = MODELS_DIR / "model-lock.json"


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def main() -> int:
    parser = argparse.ArgumentParser(description="M53 model ağırlıklarını kilitli commit ile indirir.")
    parser.add_argument("--profile", choices=["production", "coaching", "mac"], default="mac")
    parser.add_argument("--only", action="append", default=[], help="Yalnız verilen model anahtarını indirir; tekrarlanabilir.")
    parser.add_argument("--force-low-disk", action="store_true", help="8 GB güvenli boş alan korumasını geçersiz kılar.")
    args = parser.parse_args()

    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError:
        print("huggingface-hub kurulu değil. Önce requirements-models.txt dosyasını kurun.", file=sys.stderr)
        return 2

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    selected = []
    for model in manifest["models"]:
        allowed = model["profile"] == args.profile
        if args.profile == "coaching":
            allowed = model["profile"] == "coaching"
        if args.profile == "mac":
            allowed = model["profile"] in {"mac", "coaching"}
        if args.only:
            allowed = model["key"] in args.only
        if allowed:
            selected.append(model)
    if not selected:
        print("İndirilecek model seçilmedi.", file=sys.stderr)
        return 2

    api = HfApi()
    resolved = []
    total_missing = 0
    for model in selected:
        info = api.model_info(model["repository"], files_metadata=True)
        patterns = model.get("allow_patterns", ["*"])
        size = sum((item.size or 0) for item in info.siblings if any(fnmatch.fnmatch(item.rfilename, p) for p in patterns))
        destination = MODELS_DIR / model["destination"]
        existing = 0
        if destination.exists():
            existing = sum(
                file.stat().st_size
                for file in destination.rglob("*")
                if file.is_file() and ".cache" not in file.parts
            )
        missing = max(0, size - existing)
        total_missing += missing
        resolved.append((model, info.sha, size, destination))
        print(f"{model['key']}: {human_bytes(size)} · commit {info.sha[:12]}")

    free = shutil.disk_usage(MODELS_DIR).free
    reserve = 8 * 1024**3
    print(f"Tahmini yeni indirme: {human_bytes(total_missing)} · boş alan: {human_bytes(free)}")
    if not args.force_low_disk and free - total_missing < reserve:
        print(
            "İndirme sonrasında 8 GB güvenli boş alan kalmayacak. Farklı bir disk kullanın "
            "veya riski kabul ediyorsanız --force-low-disk ekleyin.",
            file=sys.stderr,
        )
        return 3

    previous = json.loads(LOCK_PATH.read_text()) if LOCK_PATH.exists() else {}
    lock = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "profile": args.profile,
        "models": previous.get("models", []),
    }
    for model, revision, size, destination in resolved:
        print(f"İndiriliyor: {model['repository']} -> {destination}")
        snapshot_download(
            repo_id=model["repository"],
            revision=revision,
            local_dir=destination,
            allow_patterns=model.get("allow_patterns"),
            max_workers=3,
        )
        lock["models"] = [item for item in lock["models"] if item["key"] != model["key"]]
        lock["models"].append(
            {
                "key": model["key"],
                "repository": model["repository"],
                "revision": revision,
                "destination": str(destination.relative_to(ROOT)),
                "reported_size_bytes": size,
                "purpose": model["purpose"],
                "license_gate": model["license_gate"],
            }
        )
        temporary = LOCK_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, LOCK_PATH)
    print(f"Tamamlandı. Kaynak kilidi: {LOCK_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
