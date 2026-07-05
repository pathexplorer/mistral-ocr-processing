import os
import sys
import re
import glob
import json
import asyncio
import argparse
import traceback
import fitz  # PyMuPDF
from pypdf import PdfReader, PdfWriter
from mistralai.client import Mistral
# Import the new validation module
from config import validate_api_key

# ==========================================
# КРОК 1: Розбиття PDF на окремі сторінки
# ==========================================
def split_pdf_to_pages(source_pdf_path: str, output_directory: str) -> int:
    if not os.path.exists(source_pdf_path):
        raise FileNotFoundError(f"Source PDF not found at: {source_pdf_path}")
    
    reader = PdfReader(source_pdf_path)
    total_pages = len(reader.pages)
    
    # Idempotency: skip if split pages already exist with correct count
    if os.path.isdir(output_directory):
        existing_pages = glob.glob(os.path.join(output_directory, "page_*.pdf"))
        if len(existing_pages) == total_pages:
            print(f"[SKIP] Split pages already exist in '{output_directory}' ({total_pages} pages). Using cached files.")
            return total_pages
    
    os.makedirs(output_directory, exist_ok=True)
    
    print(f"[INFO] Splitting '{source_pdf_path}' ({total_pages} pages)...")
    padding_length = max(3, len(str(total_pages)))
    
    for page_index in range(total_pages):
        writer = PdfWriter()
        writer.add_page(reader.pages[page_index])
        output_filename = f"page_{str(page_index + 1).zfill(padding_length)}.pdf"
        output_filepath = os.path.join(output_directory, output_filename)
        
        with open(output_filepath, "wb") as f:
            writer.write(f)
            
    print(f"[SUCCESS] Split completed. Files saved to {output_directory}")
    return total_pages

