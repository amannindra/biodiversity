#!/usr/bin/env python3
"""Download Phalaenoptilus nuttallii photos from iNaturalist.

The script downloads observation photos into a local folder and writes a
manifest with source, observer, license, and observation metadata.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


API_BASE = "https://api.inaturalist.org/v1"
SPECIES_NAME = "Phalaenoptilus nuttallii"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "phalaenoptilus_nuttallii_photos"
USER_AGENT = "Biodiversity photo downloader (local research script)"


def request_json(session: requests.Session, url: str, params: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(5):
        response = session.get(url, params=params, timeout=30)
        if response.status_code == 429:
            wait_seconds = int(response.headers.get("Retry-After", "10"))
            time.sleep(wait_seconds)
            continue
        if response.status_code >= 500 and attempt < 4:
            time.sleep(2**attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Could not fetch JSON from {response.url}: HTTP {response.status_code}")


def find_taxon_id(session: requests.Session, species_name: str) -> int:
    data = request_json(
        session,
        f"{API_BASE}/taxa",
        {"q": species_name, "rank": "species", "per_page": 10},
    )
    for taxon in data.get("results", []):
        if taxon.get("name", "").lower() == species_name.lower():
            return int(taxon["id"])
    names = ", ".join(taxon.get("name", "unknown") for taxon in data.get("results", []))
    raise RuntimeError(f"Could not resolve taxon for {species_name!r}. API returned: {names}")


def photo_url(photo: dict[str, Any], size: str) -> str | None:
    if size == "original" and photo.get("original_url"):
        return photo["original_url"]
    url = photo.get("url") or photo.get("medium_url") or photo.get("original_url")
    if not url:
        return None
    return re.sub(r"/(square|small|medium|large)\.", f"/{size}.", url)


def extension_from_response(url: str, content_type: str | None) -> str:
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if path_ext == ".jpeg" else path_ext
    return ".jpg"


def download_photo(session: requests.Session, url: str, destination_without_ext: Path) -> Path:
    for attempt in range(5):
        response = session.get(url, timeout=60)
        if response.status_code == 429:
            wait_seconds = int(response.headers.get("Retry-After", "10"))
            time.sleep(wait_seconds)
            continue
        if response.status_code >= 500 and attempt < 4:
            time.sleep(2**attempt)
            continue
        response.raise_for_status()
        extension = extension_from_response(url, response.headers.get("content-type"))
        destination = destination_without_ext.with_suffix(extension)
        destination.write_bytes(response.content)
        return destination
    raise RuntimeError(f"Could not download {url}: HTTP {response.status_code}")


def existing_photo_ids(output_dir: Path) -> set[str]:
    return {
        path.stem.split("_photo_")[-1]
        for path in output_dir.glob("inat_obs_*_photo_*.*")
        if "_photo_" in path.stem
    }


def write_manifest_header(path: Path) -> None:
    if path.exists() and path.stat().st_size:
        return
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()


MANIFEST_FIELDS = [
    "file",
    "photo_id",
    "observation_id",
    "observed_on",
    "place_guess",
    "observer",
    "license_code",
    "attribution",
    "source_url",
    "image_url",
]


def append_manifest_row(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
        writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--target-count", type=int, default=1000)
    parser.add_argument("--size", choices=["small", "medium", "large", "original"], default="large")
    parser.add_argument("--per-page", type=int, default=200)
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between downloaded images.")
    parser.add_argument("--quality-grade", default=None, help="Optional iNaturalist quality grade filter.")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.csv"
    write_manifest_header(manifest_path)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    taxon_id = find_taxon_id(session, SPECIES_NAME)
    seen_photo_ids = existing_photo_ids(args.output_dir)
    downloaded = 0
    skipped_existing = 0
    page = 1

    while downloaded < args.target_count:
        params: dict[str, Any] = {
            "taxon_id": taxon_id,
            "photos": "true",
            "order_by": "created_at",
            "order": "desc",
            "per_page": min(args.per_page, 200),
            "page": page,
        }
        if args.quality_grade:
            params["quality_grade"] = args.quality_grade

        data = request_json(session, f"{API_BASE}/observations", params)
        observations = data.get("results", [])
        if not observations:
            break

        for observation in observations:
            if downloaded >= args.target_count:
                break
            observation_id = observation.get("id")
            source_url = observation.get("uri") or f"https://www.inaturalist.org/observations/{observation_id}"
            observer = (observation.get("user") or {}).get("login")

            for photo in observation.get("photos", []):
                if downloaded >= args.target_count:
                    break
                photo_id = str(photo.get("id") or "")
                if not photo_id or photo_id in seen_photo_ids:
                    skipped_existing += 1
                    continue
                url = photo_url(photo, args.size)
                if not url:
                    continue

                base_name = args.output_dir / f"inat_obs_{observation_id}_photo_{photo_id}"
                try:
                    image_path = download_photo(session, url, base_name)
                except requests.HTTPError as error:
                    print(f"warning: skipped photo {photo_id}: {error}", file=sys.stderr)
                    continue

                seen_photo_ids.add(photo_id)
                downloaded += 1
                append_manifest_row(
                    manifest_path,
                    {
                        "file": image_path.name,
                        "photo_id": photo_id,
                        "observation_id": observation_id,
                        "observed_on": observation.get("observed_on") or "",
                        "place_guess": observation.get("place_guess") or "",
                        "observer": observer or "",
                        "license_code": photo.get("license_code") or "",
                        "attribution": photo.get("attribution") or "",
                        "source_url": source_url,
                        "image_url": url,
                    },
                )
                print(f"{downloaded:04d}/{args.target_count} {image_path.name}")
                time.sleep(args.delay)

        page += 1
        total_pages = int(data.get("total_results", 0) / params["per_page"]) + 1
        if page > total_pages:
            break

    summary = {
        "species": SPECIES_NAME,
        "taxon_id": taxon_id,
        "output_dir": str(args.output_dir),
        "target_count": args.target_count,
        "new_downloads": downloaded,
        "existing_skipped": skipped_existing,
        "total_files": len(list(args.output_dir.glob("inat_obs_*_photo_*.*"))),
        "manifest": str(manifest_path),
    }
    (args.output_dir / "download_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
