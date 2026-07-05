# Hybrid PDF Text Extraction & OCR Pipeline

A hybrid local+cloud pipeline for extracting text from multi-page PDFs. Digitally-born pages are processed locally via PyMuPDF with automatic table reconstruction. Scanned/image pages are routed to the **Mistral OCR API** (`mistral-ocr-latest`).

## Quick Start

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install PyMuPDF mistralai pypdf
export MISTRAL_API_KEY="your_key_here"
python orchestrator.py bank_statement.pdf
```

## CLI Usage

```
python orchestrator.py [PDF] [options]

Arguments:
  pdf                    Source PDF file (default: bank_statement.pdf)

Options:
  -o, --output FILE      Output markdown file (default: final_clean_statement.md)
  -w, --workspace DIR    Workspace directory (default: ./pipeline_workspace)
  --batch                Use Batch API for OCR (50% cheaper, async job queue)
  --sort-by-date         Sort table rows chronologically by detected date column
  --force                Force re-extraction, ignore all caches and OCR log
  --pages PAGES          Process only specific pages: "61" / "61,65,70" / "61-70"
```

### Examples

```bash
python orchestrator.py statement.pdf                     # Full pipeline
python orchestrator.py statement.pdf --batch             # Batch mode (50% cheaper)
python orchestrator.py statement.pdf --sort-by-date      # Sort tables by date
python orchestrator.py statement.pdf --pages 61          # Reprocess only page 61
python orchestrator.py statement.pdf --force             # Re-extract everything
```

## Pipeline Flow

```
Source PDF
    │
    ▼
┌─────────────────────┐
│ 1. PDF Splitter     │  Splits into page_001.pdf ... page_N.pdf
│    (idempotent)     │  Skips if split_pages/ already has correct count
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│ 2. Page Analysis    │  Per page: local PyMuPDF extraction → table detection
│                     │  Falls back to Mistral OCR if no native text layer
└─────────────────────┘
    │
    ├── Digital page ──► Local: table reconstruction → cache/page_NNN.md
    │
    └── Scanned page ──► Cloud: Mistral OCR API → cache/page_NNN.md
    │
    ▼
┌─────────────────────┐
│ 3. Aggregation      │  Merges all page_NNN.md → final output file
└─────────────────────┘
```

## Key Features

### Table Reconstruction
- Detects tabular data and outputs proper `| col | col |` markdown tables
- Merges fragmented cells: `0.` + `00` → `0.00`, `38.4` + `%` → `38.4 %`
- Separates header/metadata blocks from table data
- Handles mixed-content pages (table + summary section)

### Smart Column Detection
- Differentiates true side-by-side tables from single tables with indentation
- Only uses column-major sort when 2-3 dominant columns (≥3 blocks each) have a wide gap
- Otherwise preserves reading order (Y-first sort)

### OCR with Retry & Logging
- Two-step: `files.upload_async()` → `ocr.process_async(file_id=...)`
- Retries transient "file not found" errors (code 3310) with backoff
- `ocr_log.json` tracks successful API calls → skips on re-run
- Batch mode (`--batch`) for 50% cost savings

### Caching & Idempotency
- **Split**: skips if `split_pages/` has correct page count
- **Local**: skips if `.md` cache exists with valid content
- **OCR**: skips if page is logged as "success" in `ocr_log.json`
- `--force` overrides all caches; `--pages 61` reprocesses only specific pages

## Project Structure

```
.
├── orchestrator.py          # Master pipeline script
├── config.py                # API key validation
├── README.MD
├── requirements.txt
├── bank_statement.pdf       # Input PDF
├── final_clean_statement.md # Output markdown
└── pipeline_workspace/      # Working directory (created at runtime)
    ├── split_pages/         # Single-page PDFs (page_001.pdf ...)
    ├── ocr_cache/           # Individual page markdown buffers (.md)
    └── ocr_log.json         # OCR tracking: {"page_002.pdf": "success", ...}
```

## Requirements

- **OS**: Linux (kernel 6.x+)
- **Python**: 3.12+
- **Packages**: PyMuPDF, mistralai, pypdf
- **API**: Mistral API key (`export MISTRAL_API_KEY="..."`)