# ==========================================
# КРОК 2: Аналізатор текстового шару
# ==========================================
def extract_native_text_or_mark_for_ocr(pdf_path: str, cache_dir: str, sort_by_date: bool = False, force: bool = False) -> bool:
    base_name = os.path.basename(pdf_path)
    md_filename = os.path.splitext(base_name)[0] + ".md"
    md_filepath = os.path.join(cache_dir, md_filename)
    
    if os.path.exists(md_filepath) and not force:
        # Validate cached content — skip only if it contains real text, not an error stub
        with open(md_filepath, "r", encoding="utf-8") as f:
            cached = f.read().strip()
        if cached and "## ERROR EXTRACTING DATA" not in cached and len(cached) > 20:
            return True
        # Otherwise, stale/error cache — re-extract below

    doc = fitz.open(pdf_path)
    if len(doc) == 0:
        doc.close()
        return False
    page = doc[0]
    if page is None:
        doc.close()
        return False
    
    # Quick pre-check: does the page have ANY selectable text at all?
    raw_text = page.get_text("text").strip()
    
    # Save page dimensions before closing doc (close invalidates C page data)
    page_width = page.rect.width

    blocks = page.get_text("blocks")
    
    if not blocks:
        doc.close()
        return False

    # --- Smart column detection ---
    # Cluster x0 values (±5pt) to find true column boundaries vs indentation
    x0_clusters = {}
    for b in blocks:
        cluster = round(b[0] / 5) * 5  # snap to nearest 5pt grid
        x0_clusters[cluster] = x0_clusters.get(cluster, 0) + 1
    
    # A "dominant column" has 3+ blocks at that X position
    dominant_columns = [x for x, count in x0_clusters.items() if count >= 3]
    dominant_columns.sort()
    
    # Find max gap between dominant columns
    max_gap = 0
    for i in range(1, len(dominant_columns)):
        gap = dominant_columns[i] - dominant_columns[i-1]
        if gap > max_gap:
            max_gap = gap
    
    # Only use X-first sort when there are 2-3 dominant columns with a wide gap
    # (truly separate tables), not scattered indentation variations
    COLUMN_GAP_THRESHOLD = 0.25
    is_multi_column = (
        2 <= len(dominant_columns) <= 3
        and max_gap > page_width * COLUMN_GAP_THRESHOLD
    )
    
    if is_multi_column:
        blocks.sort(key=lambda b: (b[0], b[1]))   # X-first: separate tables
    else:
        blocks.sort(key=lambda b: (b[1], b[0]))   # Y-first: reading order
    
    extracted_text_pieces = []
    for b in blocks:
        text_content = b[4].strip()
        if text_content:
            extracted_text_pieces.append(text_content)

    # --- Detect tabular structure ---
    # Separate metadata blocks (1 cell) from potential table rows (2+ cells)
    # 1-cell blocks are typically headers/footers, not table data
    cells_per_block = [
        len([c.strip() for c in txt.split('\n') if c.strip()])
        for txt in extracted_text_pieces
    ]
    
    multi_cell_indices = [i for i, c in enumerate(cells_per_block) if c >= 2]
    if multi_cell_indices:
        multi_cell_counts = [cells_per_block[i] for i in multi_cell_indices]
        most_common_cols = max(set(multi_cell_counts), key=multi_cell_counts.count)
        consistent_count = sum(1 for c in multi_cell_counts if c == most_common_cols)
        # Require ≥3 columns, ≥5 dominant-format rows, and ≥35% consistency
        # (35% handles pages with a main table + small summary/footer section)
        is_tabular = (
            most_common_cols >= 3 and
            consistent_count >= 5 and
            consistent_count >= len(multi_cell_counts) * 0.35
        )
    else:
        is_tabular = False

    if is_tabular:
        # Split: dominant-format blocks (±1 tolerance) → table, others → plain text
        plain_parts = []
        table_parts = []
        for i, txt in enumerate(extracted_text_pieces):
            if abs(cells_per_block[i] - most_common_cols) <= 1:
                table_parts.append(txt)
            else:
                plain_parts.append(txt)

        # Build markdown table from multi-cell blocks
        rows = []
        for txt in table_parts:
            cells = [c.strip() for c in txt.split('\n') if c.strip()]
            if len(cells) > most_common_cols:
                cells = cells[:most_common_cols]
            elif len(cells) < most_common_cols:
                cells += [''] * (most_common_cols - len(cells))
            rows.append(cells)

        # Merge fragmented numeric cells: "0." + "00" → "0.00", "38.4" + "%" → "38.4 %"
        merged_rows = []
        for row in rows:
            merged = []
            skip_next = False
            for i in range(len(row)):
                if skip_next:
                    skip_next = False
                    continue
                cell = row[i]
                # Merge: left cell ends with '.' or ',' and right cell is pure digits
                if i + 1 < len(row) and (cell.endswith('.') or cell.endswith(',')):
                    right = row[i + 1]
                    if right.isdigit() or (right.endswith('%') and right[:-1].strip().isdigit()):
                        cell = cell + right
                        skip_next = True
                # Merge: right cell is '%' symbol alone
                elif i + 1 < len(row) and row[i + 1].strip() == '%':
                    cell = cell + ' %'
                    skip_next = True
                merged.append(cell)
            merged_rows.append(merged)

        # Recalculate column count after merging
        final_cols = max(len(r) for r in merged_rows)

        # --- Optional: sort rows by date column ---
        if sort_by_date and len(merged_rows) > 1:
            # Detect date column: first column matching DD.MM.YYYY or YYYY-MM-DD
            date_pattern = re.compile(r'^\d{2}\.\d{2}\.\d{4}$|^\d{4}-\d{2}-\d{2}$')
            date_col = None
            for col_idx in range(final_cols):
                matches = 0
                for row in merged_rows:
                    if col_idx < len(row) and date_pattern.match(row[col_idx]):
                        matches += 1
                if matches >= len(merged_rows) * 0.5:  # 50%+ rows have dates here
                    date_col = col_idx
                    break

            if date_col is not None:
                from datetime import datetime
                def parse_date(row):
                    val = row[date_col] if date_col < len(row) else ''
                    for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
                        try:
                            return datetime.strptime(val, fmt)
                        except ValueError:
                            continue
                    return datetime.min  # unparseable → sort first

                header = merged_rows[0]
                data_rows = merged_rows[1:]
                data_rows.sort(key=parse_date)
                merged_rows = [header] + data_rows

        # Normalize to final column count
        for r in merged_rows:
            while len(r) < final_cols:
                r.append('')

        lines = []
        # Plain text parts (metadata/headers) go before the table
        if plain_parts:
            lines.append('\n\n'.join(plain_parts))
            lines.append('')  # blank separator
        # Header row (first data row used as header)
        lines.append('| ' + ' | '.join(merged_rows[0]) + ' |')
        # Separator
        lines.append('| ' + ' | '.join(['---'] * final_cols) + ' |')
        # Data rows
        for row in merged_rows[1:]:
            lines.append('| ' + ' | '.join(row) + ' |')

        final_text = '\n'.join(lines)
    else:
        final_text = "\n\n".join(extracted_text_pieces)

    doc.close()
    
    # Lower threshold (20 chars) catches sparse pages like headers/cover pages.
    # Also: if PyMuPDF found raw text even below threshold, prefer local extraction
    # over costly OCR for pages with just a few words.
    if len(final_text.strip()) >= 20 or (len(raw_text) > 0 and len(final_text.strip()) > 0):
        with open(md_filepath, "w", encoding="utf-8") as f:
            f.write(final_text)
        return True
        
    return False

