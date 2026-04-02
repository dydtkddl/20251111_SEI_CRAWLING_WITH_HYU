
import json
import os
import re
import collections
from lxml import etree
import pathlib

# Paths
BASE_DIR = r"d:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\GROBID"

# 0. PDF Directory (The Source of Truth for the list of papers)
PDF_DIR = r"d:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\pdfs"

# 1. Main PDF Evidence Data (LLM Filtered Data)
# Contains evidence extracted from the main PDF body.
LLM_FILE = os.path.join(BASE_DIR, r"05_llm_filtered_data\llm_filtered_20260107_134930.jsonl")

# 2. Supplementary Material Classification Results
# Contains classified sections from supplementary files.
SUPP_FILE = os.path.join(BASE_DIR, r"07_filter_captions_out\classification_results_sup_sections\supp_sections_classification_20260107_211229.jsonl")

# 3. XML Metadata Directory
XML_DIR = r"D:\20251111_SEI_CRAWLING_WITH_HYU\Elsevier\xmls_meta_abs"

# 4. Output
OUTPUT_DIR = os.path.join(BASE_DIR, "08_merged_data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "merged_results.jsonl")
LOG_FILE = os.path.join(BASE_DIR, "merge_log.txt")

# Regex for PII extraction (S + 16 alphanumeric characters)
PII_PATTERN = re.compile(r"(S[0-9A-Z]{16})")

def extract_pii(text):
    """
    Extracts the Elsevier PII (S followed by 16 alphanumeric characters) from a given text.
    Returns the PII string or None if not found.
    """
    if not text:
        return None
    match = PII_PATTERN.search(text)
    if match:
        return match.group(1)
    return None

def parse_xml_metadata(pii):
    """
    Parses the XML file corresponding to the PII to extract title and abstract.
    Returns (title, abstract).
    """
    xml_filename = f"{pii}__META_ABS.xml"
    xml_path = os.path.join(XML_DIR, xml_filename)
    
    title = ""
    abstract = ""
    
    if not os.path.exists(xml_path):
        return title, abstract
        
    try:
        tree = etree.parse(xml_path)
        root = tree.getroot()
        
        # Namespaces are often used in these XMLs, typically dc.
        namespaces = {'dc': 'http://purl.org/dc/elements/1.1/', 'ce': 'http://www.elsevier.com/xml/common/dtd'}
        
        # Extract Title
        # Try finding dc:title with namespace
        title_node = root.find(".//dc:title", namespaces)
        if title_node is None:
             # Try without namespace if that fails or wildcard
             title_node = root.find(".//{http://purl.org/dc/elements/1.1/}title")
             
        if title_node is not None and title_node.text:
           title = title_node.text.strip()
           
        # Extract Abstract
        # Usually in dc:description or ce:abstract-sec
        desc_node = root.find(".//dc:description", namespaces)
        if desc_node is not None and desc_node.text:
            abstract = desc_node.text.strip()
        else:
            # Fallback to direct text content of description tag if namespace issue
            pass
            
    except Exception as e:
        pass
        
    return title, abstract

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_messages = []
    
    print("Starting Merge Process...")

    # --- PHASE 1: Load Supplementary Data ---
    print(f"Loading Supplementary Data from: {SUPP_FILE}")
    supp_sections_map = collections.defaultdict(list)
    if os.path.exists(SUPP_FILE):
        with open(SUPP_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip(): continue
                try:
                    data = json.loads(line)
                    # Extract PII from doc_id or source_file
                    raw_id = data.get('doc_id', '') or data.get('source_file', '')
                    pii = extract_pii(raw_id)
                    if pii:
                        supp_sections_map[pii].append(data)
                except:
                    continue
    else:
        log_messages.append(f"WARNING: Supp file missing: {SUPP_FILE}")

    # --- PHASE 2: Load Main PDF Evidence Data ---
    print(f"Loading Main PDF Evidence from: {LLM_FILE}")
    main_evidence_map = {}
    if os.path.exists(LLM_FILE):
        with open(LLM_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip(): continue
                try:
                    data = json.loads(line)
                    src = data.get('source_file', '')
                    pii = extract_pii(src)
                    if pii:
                        main_evidence_map[pii] = data
                except:
                    continue
    else:
        log_messages.append(f"WARNING: LLM filtered file missing: {LLM_FILE}")

    # --- PHASE 3: Iterate AlL PDFs (Master List) ---
    print(f"Scanning PDF Directory: {PDF_DIR}")
    
    pdf_files = [f for f in os.listdir(PDF_DIR) if f.lower().endswith('.pdf')]
    print(f"Found {len(pdf_files)} PDF files.")
    
    merged_papers = 0
    missing_xml_count = 0
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as fout:
        for filename in pdf_files:
            pii = extract_pii(filename)
            if not pii:
                log_messages.append(f"Skipping file, no PII found: {filename}")
                continue
            
            # 1. Base Object
            # Try to get existing data from LLM Evidence (Main Body)
            paper_data = main_evidence_map.get(pii, {})
            
            # Ensure identifiers are set
            paper_data['paper_id'] = pii
            if 'doc_id' not in paper_data:
                paper_data['doc_id'] = filename
            if 'source_file' not in paper_data:
                paper_data['source_file'] = filename
                
            # 2. Get Metadata from XML (Title/Abstract)
            # This is critical even if LLM extracted something, XML is usually cleaner for metadata
            xml_title, xml_abstract = parse_xml_metadata(pii)
            
            paper_data['meta_title'] = xml_title
            paper_data['meta_abstract'] = xml_abstract
            
            # If "title" is missing in paper_data, fill it with xml_title
            if not paper_data.get('title'):
                paper_data['title'] = xml_title
                
            if not xml_title and not xml_abstract:
                missing_xml_count += 1
                # log_messages.append(f"No XML metadata for {pii}")
            
            # 3. Attach Supplementary Sections
            supp_data = supp_sections_map.get(pii, [])
            paper_data['supplementary_sections'] = supp_data
            
            # 4. Write
            fout.write(json.dumps(paper_data, ensure_ascii=False) + '\n')
            merged_papers += 1

    # --- Final Logging ---
    summary = (
        f"Merge Completed.\n"
        f"  Total PDFs Processed: {len(pdf_files)}\n"
        f"  Successfully Merged: {merged_papers}\n"
        f"  Papers with Missing XML Metadata: {missing_xml_count}\n"
        f"  Output File: {OUTPUT_FILE}\n"
    )
    print(summary)
    
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write(summary + "\n\nLog Messages:\n")
        # Write first 50 log messages to avoid huge files if many errors
        for msg in log_messages[:50]:
            f.write(msg + "\n")
        if len(log_messages) > 50:
            f.write(f"... and {len(log_messages) - 50} more messages.")

if __name__ == "__main__":
    main()
