import os
import sys
import glob
import asyncio
from mistralai import Mistral

async def process_single_page(client: Mistral, file_path: str, output_dir: str, semaphore: asyncio.Semaphore) -> str:
    """
    Sends a single high-quality PDF page to the Mistral OCR API.
    Uses a semaphore to limit concurrent network requests and avoid rate limits.
    """
    # Generate output path for individual page results
    base_name = os.path.basename(file_path)
    md_filename = os.path.splitext(base_name)[0] + ".md"
    md_filepath = os.path.join(output_dir, md_filename)
    
    # If already processed in a previous run, skip network request (idempotency)
    if os.path.exists(md_filepath):
        print(f"[SKIP] Page {base_name} already processed.")
        with open(md_filepath, "r", encoding="utf-8") as f:
            return f.read()

    async with semaphore:
        print(f"[PROCESSING] Sending {base_name} to Mistral OCR API...")
        try:
            # We open the file in standard binary mode, the SDK handles the upload stream
            with open(file_path, "rb") as pdf_file:
                # Using the specialized, heavy-compute OCR endpoint
                ocr_response = await client.ocr.process_async(
                    model="mistral-ocr-latest",
                    document={"type": "pdf", "file": pdf_file}
                )
                
                markdown_text = ocr_response.markdown
                
                # Cache the individual result immediately to save progress
                with open(md_filepath, "w", encoding="utf-8") as md_file:
                    md_file.write(markdown_text)
                    
                print(f"[SUCCESS] Received OCR data for {base_name}")
                return markdown_text
                
        except Exception as e:
            print(f"[ERROR] Failed to process page {base_name}: {str(e)}", file=sys.stderr)
            return f"\n\n## ERROR PROCESSING PAGE {base_name} ##\n\n"

async def main_pipeline(input_dir: str, cache_dir: str, final_output_path: str):
    """
    Orchestrates the parallel processing of split PDF pages and aggregates results.
    """
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        print("[FATAL] Environment variable 'MISTRAL_API_KEY' is missing.", file=sys.stderr)
        print("Please run: export MISTRAL_API_KEY='your_key'", file=sys.stderr)
        sys.exit(1)
        
    os.makedirs(cache_dir, exist_ok=True)
    
    # Locate all split pages and sort them to guarantee correct chronological order
    pdf_pages = sorted(glob.glob(os.path.join(input_dir, "page_*.pdf")))
    if not pdf_pages:
        print(f"[ERROR] No source pages found in '{input_dir}'. Run the splitter first.", file=sys.stderr)
        return

    # Limit concurrency to 5 parallel workers to prevent HTTP 429 (Too Many Requests)
    # and avoid flooding your Linux network stack
    semaphore = asyncio.Semaphore(5)
    
    # Initialize asynchronous Mistral client
    async with Mistral(api_key=api_key) as client:
        print(f"[START] Batch processing {len(pdf_pages)} pages via API...")
        
        tasks = [
            process_single_page(client, page, cache_dir, semaphore)
            for page in pdf_pages
        ]
        
        # Gather all responses maintaining the original list order
        all_markdown_pages = await asyncio.gather(*tasks)
        
        # Merge all markdown strings into one consolidated master file
        print(f"[COMPILING] Aggregating results into {final_output_path}...")
        with open(final_output_path, "w", encoding="utf-8") as master_file:
            for idx, page_content in enumerate(all_markdown_pages):
                master_file.write(f"\n")
                master_file.write(page_content)
                master_file.write("\n\n<div style='page-break-after: always;'></div>\n\n")
                
        print(f"[FINISHED] Master OCR Markdown file generated successfully: {final_output_path}")

if __name__ == "__main__":
    SRC_PAGES_DIR = "./extracted_pages"      # Folder from the first script
    CACHE_DIR = "./ocr_cache"                # Intermediate markdown storage
    FINAL_MARKDOWN = "./final_statement.md"  # The 100% accurate output file
    
    # Run the async event loop
    asyncio.run(main_pipeline(SRC_PAGES_DIR, CACHE_DIR, FINAL_MARKDOWN))