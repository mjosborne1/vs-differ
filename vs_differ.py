#!/usr/bin/env python3
import argparse
import calendar
import csv
import datetime as dt
import json
import logging
import os
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple, cast
from urllib.parse import quote

import requests  # type: ignore[import-not-found]
try:
    import pandas as pd
    from openpyxl import Workbook
    from openpyxl.chart import LineChart, Reference
    EXCEL_AVAILABLE = True
except ImportError:
    EXCEL_AVAILABLE = False

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


def validate_versions_on_server(
    endpoint: str, valueset_index: Dict[str, Dict[str, Any]], versions: List[str]
) -> List[str]:
    """Validate which versions are available on the terminology server.
    
    Returns only the versions that are actually published on the server.
    Filters out versions that have not been released or pre-published.
    """
    # Find an NCTS valueset to test with
    for url, valueset_def in valueset_index.items():
        if is_ncts_valueset(url):
            # Test each version using this NCTS valueset
            valid_versions = []
            for version in versions:
                count, _ = expand_valueset_count(endpoint, url, version, valueset_def)
                if count is not None:
                    valid_versions.append(version)
                    logging.info("Version %s is available on terminology server", version)
                else:
                    logging.warning("Version %s not available on terminology server", version)
            return valid_versions
    
    # No NCTS valueset found, assume all versions are valid
    return versions


def expand_valueset_count(
    endpoint: str, valueset_url: str, snomed_version: str, valueset_def: Optional[Dict[str, Any]] = None
) -> tuple[Optional[int], Optional[str]]:
    """Expand a valueset and return (count, title).
    
    Returns a tuple of (expansion_count, valueset_title) where either can be None.
    """
    base_url = endpoint.rstrip("/") + "/ValueSet/$expand"
    system_version = f"{SNOMED_BASE_SYSTEM}%7C{SNOMED_AU_SYSTEM}/version/{snomed_version}"
    
    # Build URL with encoded parameters
    url = f"{base_url}?url={quote(valueset_url)}&system-version={system_version}&count=0&offset=0"
    try:
        response = requests.get(url, timeout=90)
    except requests.RequestException as exc:
        logging.warning("Expand request failed for %s: %s", valueset_url, exc)
        return None, None

    if response.status_code != 200:
        logging.warning(
            "Expand failed (%s) for %s version %s", response.status_code, valueset_url, snomed_version
        )
        return None, None

    try:
        payload = response.json()
    except json.JSONDecodeError:
        logging.warning("Invalid JSON response for %s", valueset_url)
        return None, None

    # Extract title from response
    title = payload.get("title") or payload.get("name")
    
    expansion = payload.get("expansion") or {}
    
    # Check if valueset contains SNOMED content
    has_snomed = valueset_def and has_snomed_au_content(valueset_def)
    
    # Only validate SNOMED version for valuesets that contain SNOMED codes
    if has_snomed:
        # Check if the server used the requested SNOMED version
        # Look for SNOMED-specific used-codesystem parameters
        snomed_version_used = None
        for param in expansion.get("parameter") or []:
            if param.get("name") == "used-codesystem":
                used_version = param.get("valueUri")
                # Only check SNOMED codesystems
                if used_version and ("snomed.info/sct" in str(used_version).lower()):
                    snomed_version_used = used_version
                    break
        
        # If we found a SNOMED codesystem in use, verify it matches what we requested
        if snomed_version_used and snomed_version not in str(snomed_version_used):
            logging.warning(
                "SNOMED version mismatch for %s: requested %s but server used %s",
                valueset_url, snomed_version, snomed_version_used
            )
            return None, None  # Return None for SNOMED version mismatches
    
    total = expansion.get("total")
    total_value = parse_int(total, -1)
    if total_value >= 0:
        return total_value, title
    contains = expansion.get("contains")
    if isinstance(contains, list):
        return len(contains), title
    logging.warning("Unexpected expansion format for %s", valueset_url)
    return None, title


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
        
        # Skip non-NCTS valuesets entirely
        if not ncts:
            continue
            
        valueset = valueset_index.get(valueset_url) or {}
        valueset_name = valueset.get("name") or valueset.get("title") or ""

        row: Dict[str, object] = {
            "valueset_url": valueset_url,
            "valueset_name": valueset_name,
            "structure_definition_url": item.get("structure_definition_url", ""),
            "structure_definition_name": item.get("structure_definition_name", ""),
        }

        # Expand NCTS valuesets
        if versions:
            for version in versions:
                count, api_title = expand_func(endpoint, valueset_url, version, valueset)
                # Use title from API if not already set from local definition
                if valueset_name == "" and api_title:
                    valueset_name = api_title
                    row["valueset_name"] = valueset_name
                row[version] = "" if count is None else count
        rows.append(row)
    
    # Group rows by valueset_url, combining structure definitions
    grouped: Dict[str, Dict[str, object]] = {}
    for row in rows:
        valueset_url = str(row["valueset_url"])
        if valueset_url not in grouped:
            grouped[valueset_url] = row
            grouped[valueset_url]["structure_definitions"] = []
        
        # Collect structure definitions
        sd_name = cast(str, row.get("structure_definition_name", ""))
        sd_url = cast(str, row.get("structure_definition_url", ""))
        if sd_name or sd_url:
            sd_list = cast(List[Tuple[str, str]], grouped[valueset_url]["structure_definitions"])
            sd_list.append((sd_name, sd_url))
    
    # Convert grouped data back to list format
    result: List[Dict[str, object]] = []
    for valueset_url, row in grouped.items():
        sds = cast(List[Tuple[str, str]], row.pop("structure_definitions", []))
        # Keep structure definitions as list of tuples for flexible formatting
        row["structure_definitions"] = sds
        # Remove individual structure definition columns
        row.pop("structure_definition_url", None)
        row.pop("structure_definition_name", None)
        result.append(row)
    
    return result

