export MISTRAL_API_KEY=""


# Hybrid PDF Text Extraction & OCR Pipeline

A highly optimized, resilient, and cost-effective data engineering pipeline designed to process complex, multi-page financial documents (e.g., bank statements, accounting ledgers) on Linux environments.

The system intelligently routes document pages by performing local geometric layout analysis to extract native text layers instantaneously, fallbacking to the **Mistral OCR Cloud API** (`mistral-ocr-latest`) only when processing raw image scans or uncopyable content.

+-----------------------+
                 |   Source PDF File     |
                 +-----------------------+
                             │
                             ▼
                 +-----------------------+
                 | Step 1: PDF Splitter  |
                 +-----------------------+
                             │
                             ▼
                 +-----------------------+
                 |  Split Pages Directory|
                 |  (page_001.pdf, etc.) |
                 +-----------------------+
                             │
                             ▼
                 +-----------------------+
                 | Step 2: Orchestrator  |
                 |  Layout Analysis &    |
                 |  Geometric Sorting    |
                 +-----------------------+
                             │
    ┌────────────────────────┴────────────────────────┐
    │ [Has Native Text Layer]                         │ [Scanned Image / No Text]
    ▼                                                 ▼
    +-----------------------+                         +-----------------------+
    | Local Extraction      |                         | Async Rate-Limited    |
    | (PyMuPDF Layout-Aware)|                         | Mistral OCR API Call  |
    +-----------------------+                         +-----------------------+
    │                                                 │
    ▼                                                 ▼
    +-----------------------+                         +-----------------------+
    | Cache Directory       |                         | Cache Directory       |
    | (page_001.md)         |                         | (page_002.md)         |
    +-----------------------+                         +-----------------------+
    │                                                 │
    └────────────────────────┬────────────────────────┘
    │
    ▼
    +-----------------------+
    | Step 3: Aggregator    |
    +-----------------------+
    │
    ▼
    +-----------------------+
    | Consolidated Master   |
    | Markdown (.md) Report |
    +-----------------------+

```
### Key Technical Engineering Features:
1. **Denoising & Cost Efficiency:** Analyzes each page locally. If native embedded text matches density criteria (>40 characters), it extracts it via CPU in milliseconds for free, preventing expensive and redundant cloud API resource consumption.
2. **Geometric Block Sorting (Layout-Aware):** Solves the "side-by-side table" and "90-degree rotated page" dilemma. By sorting raw PDF blocks via `(x0, y0)` coordinate structures, it isolates multi-column text horizontally, preventing horizontal mixing of columns before outputting into clean Markdown.
3. **High-Throughput Concurrency:** Utilizes `asyncio` bounded by an operational `Semaphore(5)` to flood the Mistral API concurrently without triggering HTTP `429 Too Many Requests` or blowing local network sockets.
4. **Idempotency & Resilience:** Implements an automated local file cache directory (`ocr_cache`). If the pipeline fails due to network instability or API quota depletion, it skips already compiled pages upon subsequent execution.

---

## Technical Environment Requirements

- **Operating System:** Linux Kubuntu 26.04 / Fedora 44 / Generic Linux Kernel 6.x
- **Runtime:** Python 3.12+
- **Package Management:** `uv` (Fast Python package installer written in Rust)

---

## Installation & Environment Set Up

1. Ensure `uv` is installed on your Linux system.
2. Initialize the isolated virtual environment inside your project directory:
```bash
uv venv --python 3.12
```

1. Activate the environment:

Bash

```
source .venv/bin/activate
```

1. Install optimized structural dependencies:

Bash

```
uv pip install PyMuPDF mistralai pypdf
```

1. Export your private Mistral authentication token to your active shell environment:

Bash

```
export MISTRAL_API_KEY="your_actual_mistral_api_key_here"
```

## Project Structure

Plaintext

```
.
├── orchestrator.py          # Master execution script (Split, Route, Extract, Call API, Merge)
├── bank_statement.pdf       # Sample heavy multi-page source file (input)
├── final_clean_statement.md # Final validated structured markdown context (output)
└── pipeline_workspace/      # Isolated pipeline tracking ecosystem
    ├── split_pages/         # Intermediate single-page PDF partitions
    └── ocr_cache/           # Individual atomic page markdown buffers (.md)
```

## Detailed Module Specifications

### `orchestrator.py`

The code is structured as an enterprise-ready pipeline with explicit exception handlings and validation gates.

- **`split_pdf_to_pages(source_pdf_path, output_directory)`**: Opens stream buffers, decouples individual page vectors, and saves atomic single-page binary payloads to storage while retaining original metadata structures.
- **`extract_native_text_or_mark_for_ocr(pdf_path, cache_dir)`**: Opens a page via `PyMuPDF (fitz)`, retrieves structural layout block tokens, executes spatial geometric matrix sorting, and validates text extraction limits.
- **`process_ocr_page(client, file_path, cache_dir, semaphore)`**: Non-blocking asynchronous function wrapping the official Mistral SDK client with internal task queuing.
- **`main_pipeline(pdf_source, workspace_dir, output_md)`**: Orchestrates data streams, tracks page distributions, profiles step durations, and triggers final clean assembly.

## Execution Guide

To trigger the data processing pipeline, ensure your virtual environment is active, the environment variable is loaded, and run:

Bash

```
uv run python3 orchestrator.py
```

### Expected Output Logs:

Plaintext

```
[INFO] Splitting 'bank_statement.pdf' (180 pages)...
[SUCCESS] Split completed. Files saved to ./pipeline_workspace/split_pages
[INFO] Analyzing text layer status across all pages...
[ANALYSIS SUMMARY] Total: 180 | Native Text: 152 | Requires OCR: 28
[LOG] Pages queued for OCR routing: ['page_005.pdf', 'page_042.pdf', 'page_112.pdf', ...]
[API CALL] Sending scanned page page_005.pdf to Mistral OCR...
[API SUCCESS] OCR completed for page_005.pdf
[COMPILING] Merging all page buffers into final_clean_statement.md
[FINISHED] Pipeline executed successfully. Final output: final_clean_statement.md
```

## LLM Integration & Downstream Cross-Verification

The compiled `final_clean_statement.md` output acts as an optimized, noise-free context file specifically suited for local or API-driven LLM applications.

### Integration with OpenWebUI:

1. Open the **OpenWebUI** dashboard interface on your Kubuntu system.
2. Navigate to **Documents** section or attach the file directly to your chat window (`+` icon).
3. Select your local verification models (e.g., `Gemma 4`, `DeepSeek`, or `Mistral Small`).
4. Execute cross-validation prompts to audit statements without risking document text length truncations or structural hallucinations:

Plaintext

```
Review the attached sanitized markdown bank ledger. Cross-verify the transactional totals against the analytical findings generated by NotebookLM. Highlight any discrepancies in values, dates, or calculations.
```