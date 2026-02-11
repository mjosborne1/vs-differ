# vs-differ

A Python tool for comparing SNOMED CT AU valueset expansions across multiple FHIR package versions.

## Overview

`vs-differ` traverses FHIR Implementation Guide (IG) packages to:
- Extract bound ValueSets from StructureDefinitions
- Query a FHIR terminology server to expand NCTS valuesets
- Compare expansion counts across multiple SNOMED CT AU versions
- Generate reports showing trends and highlighting when expansion counts decrease

## Features

- **Multi-IG Support**: Process multiple FHIR IGs in a single run via configuration
- **Version Trending**: Automatically detects when valueset expansion counts decrease between versions
- **Dual Output**: Generates both TSV (for data analysis) and HTML (for visualization) reports
- **Flexible Configuration**: Uses JSON config file for IGs, terminology server endpoint, and comparison versions
- **Comprehensive Logging**: Detailed logs for troubleshooting and version mismatch detection

## Requirements

- Python 3.8+
- `requests` library
- FHIR packages in a local cache directory
- Access to a FHIR terminology server (FHIR endpoint with ValueSet/$expand support)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/mjosborne1/vs-differ.git
cd vs-differ
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

## Configuration

Configure the tool using `config.json`:

```json
{
  "terminology_server": "https://tx.ontoserver.csiro.au/fhir",
  "versions_to_compare": 12,
  "output_filename": "vs-diff.tsv",
  "data_folder": "~/data/vs-differ",
  "ig": [
    {
      "id": "hl7.fhir.au.base",
      "version": "6.0.0"
    },
    {
      "id": "hl7.fhir.au.core",
      "version": "2.0.0"
    }
  ]
}
```

### Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `terminology_server` | FHIR terminology server endpoint URL | `https://tx.ontoserver.csiro.au/fhir` |
| `versions_to_compare` | Number of recent SNOMED CT AU month-end versions to fetch | `12` |
| `output_filename` | Name of the output TSV file | `vs-diff.tsv` |
| `data_folder` | Directory for output files and logs | `~/data/vs-differ` |
| `ig` | Array of FHIR IGs to process (id and version pairs) | `[]` |

### SNOMED CT AU Versions

Versions are specified as YYYYMMDD (month-end) dates. The tool automatically computes the last N month-end versions. For example, with `versions_to_compare: 3`:
- 20260228 (February 2026)
- 20260131 (January 2026)
- 20251231 (December 2025)

## Usage

### Run with configuration file IGs

```bash
python vs_differ.py
```

This processes all IGs defined in the `ig` array in `config.json`.

### Run with specific IG (legacy mode)

```bash
python vs_differ.py hl7.fhir.au.base 6.0.0
```

This overrides the IGs in the config file.

### Options

```bash
python vs_differ.py --help
```

- `--config`: Path to config JSON file (default: `config.json`)
- `--cache-dir`: FHIR package cache directory (default: `~/.fhir/packages`)

## Output Files

### TSV Report

**File**: `vs-diff.tsv`

Tab-separated values file with columns:
- `valueset_url`: The official URL of the ValueSet
- `valueset_name`: Human-readable ValueSet name
- `ncts`: Whether the ValueSet is from NCTS (yes/no)
- `snomed_au`: Whether the ValueSet contains SNOMED AU content (yes/no)
- `structure_definition_url`: URL of the StructureDefinition using this ValueSet
- `structure_definition_name`: Name of the StructureDefinition
- Version columns (one per SNOMED CT AU version): Expansion counts or empty if not applicable

### HTML Report

**File**: `vs-diff.html`

Interactive HTML report with:
- Styled table with alternating row colors
- **Red highlighting** on cells where counts decreased compared to the previous version
- Legend explaining the trending indicator
- Centered version columns for easy scanning

## Trending Analysis

The HTML report highlights cells in red when an expansion count **decreases** compared to the next chronological SNOMED CT AU version. This helps identify:

- ValueSets that are shrinking in subsequent SNOMED CT AU versions
- Potential issues with valueset bindings or concept changes
- Deprecated concepts being removed from SNOMED CT AU

Example: If version 20250531 has count 508, and the next version 20250630 has count 353, the cell showing 353 will be highlighted in red to indicate a drop.

```
Date:      20250531  20250630  20250731  ...
ValueSet A:   508   353(red)   350(red)  ...  (counts dropped in next versions)
ValueSet B:    50      50        52     ...   (no drops)
```

## Logging

Logs are written to `data/vs-differ/logs/vs-differ.log` and include:
- Package scanning progress
- IG processing information
- Version mismatch warnings (when requested version differs from server's actual version)
- Expansion errors or unusual formats

Example warning:
```
WARNING: Version mismatch for https://healthterminologies.gov.au/fhir/ValueSet/example: 
requested 20250531 but server used http://snomed.info/sct|.../version/20260131
```

## Examples

### Basic workflow

1. Update `config.json` with your IGs:
```json
{
  "ig": [
    {"id": "hl7.fhir.au.base", "version": "6.0.0"},
    {"id": "hl7.fhir.au.core", "version": "2.0.0"}
  ]
}
```

2. Run the tool:
```bash
python vs_differ.py
```

3. View the reports:
```bash
# Data analysis
less ~/data/vs-differ/vs-diff.tsv

# Visual inspection
open ~/data/vs-differ/vs-diff.html
```

### Running tests

```bash
python -m unittest tests.test_vs_differ -v
```

## Troubleshooting

### No packages found

**Error**: `No packages found for hl7.fhir.au.base#6.0.0`

**Solution**: Ensure FHIR packages are installed in the cache directory:
```bash
# Download packages using fhir-cli (if available)
fhir install hl7.fhir.au.base#6.0.0
```

### Version mismatch warnings

The terminology server may use a different version than requested. Check logs:
```bash
tail ~/data/vs-differ/logs/vs-differ.log
```

This typically occurs when:
- The requested version is not available on the server
- The server defaults to the latest version
- Server configuration overrides version requests

### Empty valueset columns

If a ValueSet doesn't show expansion counts, it's likely because:
- The ValueSet is not from NCTS (filter: `ncts` column != "yes")
- The expansion request failed (check logs)
- The valueset contains no SNOMED codes in the selected version

## Architecture

- **vs_differ.py**: Main module with core functions
- **config.json**: Configuration file
- **tests/test_vs_differ.py**: Unit and integration tests

## Key Functions

- `expand_valueset_count()`: Query terminology server for expansion count
- `build_rows()`: Process valuesets and fetch counts
- `write_tsv()`: Generate TSV report
- `write_html()`: Generate HTML report with trending analysis
- `get_trending_status()`: Detect count decreases for highlighting

## Contributing

Run tests before committing:
```bash
python -m unittest discover -s tests
```

## License

See LICENSE file for details.

## Support

For issues or questions, please file an issue on GitHub.