def is_change_significant(base_value, new_value):
    """
    Determines if a change is significant based on a sliding scale.
    Threshold = 0.5118 * (Value ^ 0.6257)
    """
    # Calculate the absolute difference
    difference = abs(new_value - base_value)
    
    # The constants derived from your points (500, 25) and (60,000, 500)
    # This creates a curve where the % required drops as the number grows.
    exponent = 0.6257
    coefficient = 0.5118
    
    # Calculate the dynamic threshold for this specific base value
    threshold = coefficient * (base_value ** exponent)
    
    return difference >= threshold, round(threshold, 2)

def write_tsv(
    rows: List[Dict[str, object]], output_path: str, version_columns: List[str]
) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    headers = [
        "ValueSet Name",
        "ValueSet URL",
        "Structure Definitions",
    ] + version_columns

    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter="\t")
        writer.writeheader()
        for row in rows:
            # Format structure definitions as text for TSV
            sds = row.get("structure_definitions", [])
            if isinstance(sds, list):
                sd_text = ", ".join(
                    f"{name} ({url})" if name and url else (name or url)
                    for name, url in cast(List[Tuple[str, str]], sds)
                )
            else:
                sd_text = str(sds) if sds else ""
            
            # Map old keys to new header names for output
            output_row = {
                "ValueSet Name": row.get("valueset_name", ""),
                "ValueSet URL": row.get("valueset_url", ""),
                "Structure Definitions": sd_text,
            }
            for version in version_columns:
                output_row[version] = row.get(version, "")
            writer.writerow(output_row)


