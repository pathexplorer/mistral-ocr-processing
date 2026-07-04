import os
import sys

def validate_api_key() -> str:
    """
    Validates that the MISTRAL_API_KEY environment variable is present and not empty.
    Returns the clean API key string if valid, otherwise terminates execution.
    """
    api_key = os.getenv("MISTRAL_API_KEY")
    
    # Check if the environment variable exists
    if api_key is None:
        print("[FATAL] Security Validation Failed: 'MISTRAL_API_KEY' environment variable is missing.", file=sys.stderr)
        print("[HINT] Please set it in your Linux terminal before running the script: export MISTRAL_API_KEY='your_key'", file=sys.stderr)
        sys.exit(1)
        
    # Check if the variable contains only spaces or is empty
    clean_key = api_key.strip()
    if not clean_key:
        print("[FATAL] Security Validation Failed: 'MISTRAL_API_KEY' environment variable is found but it is empty.", file=sys.stderr)
        print("[HINT] Ensure you didn't pass an empty string: export MISTRAL_API_KEY=''", file=sys.stderr)
        sys.exit(1)
        
    print("[SUCCESS] API Configuration Validation: Token found and verified.")
    return clean_key

if __name__ == "__main__":
    # Self-test block to check configuration locally
    print("[INFO] Running local configuration self-test...")
    validate_api_key()