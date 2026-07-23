#!/usr/bin/env python3
import os
import sys
import json
import zlib
import base64
import argparse
from pathlib import Path

def pack_files(file_paths: list[str]) -> str:
    """Reads files, compresses them, and encodes as a base64 string."""
    payload_dict = {}
    for path_str in file_paths:
        path = Path(path_str)
        if not path.exists() or not path.is_file():
            print(f"\033[1;33m[ WARNING ]\033[0m Skipping missing/invalid file: {path_str}")
            continue
        
        with open(path, "rb") as f:
            file_bytes = f.read()
        
        payload_dict[str(path)] = base64.b64encode(file_bytes).decode('utf-8')
        print(f"\033[1;34m[ PACKING ]\033[0m {path_str} ({len(file_bytes)} bytes)")

    if not payload_dict:
        print("\033[1;31m[ ERROR ]\033[0m No valid files packed.")
        sys.exit(1)

    json_str = json.dumps(payload_dict)
    compressed = zlib.compress(json_str.encode('utf-8'))
    encoded = base64.b64encode(compressed).decode('utf-8')
    return encoded

def unpack_payload(encoded_payload: str, output_dir: str = "."):
    """Decodes, decompresses, and writes files to disk."""
    try:
        compressed = base64.b64decode(encoded_payload)
        json_str = zlib.decompress(compressed).decode('utf-8')
        payload_dict = json.loads(json_str)
        
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_key=True)

        for filepath, b64_content in payload_dict.items():
            safe_name = Path(filepath).name
            target_path = out_path / safe_name
            file_bytes = base64.b64decode(b64_content)
            with open(target_path, "wb") as f:
                f.write(file_bytes)
            print(f"\033[1;32m[ EXTRACTED ]\033[0m {safe_name} ({len(file_bytes)} bytes)")
    except Exception as e:
        print(f"\033[1;31m[ ERROR ]\033[0m Failed to unpack payload. {str(e)}")
        sys.exit(1)

def generate_colab_cell(payload: str):
    """Generates the Python code to paste into Colab."""
    colab_script = f"""# ==========================================
# FOGSIFT COLAB BRIDGE: LOCAL -> CLOUD
# 1. Run this cell to unpack your local files
# ==========================================
import os, json, zlib, base64

PAYLOAD = "{payload}"

def extract():
    compressed = base64.b64decode(PAYLOAD)
    payload_dict = json.loads(zlib.decompress(compressed).decode('utf-8'))
    for filepath, b64_content in payload_dict.items():
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(b64_content))
        print(f"📦 Extracted: {{filepath}}")
    print("✅ Workspace hydrated. Ready for heavy compute.")

extract()

# ==========================================
# CLOUD -> LOCAL RETURN HARNESS
# 2. Add this to your final cell to pack artifacts to bring home
# ==========================================
def pack_results(files_to_return):
    out = {{}}
    for p in files_to_return:
        if os.path.exists(p):
            with open(p, "rb") as f:
                out[p] = base64.b64encode(f.read()).decode('utf-8')
    
    if out:
        c = base64.b64encode(zlib.compress(json.dumps(out).encode('utf-8'))).decode('utf-8')
        print("\\n" + "="*40)
        print("🚀 COPY THE STRING BELOW AND RUN LOCALLY:")
        print(f"python tools/colab_bridge.py unpack \\"{{c}}\\"")
        print("="*40 + "\\n")
    else:
        print("⚠️ No output files found to pack.")
"""
    print("\n\033[1;32m[ SUCCESS ]\033[0m Payload generated!\n")
    print("Copy the code below and paste it into your first Google Colab cell:")
    print("-" * 60)
    print(colab_script)
    print("-" * 60)

def main():
    parser = argparse.ArgumentParser(description="FogSift Colab Copy-Paste Bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack_parser = subparsers.add_parser("pack", help="Pack local files for Colab")
    pack_parser.add_argument("files", nargs="+", help="Files to transport to Colab")
    unpack_parser = subparsers.add_parser("unpack", help="Unpack payload from Colab")
    unpack_parser.add_argument("payload", help="The base64 payload string")
    unpack_parser.add_argument("--dir", default=".", help="Output directory")
    args = parser.parse_args()
    if args.command == "pack":
        payload_str = pack_files(args.files)
        generate_colab_cell(payload_str)
    elif args.command == "unpack":
        unpack_payload(args.payload, args.dir)

if __name__ == "__main__":
    main()