def read_tsv_data(tsv_path: str) -> Tuple[List[Dict[str, object]], List[str]]:
    """Read TSV file and return rows and version columns.
    
    Used in dev mode to skip FHIR package processing and terminology server calls.
    Returns the same data structure as build_rows() would produce.
    """
    if not os.path.exists(tsv_path):
        raise FileNotFoundError(f"TSV file not found: {tsv_path}")
    
    rows: List[Dict[str, object]] = []
    version_columns: List[str] = []
    
    with open(tsv_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        headers = reader.fieldnames or []
        
        # Extract version columns (anything that's not a standard field)
        standard_fields = {"ValueSet Name", "ValueSet URL", "Structure Definitions"}
        version_columns = [h for h in headers if h not in standard_fields]
        
        for tsv_row in reader:
            # Parse structure definitions back from text format
            sd_text = tsv_row.get("Structure Definitions", "")
            structure_definitions: List[Tuple[str, str]] = []
            if sd_text:
                # Parse "Name (URL), Name (URL)" format
                for item in sd_text.split(", "):
                    if " (" in item and item.endswith(")"):
                        name = item[:item.rfind(" (")]
                        url = item[item.rfind("(") + 1:-1]
                        structure_definitions.append((name, url))
                    else:
                        structure_definitions.append((item, ""))
            
            # Build row dict matching the format from build_rows()
            row: Dict[str, object] = {
                "valueset_name": tsv_row.get("ValueSet Name", ""),
                "valueset_url": tsv_row.get("ValueSet URL", ""),
                "structure_definitions": structure_definitions,
            }
            
            # Add version columns with their values
            for version in version_columns:
                value = tsv_row.get(version, "")
                row[version] = value
            
            rows.append(row)
    
    logging.info("Loaded %d valuesets from TSV with %d versions", len(rows), len(version_columns))
    return rows, version_columns


def get_trending_status(row: Dict[str, object], version_columns: List[str]) -> Dict[str, str]:
    """Determine which version cells should be highlighted (red for decreasing values).
    
    Highlights a cell if the count drops compared to the previous chronological version.
    Since versions are ordered newest to oldest, checking i-1 compares to the next older version.
    Highlights the newer version where the drop occurred.
    
    Example: 20250531=508, 20250630=353 -> highlight 353 (the drop at 20250630)
    """
    trending = {}
    
    # First, initialize all cells as not trending
    for version in version_columns:
        trending[version] = ""
    
    # Now check for drops: if a newer version (lower index) is lower than older (higher index)
    for i in range(len(version_columns) - 1):
        current_version = version_columns[i]  # Newer version
        next_older_version = version_columns[i + 1]  # Older version
        
        current_val = row.get(current_version)
        next_val = row.get(next_older_version)
        
        if current_val == "" or current_val is None or next_val == "" or next_val is None:
            continue
        
        try:
            current_int = int(cast(Any, current_val))
            next_int = int(cast(Any, next_val))
            # If newer value is less than older value AND the change is significant, highlight
            if current_int < next_int:
                is_significant, threshold = is_change_significant(next_int, current_int)
                if is_significant:
                    trending[current_version] = "trending-down"
        except (ValueError, TypeError):
            continue
    
    return trending


def write_html(
    rows: List[Dict[str, object]], output_path: str, version_columns: List[str],
    terminology_server: str = "", versions_to_compare: int = 0, config_igs: Optional[List[Dict[str, Any]]] = None
) -> None:
    """Write HTML file with trending visualization (red for decreasing values)."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    if config_igs is None:
        config_igs = []
    
    # Reverse version columns to show oldest -> newest (matching the chart)
    reversed_version_columns = list(reversed(version_columns))
    
    headers = [
        "ValueSet Name",
        "ValueSet URL",
        "Structure Definitions",
    ] + reversed_version_columns
    
    # Map old keys to new header names
    header_mapping = {
        "ValueSet Name": "valueset_name",
        "ValueSet URL": "valueset_url",
        "Structure Definitions": "structure_definitions",
    }
    
    html_content = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        "<title>ValueSet Expansion Counts</title>",
        "<style>",
        "  * { margin: 0; padding: 0; box-sizing: border-box; }",
        "  body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: white; padding: 0; }",
        "  .container { width: 100%; margin: 0 auto; background-color: white; }",
        "  .table-container { padding: 10px; overflow-x: auto; }",
        "  table { border-collapse: collapse; width: 100%; }",
        "  th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }",
        "  th { background-color: #1D7DB3; color: white; position: sticky; top: 0; }",
        "  tr:nth-child(even) { background-color: #f2f2f2; }",
        "  .trending-down { background-color: #ffcccc; color: red; font-weight: bold; }",
        "  .version-col { text-align: center; }",
        "  .footer { background-color: #f9f9f9; padding: 20px; text-align: center; color: #666; font-size: 12px; border-top: 1px solid #e0e0e0; }",
        "</style>",
        "</head>",
        "<body>",
        "<div class='container'>",
        "<div class='table-container'>",
        "<table>",
    ]
    
    # Write header row
    html_content.append("<tr>")
    for header in headers:
        if header in version_columns:
            html_content.append(f"<th class='version-col'>{header}</th>")
        else:
            html_content.append(f"<th>{header}</th>")
    html_content.append("</tr>")
    
    # Write data rows
    for row in rows:
        trending = get_trending_status(row, version_columns)
        html_content.append("<tr>")
        
        for header in headers:
            # Get the value using the mapping
            if header in header_mapping:
                key = header_mapping[header]
                value = row.get(key, "")
                
                # Format structure definitions as HTML links
                if key == "structure_definitions" and isinstance(value, list):
                    links = ", ".join(
                        f"<a href='{url}'>{name}</a>" if url and name else (f"<a href='{url}'>Link</a>" if url else name)
                        for name, url in cast(List[Tuple[str, str]], value)
                    )
                    html_content.append(f"<td>{links}</td>")
                    continue
            else:
                value = row.get(header, "")
            
            is_version_col = header in reversed_version_columns
            
            if is_version_col and trending.get(header) == "trending-down":
                html_content.append(
                    f"<td class='trending-down version-col'>{value}</td>"
                )
            elif is_version_col:
                html_content.append(f"<td class='version-col'>{value}</td>")
            else:
                html_content.append(f"<td>{value}</td>")
        
        html_content.append("</tr>")
    
    html_content.extend([
        "</table>",
        "<p style='margin-top: 20px; font-size: 12px;'>",
        "<strong>Legend:</strong> Cells highlighted in red indicate a decrease in expansion count from the previous version.",
        "</p>",
        "</div>",
        "</div>",
        "</body>",
        "</html>",
    ])
    
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(html_content))


def write_chart_html(
    rows: List[Dict[str, object]], output_path: str, version_columns: List[str]
) -> Tuple[str, str, str]:
    """Create HTML files with SVG line charts showing version trends, split by count ranges.
    
    Creates 3 separate chart files:
    - Low count: < 1000
    - Medium count: 1000-10000
    - High count: > 10000
    
    Returns tuple of (low_path, medium_path, high_path)
    """
    import math
    
    # Reverse version columns to show oldest -> newest
    reversed_versions = list(reversed(version_columns))
    
    # Prepare data for charting and categorize by max value
    low_data = []     # < 1000
    medium_data = []  # 1000-50000
    high_data = []    # > 50000
    
    for row in rows:
        name = str(row.get("valueset_name", ""))
        values = []
        for version in reversed_versions:
            val = row.get(version, "")
            if val != "" and val is not None:
                try:
                    values.append(float(cast(Any, val)))
                except (ValueError, TypeError):
                    values.append(None)
            else:
                values.append(None)
        
        # Only include series with at least some data
        if any(v is not None for v in values):
            series = {"name": name, "values": values}
            max_val = max((v for v in values if v is not None), default=0)
            
            if max_val < 1000:
                low_data.append(series)
            elif max_val <= 50000:
                medium_data.append(series)
            else:
                high_data.append(series)
    
    # Generate the three chart files
    base_path = output_path.replace("-chart.html", "")
    low_path = f"{base_path}-chart-low.html"
    medium_path = f"{base_path}-chart-medium.html"
    high_path = f"{base_path}-chart-high.html"
    
    _generate_single_chart(low_data, low_path, reversed_versions, "Low Count ValueSets (< 1,000)", use_log=False)
    _generate_single_chart(medium_data, medium_path, reversed_versions, "Medium Count ValueSets (1,000 - 50,000)", use_log=False)
    _generate_single_chart(high_data, high_path, reversed_versions, "High Count ValueSets (> 50,000)", use_log=False)
    
    return low_path, medium_path, high_path


def _generate_single_chart(
    chart_data: List[Dict[str, Any]], 
    output_path: str, 
    reversed_versions: List[str],
    title: str,
    use_log: bool = False
) -> None:
    """Generate a single chart HTML file."""
    import math
    
    if not chart_data:
        logging.warning("No data for chart: %s", title)
        # Create empty chart
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset='UTF-8'>
    <title>{title}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }}
        .container {{ max-width: 1440px; margin: 0 auto; background-color: white; padding: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .empty {{ text-align: center; padding: 100px; color: #999; }}
    </style>
</head>
<body>
    <div class="container">
        <h2 style="text-align: center; color: #333;">{title}</h2>
        <div class="empty">
            <p>No valuesets in this range</p>
        </div>
    </div>
</body>
</html>"""
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(html_content)
        except Exception as exc:
            logging.warning("Failed to create chart HTML: %s", exc)
        return
    
    # Chart dimensions
    width = 1400
    margin_left = 80
    margin_right = 300
    margin_top = 60
    margin_bottom = 80
    
    # Calculate height dynamically based on number of items
    # Need at least 800 for chart, but extend if legend requires more space
    num_items = len(chart_data)
    legend_height = margin_top + (num_items * 20) + 20  # 20px per item + 20px padding
    base_height = 800
    height = max(base_height, legend_height)
    
    chart_width = width - margin_left - margin_right
    chart_height = base_height - margin_top - margin_bottom
    
    # Find min/max values for scaling
    all_values = [v for series in chart_data for v in series["values"] if v is not None and v > 0]
    if not all_values:
        logging.warning("No valid data for chart generation")
        return
    
    min_val = min(all_values)
    max_val = max(all_values)
    
    # Use linear scale with padding
    y_range = max_val - min_val
    y_min = max(0, min_val - y_range * 0.1)  # 10% padding below
    y_max = max_val + y_range * 0.1  # 10% padding above
    
    def y_scale(value):
        """Scale value to Y coordinate (linear scale)"""
        if value is None or y_max == y_min:
            return None
        ratio = (value - y_min) / (y_max - y_min)
        return margin_top + chart_height - (ratio * chart_height)
    
    def x_scale(index):
        """Scale index to X coordinate"""
        return margin_left + (index * chart_width / (len(reversed_versions) - 1))
    
    # Generate color palette
    def get_color(index):
        colors = ['#e41a1c', '#377eb8', '#1D7DB3', '#984ea3', '#ff7f00', 
                  '#ffff33', '#a65628', '#f781bf', '#999999', '#66c2a5',
                  '#fc8d62', '#8da0cb', '#e78ac3', '#a6d854', '#ffd92f']
        return colors[index % len(colors)]
    
    # Build SVG content
    svg_lines = []
    
    # Draw Y-axis grid lines and labels (5-7 lines)
    num_gridlines = 6
    for i in range(num_gridlines + 1):
        value = y_min + (y_max - y_min) * i / num_gridlines
        y = y_scale(value)
        if y is not None:
            svg_lines.append(f'<line x1="{margin_left}" y1="{y}" x2="{margin_left + chart_width}" y2="{y}" stroke="#e0e0e0" stroke-width="1"/>')
            svg_lines.append(f'<text x="{margin_left - 10}" y="{y + 5}" text-anchor="end" font-size="12" fill="#666">{int(value):,}</text>')
    
    # Draw X-axis labels
    for i, version in enumerate(reversed_versions):
        x = x_scale(i)
        svg_lines.append(f'<text x="{x}" y="{margin_top + chart_height + 30}" text-anchor="middle" font-size="11" fill="#666">{version}</text>')
    
    # Draw axes
    svg_lines.append(f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{margin_top + chart_height}" stroke="#333" stroke-width="2"/>')
    svg_lines.append(f'<line x1="{margin_left}" y1="{margin_top + chart_height}" x2="{margin_left + chart_width}" y2="{margin_top + chart_height}" stroke="#333" stroke-width="2"/>')
    
    # Draw lines for each valueset
    legend_items = []
    for idx, series in enumerate(chart_data):
        color = get_color(idx)
        points = []
        for i, value in enumerate(series["values"]):
            y = y_scale(value)
            if y is not None:
                x = x_scale(i)
                points.append(f"{x},{y}")
        
        if points:
            # Add line with tooltip showing valueset name
            svg_lines.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2"><title>{series["name"]}</title></polyline>')
            
            # Add points with tooltips showing count
            for i, value in enumerate(series["values"]):
                y = y_scale(value)
                if y is not None:
                    x = x_scale(i)
                    version = reversed_versions[i]
                    # Create circle with tooltip showing count
                    svg_lines.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{color}"><title>{int(value):,}</title></circle>')
            
            legend_items.append((color, series["name"]))
    
    # Create legend
    legend_y = margin_top
    for color, name in legend_items:
        svg_lines.append(f'<line x1="{margin_left + chart_width + 20}" y1="{legend_y}" x2="{margin_left + chart_width + 50}" y2="{legend_y}" stroke="{color}" stroke-width="2"/>')
        svg_lines.append(f'<text x="{margin_left + chart_width + 55}" y="{legend_y + 4}" font-size="11" fill="#333">{name[:35]}</text>')
        legend_y += 20
    
    # Axis titles
    svg_lines.append(f'<text x="{margin_left + chart_width / 2}" y="{margin_top + chart_height + 60}" text-anchor="middle" font-size="14" font-weight="bold" fill="#333">Release (Oldest to Newest)</text>')
    svg_lines.append(f'<text x="{margin_left - 60}" y="{margin_top + chart_height / 2}" text-anchor="middle" font-size="14" font-weight="bold" fill="#333" transform="rotate(-90, {margin_left - 60}, {margin_top + chart_height / 2})">Count of Elements</text>')
    svg_lines.append(f'<text x="{width / 2}" y="{margin_top - 20}" text-anchor="middle" font-size="18" font-weight="bold" fill="#333">{title}</text>')
    
    # Generate HTML
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset='UTF-8'>
    <title>{title}</title>
    <style>
        body {{ 
            font-family: Arial, sans-serif; 
            margin: 20px; 
            background-color: #f5f5f5;
            position: relative;
        }}
        .container {{ 
            max-width: {width + 40}px; 
            margin: 0 auto; 
            background-color: white; 
            padding: 20px; 
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        svg {{ 
            display: block; 
            margin: 20px auto;
        }}
        .tooltip {{
            position: fixed;
            background-color: rgba(0, 0, 0, 0.9);
            color: white;
            padding: 8px 12px;
            border-radius: 4px;
            font-size: 14px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.2s;
            z-index: 10000;
            white-space: nowrap;
        }}
        .tooltip.visible {{
            opacity: 1;
        }}
        polyline {{ cursor: pointer; }}
        circle {{ cursor: pointer; }}
    </style>
</head>
<body>
    <div class="tooltip" id="tooltip"></div>
    <div class="container">
        <svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" id="chart-svg">
            {chr(10).join(svg_lines)}
        </svg>
        <p style="text-align: center; color: #666; font-size: 12px;">
            Showing trends for {len(chart_data)} NCTS valuesets across {len(reversed_versions)} releases
        </p>
    </div>
    <script>
        const tooltip = document.getElementById('tooltip');
        const svg = document.getElementById('chart-svg');
        
        // Add hover events to all polylines (lines)
        const polylines = svg.querySelectorAll('polyline');
        polylines.forEach(polyline => {{
            polyline.addEventListener('mouseover', (e) => {{
                const title = polyline.querySelector('title');
                if (title) {{
                    tooltip.textContent = title.textContent;
                    tooltip.classList.add('visible');
                }}
            }});
            
            polyline.addEventListener('mousemove', (e) => {{
                tooltip.style.left = (e.clientX + 15) + 'px';
                tooltip.style.top = (e.clientY - 15) + 'px';
            }});
            
            polyline.addEventListener('mouseout', () => {{
                tooltip.classList.remove('visible');
            }});
        }});
        
        // Add hover events to all circles (data points)
        const circles = svg.querySelectorAll('circle');
        circles.forEach(circle => {{
            circle.addEventListener('mouseover', (e) => {{
                const title = circle.querySelector('title');
                if (title) {{
                    tooltip.textContent = title.textContent;
                    tooltip.classList.add('visible');
                }}
            }});
            
            circle.addEventListener('mousemove', (e) => {{
                tooltip.style.left = (e.clientX + 15) + 'px';
                tooltip.style.top = (e.clientY - 15) + 'px';
            }});
            
            circle.addEventListener('mouseout', () => {{
                tooltip.classList.remove('visible');
            }});
        }});
    </script>
</body>
</html>"""
    
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception as exc:
        logging.warning("Failed to create chart HTML: %s", exc)


def create_web_folder(
    data_folder: str, html_path: str, chart_low_path: str, chart_medium_path: str, chart_high_path: str,
    terminology_server: str = "", versions_to_compare: int = 0, 
    config_igs: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Create a web-ready folder with index.html and supporting files.
    
    Returns the path to the web folder.
    """
    import shutil
    
    if config_igs is None:
        config_igs = []
    
    # Create web folder
    web_folder = os.path.join(data_folder, "web")
    os.makedirs(web_folder, exist_ok=True)
    
    # Copy HTML files to web folder
    table_filename = "table.html"
    chart_low_filename = "chart-low.html"
    chart_medium_filename = "chart-medium.html"
    chart_high_filename = "chart-high.html"
    
    try:
        shutil.copy2(html_path, os.path.join(web_folder, table_filename))
        shutil.copy2(chart_low_path, os.path.join(web_folder, chart_low_filename))
        shutil.copy2(chart_medium_path, os.path.join(web_folder, chart_medium_filename))
        shutil.copy2(chart_high_path, os.path.join(web_folder, chart_high_filename))
    except Exception as exc:
        logging.warning("Failed to copy HTML files to web folder: %s", exc)
        return web_folder
    
    # Build description from config
    igs_text = ", ".join(f"{ig.get('id')}#{ig.get('version')}" for ig in config_igs if ig.get('id') and ig.get('version'))
    
    # Create index.html
    index_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset='UTF-8'>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NCTS ValueSet Analysis</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: white;
            min-height: 100vh;
            padding: 10px;
        }}
        .container {{
            max-width: 98%;
            margin: 0 auto;
            background-color: white;
            border-radius: 10px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #1D7DB3 0%, #1668a0 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 32px;
            margin-bottom: 10px;
        }}
        .header p {{
            font-size: 16px;
            opacity: 0.9;
        }}
        .report-info {{
            background-color: #f9f9f9;
            border-left: 4px solid #1D7DB3;
            padding: 20px 30px;
            margin: 20px 30px;
        }}
        .report-info p {{
            margin: 8px 0;
            font-size: 14px;
            color: #333;
        }}
        .report-info strong {{
            color: #1D7DB3;
        }}
        .nav-tabs {{
            display: flex;
            background-color: #f1f1f1;
            border-bottom: 2px solid #1D7DB3;
            padding: 0 30px;
        }}
        .nav-tabs button {{
            background-color: transparent;
            border: none;
            padding: 15px 30px;
            cursor: pointer;
            font-size: 16px;
            font-weight: 500;
            color: #666;
            transition: all 0.3s;
            border-bottom: 3px solid transparent;
        }}
        .nav-tabs button:hover {{
            background-color: #e9e9e9;
            color: #333;
        }}
        .nav-tabs button.active {{
            color: #1D7DB3;
            border-bottom-color: #1D7DB3;
            background-color: white;
        }}
        .tab-content {{
            display: none;
            padding: 0;
        }}
        .tab-content.active {{
            display: block;
        }}
        iframe {{
            width: 100%;
            min-height: 1600px;
            border: none;
        }}
        .footer {{
            background-color: #f9f9f9;
            padding: 20px;
            text-align: center;
            color: #666;
            font-size: 12px;
            border-top: 1px solid #e0e0e0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>NCTS ValueSet Analysis Dashboard</h1>
            <p>Comprehensive analysis of NCTS ValueSet expansions across NCTS releases</p>
        </div>
        
        <div class="report-info">
            <p><strong>FHIR IGs:</strong> {igs_text or 'N/A'}</p>
            <p><strong>Terminology Server:</strong> {terminology_server or 'N/A'}</p>
            <p><strong>Versions Compared:</strong> {versions_to_compare} months</p>
            <p><strong>Generated:</strong> {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
        
        <div class="nav-tabs">
            <button class="tab-button active" onclick="openTab(event, 'table')">📋 Data Table</button>
            <button class="tab-button" onclick="openTab(event, 'chart-low')">📊 Low Count (<1K)</button>
            <button class="tab-button" onclick="openTab(event, 'chart-medium')">📈 Medium Count (1K-50K)</button>
            <button class="tab-button" onclick="openTab(event, 'chart-high')">📉 High Count (>50K)</button>
        </div>
        
        <div id="table" class="tab-content active">
            <iframe src="{table_filename}" title="Table View"></iframe>
        </div>
        
        <div id="chart-low" class="tab-content">
            <iframe src="{chart_low_filename}" title="Low Count Chart"></iframe>
        </div>
        
        <div id="chart-medium" class="tab-content">
            <iframe src="{chart_medium_filename}" title="Medium Count Chart"></iframe>
        </div>
        
        <div id="chart-high" class="tab-content">
            <iframe src="{chart_high_filename}" title="High Count Chart"></iframe>
        </div>
        
        <div class="footer">
            <p>Generated by <a href='https://github.com/mjosborne1/vs-differ' target='_blank' style='color: #1D7DB3; text-decoration: none;'>NCTS ValueSet Differ Tool</a></p>
            <p>ValueSets sourced from HL7 AU FHIR IGs and monthly NCTS Terminology releases</p>
        </div>
    </div>
    
    <script>
        function openTab(evt, tabName) {{
            // Hide all tab contents
            var tabContents = document.getElementsByClassName("tab-content");
            for (var i = 0; i < tabContents.length; i++) {{
                tabContents[i].classList.remove("active");
            }}
            
            // Remove active class from all buttons
            var tabButtons = document.getElementsByClassName("tab-button");
            for (var i = 0; i < tabButtons.length; i++) {{
                tabButtons[i].classList.remove("active");
            }}
            
            // Show the selected tab and mark button as active
            document.getElementById(tabName).classList.add("active");
            evt.currentTarget.classList.add("active");
        }}
    </script>
</body>
</html>"""
    
    index_path = os.path.join(web_folder, "index.html")
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(index_html)
        logging.info("Created web folder at: %s", web_folder)
    except Exception as exc:
        logging.warning("Failed to create index.html: %s", exc)
    
    return web_folder


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Traverse FHIR packages to list bound ValueSets and compare SNOMED CT AU expansions."
        )
    )
    parser.add_argument(
        "ig_id", 
        nargs="?",
        help="FHIR IG package id (e.g. hl7.fhir.au.base). If not provided, uses 'ig' array from config."
    )
    parser.add_argument(
        "ig_version",
        nargs="?",
        help="FHIR IG package version (e.g. 1.2.0). If not provided, uses 'ig' array from config."
    )
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
    parser.add_argument(
        "-v",
        "--sctver",
        help="Latest SNOMED CT AU version (YYYYMMDD format). Versions newer than this will be filtered out. If not specified, versions more than 7 days in the future will be removed.",
    )
    args = parser.parse_args()

    config_path = expand_user(args.config)
    cache_dir = expand_user(args.cache_dir)

    config = load_config(config_path)
    endpoint = str(config.get("terminology_server", "https://tx.ontoserver.csiro.au/fhir"))
    versions_to_compare = parse_int(config.get("versions_to_compare"), 0)
    output_filename = str(config.get("output_filename", "vs-diff.tsv"))
    data_folder = expand_user(str(config.get("data_folder", "~/data/vs-differ")))
    dev_mode = config.get("dev", False)

    output_path = os.path.join(data_folder, output_filename)
    html_output_path = output_path.replace(".tsv", ".html")
    log_dir = os.path.join(data_folder, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "vs-differ.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        filename=log_file,
    )

    # Determine which IGs to process
    if args.ig_id and args.ig_version:
        igs = [{"id": args.ig_id, "version": args.ig_version}]
    else:
        igs = config.get("ig", [])
        if not igs:
            logging.error("No IGs specified in config and no command-line arguments provided")
            return 1

    # DEV MODE: If TSV file exists, use it instead of processing FHIR packages
    if dev_mode and os.path.exists(output_path):
        logging.info("DEV MODE: Using existing TSV file: %s", output_path)
        print(f"\n✓ DEV MODE: Loading data from existing TSV file")
        print(f"  {output_path}")
        
        try:
            rows, versions = read_tsv_data(output_path)
        except Exception as exc:
            logging.error("Failed to read TSV file: %s", exc)
            return 1
        
        # Skip the version filtering since TSV already has the right versions
        # Just regenerate HTML and charts from the TSV data
        write_html(rows, html_output_path, versions, endpoint, versions_to_compare, igs)
        
        # Create HTML charts
        chart_output_path = output_path.replace(".tsv", "-chart.html")
        chart_low_path, chart_medium_path, chart_high_path = write_chart_html(rows, chart_output_path, versions)
        
        logging.info("Regenerated %s", html_output_path)
        logging.info("Regenerated %s", chart_low_path)
        logging.info("Regenerated %s", chart_medium_path)
        logging.info("Regenerated %s", chart_high_path)
        
        # Create web-ready folder with index.html
        web_folder = create_web_folder(data_folder, html_output_path, 
                                       chart_low_path, chart_medium_path, chart_high_path,
                                       endpoint, versions_to_compare, igs)
        logging.info("Created web folder: %s", web_folder)
        print(f"\n✓ Web folder ready for deployment: {web_folder}")
        print(f"  - Upload the contents of this folder to your web host")
        print(f"  - Open index.html in a browser to view the dashboard")
        
        return 0

    bound_valuesets: List[Dict[str, str]] = []
    valueset_index: Dict[str, Dict[str, Any]] = {}

    # Process each IG
    for ig in igs:
        ig_id = ig.get("id")
        ig_version = ig.get("version")
        if not ig_id or not ig_version:
            logging.warning("Invalid IG config: missing id or version")
            continue
        
        logging.info("Processing IG: %s#%s", ig_id, ig_version)
        packages = gather_packages(cache_dir, ig_id, ig_version)
        if not packages:
            logging.warning("No packages found for %s#%s", ig_id, ig_version)
            continue

        for package_id, version, package_dir in packages:
            logging.info("Scanning %s#%s", package_id, version)
            bound_valuesets.extend(extract_bound_valuesets(package_dir))
            valueset_index.update(collect_valuesets(package_dir))

    if not bound_valuesets:
        logging.error("No bound valuesets found in any IG")
        return 1

    seen: Set[Tuple[str, str]] = set()
    deduped: List[Dict[str, str]] = []
    for item in bound_valuesets:
        key = (item.get("valueset_url", ""), item.get("structure_definition_url", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    logging.info("Found %d unique ValueSets bound in elements", len(deduped))

    # Dev mode: limit to a sample of NCTS valuesets for faster testing
    # This sample should include valuesets across different count ranges (low, medium, high)
    if dev_mode:
        # Filter to only NCTS valuesets
        ncts_deduped = [vs for vs in deduped if is_ncts_valueset(vs.get("valueset_url", ""))]
        
        # Take every 3rd valueset to get better distribution across count ranges
        # This gives us a more representative sample than just taking the first N
        sampled = [ncts_deduped[i] for i in range(0, min(150, len(ncts_deduped)), 3)]
        deduped = sampled[:50]  # Limit to 50 for reasonable runtime
        logging.info("DEV MODE: Limited to %d valuesets for testing (sampled every 3rd)", len(deduped))

    versions = compute_versions(versions_to_compare)
    
    # Validate that versions are available on the server (filters out unreleased versions)
    versions = validate_versions_on_server(endpoint, valueset_index, versions)
    
    if not versions:
        logging.error("No valid versions found on terminology server")
        return 1

    # Filter out versions based on sctver or 7-day future cutoff
    if args.sctver:
        # User specified latest SNOMED CT AU version
        try:
            max_version_date = dt.datetime.strptime(args.sctver, "%Y%m%d").date()
            logging.info("Using SNOMED CT AU version cutoff: %s", args.sctver)
        except ValueError:
            logging.error("Invalid sctver format: %s (expected YYYYMMDD)", args.sctver)
            return 1
    else:
        # Default to 7 days in the future
        today = dt.date.today()
        max_version_date = today + dt.timedelta(days=7)
        logging.info("Using default cutoff: 7 days in future from %s", today)
    
    filtered_versions = []
    for version in versions:
        try:
            version_date = dt.datetime.strptime(version, "%Y%m%d").date()
            if version_date > max_version_date:
                if args.sctver:
                    logging.info(
                        "Removing version %s: newer than specified SNOMED CT AU version %s",
                        version, args.sctver
                    )
                else:
                    logging.info(
                        "Removing version %s: more than 7 days in the future",
                        version
                    )
            else:
                filtered_versions.append(version)
        except ValueError:
            logging.warning("Invalid version date format: %s", version)
            continue
    
    versions = filtered_versions
    if not versions:
        logging.error("No versions remain after filtering")
        return 1

    rows = build_rows(deduped, valueset_index, versions, endpoint)
    
    # Filter out version columns that have no data across all rows
    versions_with_data = []
    for version in versions:
        has_data = any(row.get(version) != "" and row.get(version) is not None for row in rows)
        if has_data:
            versions_with_data.append(version)
    
    # Remove empty version columns from rows
    for row in rows:
        for version in versions:
            if version not in versions_with_data:
                row.pop(version, None)
    
    # Sort rows by ValueSet Name
    rows.sort(key=lambda r: str(r.get("valueset_name", "")).lower())

    write_tsv(rows, output_path, versions)
    write_html(rows, html_output_path, versions, endpoint, versions_to_compare, igs)
    
    # Create HTML charts (3 separate charts by count range)
    chart_output_path = output_path.replace(".tsv", "-chart.html")
    chart_low_path, chart_medium_path, chart_high_path = write_chart_html(rows, chart_output_path, versions)

    logging.info("Wrote %s", output_path)
    logging.info("Wrote %s", html_output_path)
    logging.info("Wrote %s", chart_low_path)
    logging.info("Wrote %s", chart_medium_path)
    logging.info("Wrote %s", chart_high_path)
    
    # Create web-ready folder with index.html
    web_folder = create_web_folder(data_folder, html_output_path, 
                                   chart_low_path, chart_medium_path, chart_high_path,
                                   endpoint, versions_to_compare, igs)
    logging.info("Created web folder: %s", web_folder)
    print(f"\n✓ Web folder ready for deployment: {web_folder}")
    print(f"  - Upload the contents of this folder to your web host")
    print(f"  - Open index.html in a browser to view the dashboard")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
