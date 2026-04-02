import re
import argparse
import sys
from pathlib import Path

class ExperimentalExtractor:
    def __init__(self):
        # Flexible regex patterns for the Experimental Header
        self.experimental_patterns = [
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Experimental\s*(?:Section|Part|Details|Procedures)?\s*(?:#+|\*+)?\s*$',  # Experimental...
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Experiments?\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Materials\s+and\s+Methods\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Methods\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Materials?\s+Preparation\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Electrode\s+Preparation\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Experimental\s+Method\s*(?:#+|\*+)?\s*$',
        ]
        
        # Regex for lines that clearly start a NEW section (Results, Discussion, etc.)
        self.stop_patterns = [
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Results\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Results\s+and\s+Discussion\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Discussion\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Conclusions?\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?References?\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*(?:\d+\.?\s*)?Supplementary\s+Figures?\s*(?:#+|\*+)?\s*$',
            r'^(?:#+|\*+)?\s*Figure\s+S\d+.*$', # Figure S1 captions often start new blocks in supp info
            r'^(?:#+|\*+)?\s*Table\s+S\d+.*$',
        ]

    def is_header_match(self, line, patterns):
        # Remove bolding/headers for checking but keep strictness
        clean_line = line.strip()
        for pat in patterns:
            if re.match(pat, clean_line, re.IGNORECASE):
                return True
        return False

    def extract_section(self, content):
        lines = content.split('\n')
        extracted_lines = []
        in_experimental_section = False
        
        for i, line in enumerate(lines):
            line_str = line.strip()
            if not line_str:
                if in_experimental_section:
                    extracted_lines.append(line)
                continue

            # Check for start of experimental section
            if not in_experimental_section:
                if self.is_header_match(line_str, self.experimental_patterns):
                    in_experimental_section = True
                    extracted_lines.append(line) # Include the header
                    continue
            
            # Check for end key (Stop pattern)
            if in_experimental_section:
                if self.is_header_match(line_str, self.stop_patterns):
                    # We reached a stop section
                    break
                extracted_lines.append(line)
        
        return "\n".join(extracted_lines)

    def filter_paragraphs(self, text):
        """
        Filter for contents related to Zinc aqueous battery, anode preparation, coating, etc.
        """
        paragraphs = re.split(r'\n\s*\n', text)
        relevant_paragraphs = []
        
        # Keywords logic
        # Must have related to Zinc AND Anode/Electrode AND Preparation/Coating
        
        zinc_keywords = {'zinc', 'zn'}
        component_keywords = {'anode', 'electrode', 'cathode', 'separator'} # Expanding to cathode/separator if user wants general preparation
        # User asked for: "Zn anode... Exsitu Layer coating... or making aqueous zinc batteries"
        
        # Actually, let's stick to the prompt: 
        # "Zinc aqueous battery Exsitu Layer coating (or Insitu) or making aqueous zinc batteries"
        
        # Broad keyword set for "making/coating"
        action_keywords = {
            'coating', 'coated', 'layer', 'film', 'interface', 'intertace', 
            'etching', 'etched', 'deposit', 'deposition', 'preparation', 'prepared',
            'fabrication', 'fabricated', 'synthesis', 'synthesized', 'growing', 'growth',
            'hydrothermal', 'dip-coating', 'immersing', 'assembly', 'assembled'
        }

        for para in paragraphs:
            lower_para = para.lower()
            
            # Check if it contains "experimental" header (keep it for context if it's the header para)
            if any(re.match(pat, para.strip(), re.IGNORECASE) for pat in self.experimental_patterns):
                relevant_paragraphs.append(para)
                continue

            # Check content
            has_zinc = any(k in lower_para for k in zinc_keywords)
            has_component = any(k in lower_para for k in component_keywords) # anode/cathode/battery/cell
            has_action = any(k in lower_para for k in action_keywords)
            
            # If it mentions Zinc AND (Action OR Component), it's likely relevant in this context
            if has_zinc and (has_component or has_action):
                relevant_paragraphs.append(para)
            elif 'battery' in lower_para and (has_action or has_zinc):
                 relevant_paragraphs.append(para)

        return "\n\n".join(relevant_paragraphs)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Extract Experimental Section from Markdown')
    parser.add_argument('file_path', help='Path to markdown file')
    args = parser.parse_args()

    infile = Path(args.file_path)
    if not infile.exists():
        print(f"Error: File {infile} not found.")
        sys.exit(1)

    content = infile.read_text(encoding='utf-8')
    extracto = ExperimentalExtractor()
    
    # 1. Extract the broad section
    raw_section = extracto.extract_section(content)
    
    if not raw_section:
        print(f"No Experimental section found in {infile.name}")
        # Fallback: Search specifically for preparation paragraphs even if section header is missed/weird
        # (Optional: implement full text scan if section not found)
    else:
        # 2. Filter relevant paragraphs
        filtered_section = extracto.filter_paragraphs(raw_section)
        
        print("-" * 40)
        print(f"Filtered Experimental Content for {infile.name}:")
        print("-" * 40)
        print(filtered_section)
        print("-" * 40)
