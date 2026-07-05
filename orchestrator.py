import os
import sys
import glob
import json
import asyncio
import argparse
import traceback
from datetime import date
import fitz  # PyMuPDF
from pypdf import PdfReader, PdfWriter
from mistralai.client import Mistral
from config import validate_api_key

# ==========================================
# Pricing constants (Mistral OCR, USD per page)
# Source: https://mistral.ai/pricing/api/
# Update if Mistral changes pricing
# ==========================================
OCR_PRICE_PER_PAGE = 4.0 / 1000        # $4.00 per 1000 pages (standard)
OCR_PRICE_BATCH_PER_PAGE = 2.0 / 1000   # 50% off for batch mode

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
# КРОК 2: Аналізатор текстового шару (IBM Docling)
# ==========================================
import os as _os
_docling_conv = None

def _get_docling_converter():
    """Lazy-init Docling converter (CPU-only, digital PDFs — no OCR)."""
    global _docling_conv
    if _docling_conv is None:
        _os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # force CPU
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        pipeline_opts = PdfPipelineOptions()
        pipeline_opts.do_ocr = False  # digital PDFs only
        _docling_conv = DocumentConverter(
            format_options={"pdf": PdfFormatOption(pipeline_options=pipeline_opts)}
        )
    return _docling_conv

def extract_native_text_or_mark_for_ocr(pdf_path: str, cache_dir: str, sort_by_date: bool = False, force: bool = False) -> bool:
    base_name = os.path.basename(pdf_path)
    md_filename = os.path.splitext(base_name)[0] + ".md"
    md_filepath = os.path.join(cache_dir, md_filename)
    
    if os.path.exists(md_filepath) and not force:
        with open(md_filepath, "r", encoding="utf-8") as f:
            cached = f.read().strip()
        if cached and "## ERROR EXTRACTING DATA" not in cached and len(cached) > 20:
            return True

    # Quick pre-check: does the PDF have any native text at all?
    doc = fitz.open(pdf_path)
    if len(doc) == 0:
        doc.close()
        return False
    page = doc[0]
    if page is None:
        doc.close()
        return False
    raw_text = page.get_text("text").strip()
    doc.close()
    
    if not raw_text:
        return False  # no text → route to OCR

    # Use Docling for layout-aware extraction
    try:
        conv = _get_docling_converter()
        result = conv.convert(pdf_path)
        final_text = result.document.export_to_markdown()
    except Exception as e:
        print(f"[WARN] Docling failed for {base_name}: {e}", file=sys.stderr)
        return False  # fall through to OCR
    
    if len(final_text.strip()) >= 20:
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
async def main_pipeline(pdf_source: str, workspace_dir: str, output_md: str, use_batch: bool = False, sort_by_date: bool = False, force: bool = False, pages: str = None):
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
    
    # If --pages specified, filter to those pages only and force re-extract them
    target_pages = None
    if pages:
        target_pages = set()
        for part in pages.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                target_pages.update(range(int(start), int(end) + 1))
            else:
                target_pages.add(int(part))
        all_pdf_files = [
            f for f in all_pdf_files
            if int(os.path.basename(f).split("_")[1].split(".")[0]) in target_pages
        ]
        print(f"[INFO] --pages filter: processing only {sorted(target_pages)} ({len(all_pdf_files)} files)")
        force = True  # always force re-extract for explicitly requested pages
    
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
    ocr_processed_count = 0
    if ocr_needed_list:
        # Filter out pages already successfully OCR'd (unless --force)
        if not force:
            already_done = [p for p in ocr_needed_list if ocr_log.get(os.path.basename(p)) == "success"]
            if already_done:
                print(f"[SKIP] {len(already_done)} pages already OCR'd (from log): {[os.path.basename(p) for p in already_done]}")
                ocr_needed_list = [p for p in ocr_needed_list if ocr_log.get(os.path.basename(p)) != "success"]
        
        if ocr_needed_list:
            print(f"[LOG] Pages queued for OCR routing: {[os.path.basename(p) for p in ocr_needed_list]}")
            ocr_processed_count = len(ocr_needed_list)
            
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

    # --- Cost tracking ---
    cost_log_path = os.path.join(workspace_dir, "cost_tracker.json")
    cost_data = {"by_date": {}, "all_time": {"pages": 0, "batch_pages": 0, "cost": 0.0}}
    if os.path.exists(cost_log_path):
        with open(cost_log_path, "r", encoding="utf-8") as f:
            cost_data = json.load(f)
    
    today = str(date.today())
    cost_data["by_date"].setdefault(today, {"pages": 0, "batch_pages": 0})
    cost_data.setdefault("all_time", {"pages": 0, "batch_pages": 0, "cost": 0.0})
    
    ocr_processed = ocr_processed_count
    
    if ocr_processed > 0:
        if use_batch:
            cost_data["by_date"][today]["batch_pages"] += ocr_processed
            cost_data["all_time"]["batch_pages"] += ocr_processed
            run_cost = ocr_processed * OCR_PRICE_BATCH_PER_PAGE
        else:
            cost_data["by_date"][today]["pages"] += ocr_processed
            cost_data["all_time"]["pages"] += ocr_processed
            run_cost = ocr_processed * OCR_PRICE_PER_PAGE
        
        cost_data["all_time"]["cost"] += run_cost
        
        with open(cost_log_path, "w", encoding="utf-8") as f:
            json.dump(cost_data, f, indent=2)
        
        mode_label = "batch" if use_batch else "standard"
        print(f"[COST] This run: {ocr_processed} OCR pages ({mode_label}) → ${run_cost:.4f}")
    
    td = cost_data["by_date"][today]
    today_pages = td["pages"] + td["batch_pages"]
    today_cost = td["pages"] * OCR_PRICE_PER_PAGE + td["batch_pages"] * OCR_PRICE_BATCH_PER_PAGE
    if today_pages > 0:
        print(f"[COST] Today ({today}): {today_pages} pages → ${today_cost:.4f}")
    at = cost_data["all_time"]
    if at["pages"] + at["batch_pages"] > 0:
        print(f"[COST] All-time: {at['pages'] + at['batch_pages']} pages → ${at['cost']:.4f}")
            
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
    parser.add_argument(
        "--pages", type=str, default=None,
        help="Process only specific pages (e.g. '61' or '61,65,70' or '61-70')"
    )
    args = parser.parse_args()

    try:
        asyncio.run(main_pipeline(args.pdf, args.workspace, args.output,
                                  use_batch=args.batch, sort_by_date=args.sort_by_date,
                                  force=args.force, pages=args.pages))
    except KeyboardInterrupt:
        print("\n[INFO] Pipeline paused by user command.")
    except Exception as err:
        print(f"[FATAL] Pipeline broken: {err}", file=sys.stderr)
        sys.exit(1)