# ==========================================
# КРОК 3: Асинхронний виклик Mistral OCR
# ==========================================
async def process_ocr_page(client: Mistral, file_path: str, cache_dir: str, semaphore: asyncio.Semaphore, ocr_log: dict):
    base_name = os.path.basename(file_path)
    md_filename = os.path.splitext(base_name)[0] + ".md"
    md_filepath = os.path.join(cache_dir, md_filename)
    
    # Skip if already successfully OCR'd (tracked in log) and cache file exists
    if ocr_log.get(base_name) == "success" and os.path.exists(md_filepath):
        print(f"[SKIP] {base_name} already OCR'd successfully (from log).")
        return

    if os.path.exists(md_filepath):
        return

    async with semaphore:
        print(f"[API CALL] Sending scanned page {base_name} to Mistral OCR...")
        try:
            # Step 1: Upload PDF bytes to get a file_id
            with open(file_path, "rb") as pdf_file:
                pdf_bytes = pdf_file.read()

            uploaded = await client.files.upload_async(
                file={
                    "file_name": base_name,
                    "content": pdf_bytes,
                },
                purpose="ocr",
            )

            # Brief delay — lets the file propagate on Mistral's servers before OCR references it
            await asyncio.sleep(0.5)

            # Step 2: Pass file_id to OCR, with retry for transient "file not found" errors
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    ocr_response = await client.ocr.process_async(
                        model="mistral-ocr-latest",
                        document={
                            "type": "file",
                            "file_id": uploaded.id,
                        },
                    )
                    break  # success, exit retry loop
                except Exception as retry_err:
                    err_str = str(retry_err)
                    # Only retry on "file not found/expired" (code 3310), not on other errors
                    if "3310" in err_str or "could not be found" in err_str:
                        if attempt < max_retries - 1:
                            wait = 1.0 * (attempt + 1)
                            print(f"[RETRY] File not ready for {base_name}, waiting {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                    raise  # re-raise if not retryable or out of attempts

            with open(md_filepath, "w", encoding="utf-8") as f:
                f.write(ocr_response.pages[0].markdown)
            ocr_log[base_name] = "success"
            print(f"[API SUCCESS] OCR completed for {base_name}")
                
        except Exception as e:
            print(f"[API ERROR] Failed to process {base_name}: {e}", file=sys.stderr)
            ocr_log[base_name] = "error"
            # Write error stub to prevent pipeline interruption
            with open(md_filepath, "w", encoding="utf-8") as f:
                f.write(f"\n\n## ERROR EXTRACTING DATA FROM {base_name} ##\n\n")

# ==========================================
# КРОК 3b: Batch OCR (50% cheaper, async job queue)
# ==========================================
async def process_pages_batch(client: Mistral, ocr_pages: list, cache_dir: str):
    """Process multiple pages via Mistral Batch API at half the cost."""
    page_count = len(ocr_pages)
    print(f"[BATCH] Uploading {page_count} pages to Mistral for batch processing...")

    # Step 1: Upload all PDFs, collect file_ids keyed by base name
    page_file_ids = {}
    for page_path in ocr_pages:
        base_name = os.path.basename(page_path)
        md_filename = os.path.splitext(base_name)[0] + ".md"
        md_filepath = os.path.join(cache_dir, md_filename)
        if os.path.exists(md_filepath):
            continue  # already cached, skip upload

        with open(page_path, "rb") as f:
            uploaded = await client.files.upload_async(
                file={"file_name": base_name, "content": f.read()},
                purpose="ocr",
            )
        page_file_ids[base_name] = uploaded.id
        print(f"[BATCH] Uploaded {base_name}")

    if not page_file_ids:
        print("[BATCH] All pages already cached — nothing to do.")
        return

    # Step 2: Build batch requests
    batch_requests = []
    for base_name, file_id in page_file_ids.items():
        batch_requests.append({
            "custom_id": base_name,
            "body": {
                "model": "mistral-ocr-latest",
                "document": {"type": "file", "file_id": file_id},
            },
        })

    # Step 3: Submit batch job
    print(f"[BATCH] Submitting batch job with {len(batch_requests)} requests...")
    job = client.batch.jobs.create(
        endpoint="/v1/ocr",
        model="mistral-ocr-latest",
        requests=batch_requests,
        timeout_hours=24,
    )
    job_id = job.id
    print(f"[BATCH] Job created: {job_id}, status: {job.status}")

    # Step 4: Poll until complete
    while True:
        await asyncio.sleep(15)
        job = client.batch.jobs.get(job_id=job_id)
        completed = job.completed_requests or 0
        total = job.total_requests or len(batch_requests)
        print(f"[BATCH] {job.status}: {completed}/{total} requests done")

        if job.status in ("SUCCESS", "COMPLETED", "DONE"):
            break
        elif job.status in ("FAILED", "CANCELLED", "EXPIRED"):
            print(f"[BATCH ERROR] Job {job_id} ended with status: {job.status}", file=sys.stderr)
            for base_name in page_file_ids:
                md_filename = os.path.splitext(base_name)[0] + ".md"
                md_filepath = os.path.join(cache_dir, md_filename)
                if not os.path.exists(md_filepath):
                    with open(md_filepath, "w", encoding="utf-8") as f:
                        f.write(f"\n\n## BATCH ERROR: Job {job.status} ##\n\n")
            return

    # Step 5: Parse results from job outputs
    print(f"[BATCH] Job completed! Parsing results...")
    if job.outputs:
        for output in job.outputs:
            custom_id = output.get("custom_id", "unknown")
            response_body = output.get("response", {}).get("body", {})
            pages = response_body.get("pages", [])
            markdown = pages[0].get("markdown", "") if pages else ""

            md_filename = os.path.splitext(custom_id)[0] + ".md"
            md_filepath = os.path.join(cache_dir, md_filename)
            with open(md_filepath, "w", encoding="utf-8") as f:
                f.write(markdown)
            print(f"[BATCH] Saved result for {custom_id}")

    # Handle any pages missing results
    for base_name in page_file_ids:
        md_filename = os.path.splitext(base_name)[0] + ".md"
        md_filepath = os.path.join(cache_dir, md_filename)
        if not os.path.exists(md_filepath):
            with open(md_filepath, "w", encoding="utf-8") as f:
                f.write(f"\n\n## BATCH: No result received for {base_name} ##\n\n")

    print(f"[BATCH] All batch results saved to cache.")

# ==========================================
# КРОК 4: Оркестрація та Агрегація
# ==========================================
async def main_pipeline(pdf_source: str, workspace_dir: str, output_md: str, use_batch: bool = False, sort_by_date: bool = False, force: bool = False):
    # EXPLICIT GATE: Validate configuration BEFORE processing any heavy data files
    api_key = validate_api_key()
    
    pages_dir = os.path.join(workspace_dir, "split_pages")
    cache_dir = os.path.join(workspace_dir, "ocr_cache")
    ocr_log_path = os.path.join(workspace_dir, "ocr_log.json")
    os.makedirs(cache_dir, exist_ok=True)
    
    # Load OCR log: tracks which pages were successfully sent to API
    ocr_log = {}
    if os.path.exists(ocr_log_path):
        with open(ocr_log_path, "r", encoding="utf-8") as f:
            ocr_log = json.load(f)
    
    if force:
        print("[INFO] --force enabled: overwriting all cached results.")
    
    # 1. Split PDF
    total_pages = split_pdf_to_pages(pdf_source, pages_dir)
    
    # 2. Analyze all pages locally
    print("[INFO] Analyzing text layer status across all pages...")
    all_pdf_files = sorted(glob.glob(os.path.join(pages_dir, "page_*.pdf")))
    
    ocr_needed_list = []
    native_text_count = 0
    
    for pdf_file in all_pdf_files:
        try:
            has_text = extract_native_text_or_mark_for_ocr(pdf_file, cache_dir, sort_by_date, force)
        except Exception as e:
            print(f"[ERROR] Failed analyzing {os.path.basename(pdf_file)}: {e}")
            traceback.print_exc()
            has_text = False
        if has_text:
            native_text_count += 1
        else:
            ocr_needed_list.append(pdf_file)
            
    print(f"[ANALYSIS SUMMARY] Total: {total_pages} | Native Text: {native_text_count} | Requires OCR: {len(ocr_needed_list)}")
    
    # Log pages that are going to OCR for transparency
    if ocr_needed_list:
        # Filter out pages already successfully OCR'd (unless --force)
        if not force:
            already_done = [p for p in ocr_needed_list if ocr_log.get(os.path.basename(p)) == "success"]
            if already_done:
                print(f"[SKIP] {len(already_done)} pages already OCR'd (from log): {[os.path.basename(p) for p in already_done]}")
                ocr_needed_list = [p for p in ocr_needed_list if ocr_log.get(os.path.basename(p)) != "success"]
        
        if ocr_needed_list:
            print(f"[LOG] Pages queued for OCR routing: {[os.path.basename(p) for p in ocr_needed_list]}")
            
            async with Mistral(api_key=api_key) as client:
                if use_batch:
                    await process_pages_batch(client, ocr_needed_list, cache_dir)
                else:
                    semaphore = asyncio.Semaphore(5)
                    tasks = [
                        process_ocr_page(client, page, cache_dir, semaphore, ocr_log)
                        for page in ocr_needed_list
                    ]
                    await asyncio.gather(*tasks)
            
            # Save OCR log after processing
            with open(ocr_log_path, "w", encoding="utf-8") as f:
                json.dump(ocr_log, f, indent=2, ensure_ascii=False)
            
    # 4. Final Aggregation of all generated Markdown files
    print(f"[COMPILING] Merging all page buffers into {output_md}...")
    all_md_files = sorted(glob.glob(os.path.join(cache_dir, "page_*.md")))
    
    with open(output_md, "w", encoding="utf-8") as master_file:
        for idx, md_file in enumerate(all_md_files):
            master_file.write(f"\n")
            with open(md_file, "r", encoding="utf-8") as f:
                master_file.write(f.read())
            master_file.write("\n\n<div style='page-break-after: always;'></div>\n\n")
            
    print(f"[FINISHED] Pipeline executed successfully. Final output: {output_md}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Hybrid PDF OCR pipeline — local extraction + Mistral OCR API"
    )
    parser.add_argument(
        "pdf", nargs="?", default="bank_statement.pdf",
        help="Source PDF file (default: bank_statement.pdf)"
    )
    parser.add_argument(
        "-o", "--output", default="final_clean_statement.md",
        help="Output markdown file (default: final_clean_statement.md)"
    )
    parser.add_argument(
        "-w", "--workspace", default="./pipeline_workspace",
        help="Workspace directory for temp files (default: ./pipeline_workspace)"
    )
    parser.add_argument(
        "--batch", action="store_true",
        help="Use Batch API for OCR (50%% cheaper, async job queue)"
    )
    parser.add_argument(
        "--sort-by-date", action="store_true",
        help="Sort table rows chronologically by detected date column"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-extraction of all pages, ignoring cache and OCR log"
    )
    args = parser.parse_args()

    try:
        asyncio.run(main_pipeline(args.pdf, args.workspace, args.output,
                                  use_batch=args.batch, sort_by_date=args.sort_by_date,
                                  force=args.force))
    except KeyboardInterrupt:
        print("\n[INFO] Pipeline paused by user command.")
    except Exception as err:
        print(f"[FATAL] Pipeline broken: {err}", file=sys.stderr)
        sys.exit(1)