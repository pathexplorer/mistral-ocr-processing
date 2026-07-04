import os
import sys
from pypdf import PdfReader, PdfWriter

def split_pdf_to_pages(source_pdf_path: str, output_directory: str) -> None:
    """
    Splits a multi-page PDF file into separate single-page PDF files.
    Ensures input file exists and creates output directory if it doesn't.
    """
    # Validation checks
    if not os.path.exists(source_pdf_path):
        raise FileNotFoundError(f"Source PDF file not found at: {source_pdf_path}")
        
    if not source_pdf_path.lower().endswith('.pdf'):
        raise ValueError("The provided source file is not a PDF.")

    try:
        # Create output directory safely
        os.makedirs(output_directory, exist_ok=True)
        
        # Read the source PDF
        reader = PdfReader(source_pdf_path)
        total_pages = len(reader)
        
        print(f"[INFO] Started splitting '{source_pdf_path}' ({total_pages} pages)...")
        
        # Determine padding for filename serialization (e.g., page_001.pdf)
        padding_length = max(3, len(str(total_pages)))
        
        for page_index in range(total_pages):
            writer = PdfWriter()
            writer.add_page(reader.pages[page_index])
            
            # Format output filename: page_001.pdf, page_002.pdf, etc.
            page_number = page_index + 1
            output_filename = f"page_{str(page_number).zfill(padding_length)}.pdf"
            output_filepath = os.path.join(output_directory, output_filename)
            
            # Write single page to disk
            with open(output_filepath, "wb") as output_file:
                writer.write(output_file)
                
        print(f"[SUCCESS] All {total_pages} pages successfully saved to '{output_directory}'.")
        
    except PermissionError:
        print(f"[ERROR] Permission denied when accessing paths: {source_pdf_path} or {output_directory}", file=sys.stderr)
        raise
    except Exception as e:
        print(f"[ERROR] An unexpected error occurred during PDF splitting: {str(e)}", file=sys.stderr)
        raise

if __name__ == "__main__":
    # Example execution configuration
    INPUT_FILE = "bank_statement.pdf"      # Replace with your actual 11MB file path
    OUTPUT_DIR = "./extracted_pages"       # Target directory for single pages
    
    try:
        split_pdf_to_pages(INPUT_FILE, OUTPUT_DIR)
    except Exception as main_error:
        print(f"[FATAL] Pipeline execution terminated: {main_error}", file=sys.stderr)
        sys.exit(1)