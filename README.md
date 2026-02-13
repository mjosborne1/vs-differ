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
- **Interactive Web Dashboard**: Modern tabbed interface with data table and three chart views
- **Chart Visualization**: Separate charts for low (<1K), medium (1K-50K), and high (>50K) count valuesets with linear scaling
- **Interactive Tooltips**: Hover over chart lines to see valueset names and data points to see counts
- **Version Filtering**: Filter results to specific SNOMED CT AU versions using `--sctver` option
- **Dev Mode**: Fast regeneration from cached TSV data for rapid development and testing
- **GitHub Pages Ready**: Generates deployment-ready web folder with index.html
- **Responsive Design**: Wide viewport support (98% width) for maximum data visibility
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
  "dev": false,
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
| `dev` | Dev mode: if true and TSV exists, skip FHIR processing and regenerate HTML/charts from TSV | `false` |
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

### Filter to specific SNOMED CT AU version

```bash
python vs_differ.py -v 20260131
```

This filters results to only include SNOMED CT AU versions up to and including the specified version (YYYYMMDD format).

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
- `-v, --sctver`: Latest SNOMED CT AU version (YYYYMMDD format). Versions newer than this will be filtered out. If not specified, versions more than 7 days in the future will be removed.

## Output Files

### TSV Report

**File**: `~/data/vs-differ/vs-diff.tsv`

Tab-separated values file with columns:
- `ValueSet Name`: Human-readable ValueSet name
- `ValueSet URL`: The official URL of the ValueSet
- `Structure Definitions`: List of StructureDefinitions using this ValueSet
- Version columns (one per SNOMED CT AU version): Expansion counts or empty if not applicable

### Web Dashboard

**Folder**: `~/data/vs-differ/web/`

Interactive web dashboard with:
- **index.html**: Main dashboard with tabbed interface
- **table.html**: Data table showing all valuesets and counts with red highlighting for decreases
- **chart-low.html**: Chart for valuesets with <1,000 elements
- **chart-medium.html**: Chart for valuesets with 1,000-50,000 elements
- **chart-high.html**: Chart for valuesets with >50,000 elements

#### Dashboard Features

- **Tabbed navigation**: Switch between data table and different chart views
- **Interactive tooltips**: Hover over chart lines to see valueset names, hover over data points to see counts
- **Responsive design**: Wide viewport (98% width) and tall iframe (1600px) for maximum visibility
- **Modern styling**: Blue color scheme (#1D7DB3) with gradient headers
- **Version range display**: Shows oldest to newest versions in the header
- **GitHub link**: Footer includes link to the repository

### Chart Visualization

Charts use linear scaling with:
- X-axis: SNOMED CT AU versions (oldest to newest)
- Y-axis: Element count
- Color-coded lines: Each valueset gets a unique color
- Interactive legend: Shows all valuesets with color indicators
- Dynamic height: SVG height adjusts to accommodate all legend items

### GitHub Pages Deployment

The `web/` folder is ready for deployment to GitHub Pages:

1. Copy contents to your GitHub Pages repository
2. Push to GitHub
3. Enable GitHub Pages in repository settings
4. Access at `https://yourusername.github.io/yourrepo/`

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
    {"id": "hl7.fhir.au.core", "version": "2.0.0"},
    {"id": "hl7.fhir.au.ereq", "version": "dev"},
    {"id": "hl7.fhir.au.ps", "version": "dev"}
  ],
  "versions_to_compare": 12
}
```

2. Run the tool with version filter:
```bash
python vs_differ.py -v 20260131
```

3. View the web dashboard:
```bash
open ~/data/vs-differ/web/index.html
```

4. Or view individual files:
```bash
# Data analysis
less ~/data/vs-differ/vs-diff.tsv

# Data table
open ~/data/vs-differ/web/table.html

# Charts
open ~/data/vs-differ/web/chart-low.html
```

### Dev mode workflow

Enable dev mode for rapid iteration without re-processing FHIR packages:

1. Set `"dev": true` in config.json

2. First run creates the TSV file:
```bash
python vs_differ.py -v 20260131
# Processes FHIR packages, calls terminology server, generates TSV + web files
```

3. Subsequent runs use cached TSV:
```bash
python vs_differ.py -v 20260131
# Skips FHIR processing, regenerates web files from TSV in seconds
```

This is useful when:
- Tweaking chart colors or styling
- Adjusting dashboard layout
- Testing different visualizations
- Making quick changes without waiting for API calls

### Deploying to GitHub Pages

1. Create a GitHub repository for deployment
2. Copy web folder contents:
```bash
cp -r ~/data/vs-differ/web/* /path/to/your/github-pages-repo/
```

3. Commit and push:
```bash
cd /path/to/your/github-pages-repo/
git add .
git commit -m "Update NCTS ValueSet analysis"
git push
```

4. Access at `https://yourusername.github.io/yourrepo/`

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
- The ValueSet is not from NCTS (only processes NCTS valuesets)
- The expansion request failed (check logs)
- The valueset contains no SNOMED codes in the selected version

### Dev mode not using cached TSV

**Issue**: Dev mode still processes FHIR packages instead of using TSV

**Solutions**:
- Verify `"dev": true` is set in config.json
- Ensure TSV file exists at the expected path (check `data_folder` and `output_filename` in config)
- Check file permissions on the TSV file
- Review logs for "DEV MODE: Using existing TSV file" message

### Charts not displaying

**Issue**: Charts show empty or don't render

**Solutions**:
- Check browser console for JavaScript errors
- Ensure all chart files were generated (chart-low.html, chart-medium.html, chart-high.html)
- Verify valuesets exist in each count range
- Check that SVG height accommodates all legend items

## Architecture

- **vs_differ.py**: Main module with core functions
- **config.json**: Configuration file
- **tests/test_vs_differ.py**: Unit and integration tests

## Key Functions

- `expand_valueset_count()`: Query terminology server for expansion count
- `build_rows()`: Process valuesets and fetch counts
- `read_tsv_data()`: Load cached TSV data in dev mode
- `write_tsv()`: Generate TSV report
- `write_html()`: Generate HTML data table with trending analysis
- `write_chart_html()`: Generate three chart HTML files by count ranges
- `create_web_folder()`: Create deployment-ready web folder with index.html
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
