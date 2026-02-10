#!/usr/bin/env python3
import argparse
import calendar
import csv
import datetime as dt
import json
import logging
import os
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import quote

import requests  # type: ignore[import-not-found]

NCTS_PREFIXES = (
    "http://healthterminologies.gov.au",
    "https://healthterminologies.gov.au",
    "https://ranzcr.com",
    "https://www.rcpa.edu.au",
    "http://www.abs.gov.au",
)

SNOMED_AU_SYSTEM = "http://snomed.info/sct/32506021000036107"
SNOMED_BASE_SYSTEM = "http://snomed.info/sct"

DEFAULT_CACHE_DIR = "~/.fhir/packages"
DEFAULT_CONFIG_PATH = "config.json"


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
        return data if isinstance(data, dict) else {}


def expand_user(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def parse_int(value: Any, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return default


def find_package_dir(cache_dir: str, package_id: str, version: str) -> str:
    package_folder = f"{package_id}#{version}"
    return os.path.join(cache_dir, package_folder, "package")


def read_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        logging.warning("Failed to read JSON: %s", path)
        return None


def list_json_files(folder: str) -> Iterable[str]:
    for entry in os.scandir(folder):
        if entry.is_file() and entry.name.endswith(".json"):
            yield entry.path


def get_package_dependencies(package_dir: str) -> Dict[str, str]:
    package_json = os.path.join(package_dir, "package.json")
    data = read_json_file(package_json) or {}
    deps = data.get("dependencies") or {}
    if isinstance(deps, dict):
        return {str(key): str(value) for key, value in deps.items()}
    return {}


def gather_packages(cache_dir: str, root_id: str, root_version: str) -> List[Tuple[str, str, str]]:
    visited: Set[Tuple[str, str]] = set()
    queue: List[Tuple[str, str]] = [(root_id, root_version)]
    packages: List[Tuple[str, str, str]] = []

    while queue:
        package_id, version = queue.pop(0)
        if (package_id, version) in visited:
            continue
        visited.add((package_id, version))
        package_dir = find_package_dir(cache_dir, package_id, version)
        if not os.path.isdir(package_dir):
            logging.warning("Missing package directory: %s", package_dir)
            continue
        packages.append((package_id, version, package_dir))

        for dep_id, dep_version in get_package_dependencies(package_dir).items():
            if (dep_id, dep_version) not in visited:
                queue.append((dep_id, dep_version))

    return packages


def extract_bound_valuesets(package_dir: str) -> List[Dict[str, str]]:
    bound_valuesets: List[Dict[str, str]] = []
    for path in list_json_files(package_dir):
        data = read_json_file(path)
        if not data or data.get("resourceType") != "StructureDefinition":
            continue
        url = data.get("url")
        name = data.get("name")
        elements = []
        for section in ("snapshot", "differential"):
            block = data.get(section) or {}
            block_elements = block.get("element") or []
            if isinstance(block_elements, list):
                elements.extend(block_elements)
        for element in elements:
            if not isinstance(element, dict):
                continue
            binding = element.get("binding") or {}
            if not isinstance(binding, dict):
                continue
            value_set = binding.get("valueSet")
            if isinstance(value_set, str) and value_set.strip():
                bound_valuesets.append(
                    {
                        "valueset_url": value_set.strip(),
                        "structure_definition_url": str(url) if url else "",
                        "structure_definition_name": str(name) if name else "",
                    }
                )
    return bound_valuesets


def collect_valuesets(package_dir: str) -> Dict[str, Dict[str, Any]]:
    valuesets: Dict[str, Dict[str, Any]] = {}
    for path in list_json_files(package_dir):
        data = read_json_file(path)
        if not data or data.get("resourceType") != "ValueSet":
            continue
        url = data.get("url")
        if isinstance(url, str) and url not in valuesets:
            valuesets[url] = data
    return valuesets


def is_ncts_valueset(url: str) -> bool:
    return any(url.startswith(prefix) for prefix in NCTS_PREFIXES)


def has_snomed_au_content(valueset: Mapping[str, Any]) -> bool:
    compose = valueset.get("compose") or {}
    if not isinstance(compose, dict):
        return False
    includes = compose.get("include") or []
    if not isinstance(includes, list):
        return False
    for include in includes:
        if not isinstance(include, dict):
            continue
        system = include.get("system")
        version = include.get("version")
        if isinstance(system, str):
            if system.startswith(SNOMED_AU_SYSTEM):
                return True
            if system == SNOMED_BASE_SYSTEM and isinstance(version, str):
                if SNOMED_AU_SYSTEM in version:
                    return True
    return False


def month_end_version(date_value: dt.date) -> str:
    last_day = calendar.monthrange(date_value.year, date_value.month)[1]
    return f"{date_value.year:04d}{date_value.month:02d}{last_day:02d}"


def compute_versions(count: int, today: Optional[dt.date] = None) -> List[str]:
    if count <= 0:
        return []
    today = today or dt.date.today()
    versions: List[str] = []
    year = today.year
    month = today.month
    for _ in range(count):
        date_value = dt.date(year, month, 1)
        versions.append(month_end_version(date_value))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return versions


def expand_valueset_count(
    endpoint: str, valueset_url: str, snomed_version: str
) -> Optional[int]:
    base_url = endpoint.rstrip("/") + "/ValueSet/$expand"
    system_version = f"{SNOMED_BASE_SYSTEM}%7C{SNOMED_AU_SYSTEM}/version/{snomed_version}"
    
    # Build URL with encoded parameters
    url = f"{base_url}?url={quote(valueset_url)}&system-version={system_version}&count=0&offset=0"
    if valueset_url == "https://healthterminologies.gov.au/fhir/ValueSet/healthcare-organisation-role-type-1":
        sys.stdout.write(f"url:{url}\n")
        sys.stdout.flush()
    try:
        response = requests.get(url, timeout=90)
    except requests.RequestException as exc:
        logging.warning("Expand request failed for %s: %s", valueset_url, exc)
        return None

    if response.status_code != 200:
        logging.warning(
            "Expand failed (%s) for %s version %s", response.status_code, valueset_url, snomed_version
        )
        return None

    try:
        payload = response.json()
    except json.JSONDecodeError:
        logging.warning("Invalid JSON response for %s", valueset_url)
        return None

    expansion = payload.get("expansion") or {}
    
    # Log the actual version used by the server
    used_version = None
    for param in expansion.get("parameter") or []:
        if param.get("name") == "used-codesystem":
            used_version = param.get("valueUri")
            break
    
    if used_version and snomed_version not in str(used_version):
        logging.warning(
            "Version mismatch for %s: requested %s but server used %s",
            valueset_url, snomed_version, used_version
        )
    
    total = expansion.get("total")
    total_value = parse_int(total, -1)
    if total_value >= 0:
        return total_value
    contains = expansion.get("contains")
    if isinstance(contains, list):
        return len(contains)
    logging.warning("Unexpected expansion format for %s", valueset_url)
    return None


def build_rows(
    deduped: List[Dict[str, str]],
    valueset_index: Dict[str, Dict[str, Any]],
    versions: List[str],
    endpoint: str,
    expand_func=expand_valueset_count,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for item in deduped:
        valueset_url = item.get("valueset_url", "")
        ncts = is_ncts_valueset(valueset_url)
        if not ncts:
            continue
        valueset = valueset_index.get(valueset_url) or {}
        valueset_name = valueset.get("name") or valueset.get("title") or ""
        snomed_au = bool(valueset) and has_snomed_au_content(valueset)

        row: Dict[str, object] = {
            "valueset_url": valueset_url,
            "valueset_name": valueset_name,
            "ncts": "yes" if ncts else "no",
            "snomed_au": "yes" if snomed_au else "no",
            "structure_definition_url": item.get("structure_definition_url", ""),
            "structure_definition_name": item.get("structure_definition_name", ""),
        }

        if ncts and versions:
            for version in versions:
                count = expand_func(endpoint, valueset_url, version)
                row[version] = "" if count is None else count
        rows.append(row)
    return rows


def write_tsv(
    rows: List[Dict[str, object]], output_path: str, version_columns: List[str]
) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    headers = [
        "valueset_url",
        "valueset_name",
        "ncts",
        "snomed_au",
        "structure_definition_url",
        "structure_definition_name",
    ] + version_columns

    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Traverse FHIR packages to list bound ValueSets and compare SNOMED CT AU expansions."
        )
    )
    parser.add_argument("ig_id", help="FHIR IG package id (e.g. hl7.fhir.au.base)")
    parser.add_argument("ig_version", help="FHIR IG package version (e.g. 1.2.0)")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"FHIR package cache (default: {DEFAULT_CACHE_DIR})",
    )
    args = parser.parse_args()

    config_path = expand_user(args.config)
    cache_dir = expand_user(args.cache_dir)

    config = load_config(config_path)
    endpoint = str(config.get("terminology_server", "https://tx.ontoserver.csiro.au/fhir"))
    versions_to_compare = parse_int(config.get("versions_to_compare"), 0)
    output_filename = str(config.get("output_filename", "vs-diff.tsv"))
    data_folder = expand_user(str(config.get("data_folder", "~/data/vs-differ")))

    output_path = os.path.join(data_folder, output_filename)
    log_dir = os.path.join(data_folder, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "vs-differ.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        filename=log_file,
    )

    packages = gather_packages(cache_dir, args.ig_id, args.ig_version)
    if not packages:
        logging.error("No packages found for %s#%s", args.ig_id, args.ig_version)
        return 1

    bound_valuesets: List[Dict[str, str]] = []
    valueset_index: Dict[str, Dict[str, Any]] = {}

    for package_id, version, package_dir in packages:
        logging.info("Scanning %s#%s", package_id, version)
        bound_valuesets.extend(extract_bound_valuesets(package_dir))
        valueset_index.update(collect_valuesets(package_dir))

    seen: Set[Tuple[str, str]] = set()
    deduped: List[Dict[str, str]] = []
    for item in bound_valuesets:
        key = (item.get("valueset_url", ""), item.get("structure_definition_url", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    versions = compute_versions(versions_to_compare)

    rows = build_rows(deduped, valueset_index, versions, endpoint)

    write_tsv(rows, output_path, versions)

    logging.info("Wrote %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
