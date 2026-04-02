import json
import csv
import os

def merge_content():
    # Define paths
    # Assuming the script is running from the root or relative paths are handled correctly.
    # We will use absolute paths based on the current context or relative to this script location.
    
    # Current script directory: .../merge_methods_sections/
    # We need to go up one level to find the input folders.
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    json_path = os.path.join(base_dir, "pdf_section_identify", "03_extracted_content.json")
    csv_path = os.path.join(base_dir, "supplementary_files", "03_keyword_check_results.csv")
    
    output_dir = os.path.dirname(os.path.abspath(__file__)) # Save in the same directory as this script
    output_filename = "01_merged_content.json"
    output_path = os.path.join(output_dir, output_filename)

    print(f"Loading JSON from: {json_path}")
    print(f"Loading CSV from: {csv_path}")

    # Load Main JSON
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            main_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: JSON file not found at {json_path}")
        return
    except json.JSONDecodeError:
        print("Error: Failed to decode JSON file.")
        return

    # Create mapping from ID to Main JSON Key
    # Key Format Example: "1-s2.0-S0008622324005128-main"
    # Target ID Example: "S0008622324005128"
    id_to_key_map = {}
    for key in main_data.keys():
        parts = key.split('-')
        # Heuristic: the ID starts with 'S' and is typically the 3rd part (index 2)
        # But let's be robust and look for the part starting with S and having numbers
        matched_id = None
        for part in parts:
            if part.startswith('S') and len(part) > 10:
                matched_id = part
                break
        
        if matched_id:
            id_to_key_map[matched_id] = key
        else:
            # Fallback or log if ID structure is different
            pass

    print(f"Mapped {len(id_to_key_map)} keys from JSON.")

    # Process CSV
    merged_count = 0
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f: # utf-8-sig to handle BOM if present
            reader = csv.DictReader(f)
            
            for row_idx, row in enumerate(reader, start=1):
                # Check if evidence was found
                if row.get('Found', 'False').lower() != 'true':
                    continue

                file_path = row.get('File', '')
                if not file_path:
                    continue

                # Extract ID from file path (first folder name)
                # Example: S0008622324005128/S0008622324005128_1-s2.0-....md
                path_parts = file_path.replace('\\', '/').split('/')
                file_id = path_parts[0]

                if file_id not in id_to_key_map:
                    # Try to find partial match or log
                    # print(f"Row {row_idx}: ID {file_id} not found in JSON.")
                    continue

                json_key = id_to_key_map[file_id]
                
                # Get Evidence JSON
                evidence_str = row.get('EvidenceJSON_Cleaned') or row.get('EvidenceJSON')
                if not evidence_str or evidence_str == '[]':
                    continue

                try:
                    evidence_items = json.loads(evidence_str)
                except json.JSONDecodeError:
                    print(f"Row {row_idx}: Failed to parse EvidenceJSON for {file_id}")
                    continue

                if not isinstance(evidence_items, list):
                    continue

                target_dict = main_data[json_key]
                
                # Merge items
                for item in evidence_items:
                    heading_raw = item.get('heading_raw')
                    heading_norm = item.get('heading_norm', 'Supplementary Information')
                    block_text = item.get('block_text', '')

                    # Determine the key to use for the new section
                    # Use heading_raw if available and looks like a header (starts with # or **), else valid name
                    section_key = heading_raw if heading_raw else heading_norm
                    
                    # Clean up key if it's too long or weird? For now keep it as is.
                    
                    if section_key in target_dict:
                        # Append if exists
                        target_dict[section_key]['content'] += f"\n\n[Supplementary Merger]\n{block_text}"
                    else:
                        # Create new
                        target_dict[section_key] = {
                            "_matched_header": section_key,
                            "_matched_level": 1, # Default
                            "content": block_text,
                            "source": "supplementary_files"
                        }
                
                merged_count += 1

    except FileNotFoundError:
        print(f"Error: CSV file not found at {csv_path}")
        return

    print(f"Successfully merged supplementary data for {merged_count} entries.")

    # Save JSON output
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(main_data, f, indent=4, ensure_ascii=False)
    print(f"JSON Output saved to: {output_path}")

    # Prepare data for CSV
    csv_rows = []
    for file_key, sections in main_data.items():
        for section_name, section_data in sections.items():
            # The structure of main_data entries is: 
            # { "section_key": { "content": "...", ... } }
            if isinstance(section_data, dict) and 'content' in section_data:
                csv_rows.append({
                    'FileKey': file_key,
                    'Section': section_name,
                    'Content': section_data.get('content', ''),
                    'Source': section_data.get('source', 'original_json')
                })
    
    csv_output_path = output_path.replace('.json', '.csv')
    try:
        with open(csv_output_path, 'w', encoding='utf-8-sig', newline='') as f:
            fieldnames = ['FileKey', 'Section', 'Content', 'Source']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"CSV Output saved to: {csv_output_path}")
    except Exception as e:
        print(f"Error saving CSV: {e}")

if __name__ == "__main__":
    merge_content()
