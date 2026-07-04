import os
import sys
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
    
    os.makedirs(output_directory, exist_ok=True)
    reader = PdfReader(source_pdf_path)
    total_pages = len(reader.pages)
    
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
def extract_native_text_or_mark_for_ocr(pdf_path: str, cache_dir: str) -> bool:
    base_name = os.path.basename(pdf_path)
    md_filename = os.path.splitext(base_name)[0] + ".md"
    md_filepath = os.path.join(cache_dir, md_filename)
    
    if os.path.exists(md_filepath):
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
    # Collect unique left-edge (x0) coordinates to find column boundaries
    x0_values = sorted(set(b[0] for b in blocks))
    
    # Find the largest horizontal gap between consecutive x0 clusters
    max_gap = 0
    for i in range(1, len(x0_values)):
        gap = x0_values[i] - x0_values[i-1]
        if gap > max_gap:
            max_gap = gap
    
    # Heuristic: if largest gap > 25% of page width → truly separate tables → X-first sort
    # Otherwise → one logical table with columns → Y-first sort (preserve row order)
    COLUMN_GAP_THRESHOLD = 0.25
    
    if max_gap > page_width * COLUMN_GAP_THRESHOLD:
        blocks.sort(key=lambda b: (b[0], b[1]))   # X-first: separate tables
    else:
        blocks.sort(key=lambda b: (b[1], b[0]))   # Y-first: single table, row order
    
    extracted_text_pieces = []
    for b in blocks:
        text_content = b[4].strip()
        if text_content:
            extracted_text_pieces.append(text_content)
            
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
async def process_ocr_page(client: Mistral, file_path: str, cache_dir: str, semaphore: asyncio.Semaphore):
    base_name = os.path.basename(file_path)
    md_filename = os.path.splitext(base_name)[0] + ".md"
    md_filepath = os.path.join(cache_dir, md_filename)
    
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
            print(f"[API SUCCESS] OCR completed for {base_name}")
                
        except Exception as e:
            print(f"[API ERROR] Failed to process {base_name}: {e}", file=sys.stderr)
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
async def main_pipeline(pdf_source: str, workspace_dir: str, output_md: str, use_batch: bool = False):
    # EXPLICIT GATE: Validate configuration BEFORE processing any heavy data files
    api_key = validate_api_key()
    
    pages_dir = os.path.join(workspace_dir, "split_pages")
    cache_dir = os.path.join(workspace_dir, "ocr_cache")
    os.makedirs(cache_dir, exist_ok=True)
    
    # 1. Split PDF
    total_pages = split_pdf_to_pages(pdf_source, pages_dir)
    
    # 2. Analyze all pages locally
    print("[INFO] Analyzing text layer status across all pages...")
    all_pdf_files = sorted(glob.glob(os.path.join(pages_dir, "page_*.pdf")))
    
    ocr_needed_list = []
    native_text_count = 0
    
    for pdf_file in all_pdf_files:
        try:
            has_text = extract_native_text_or_mark_for_ocr(pdf_file, cache_dir)
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
        print(f"[LOG] Pages queued for OCR routing: {[os.path.basename(p) for p in ocr_needed_list]}")
        
        async with Mistral(api_key=api_key) as client:
            if use_batch:
                # Batch mode: one job, 50% cheaper, async queue
                await process_pages_batch(client, ocr_needed_list, cache_dir)
            else:
                # Real-time mode: concurrent API calls with semaphore
                semaphore = asyncio.Semaphore(5)
                tasks = [
                    process_ocr_page(client, page, cache_dir, semaphore)
                    for page in ocr_needed_list
                ]
                await asyncio.gather(*tasks)
            
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
    args = parser.parse_args()

    try:
        asyncio.run(main_pipeline(args.pdf, args.workspace, args.output, use_batch=args.batch))
    except KeyboardInterrupt:
        print("\n[INFO] Pipeline paused by user command.")
    except Exception as err:
        print(f"[FATAL] Pipeline broken: {err}", file=sys.stderr)
        sys.exit(1)