import streamlit as st
import json
import os

# Page Config
st.set_page_config(layout="wide", page_title="Methods Viewer", page_icon="🧪")

# Load Data
@st.cache_data
def load_data(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

JSON_PATH = r"d:\20251111_SEI_CRAWLING_WITH_HYU\20260105_전달내용_및_develope\pdfs_section_identify\03_extract_numbered_methods_sections_content.json"
data = load_data(JSON_PATH)

# Styling
st.markdown("""
    <style>
    .block-container {padding-top: 2rem;}
    .reportview-container .main .block-container {max-width: 1000px;}
    </style>
    """, unsafe_allow_html=True)

st.title("🧪 Experimental Sections Viewer")

if not data:
    st.error(f"JSON data not found at: `{JSON_PATH}`")
    st.stop()

# Sidebar: Paper Selection
paper_ids = list(data.keys())
selected_paper = st.sidebar.selectbox(
    "Select Paper ID", 
    paper_ids, 
    index=0 if paper_ids else None
)

if not selected_paper:
    st.info("No papers found in the JSON file.")
    st.stop()

# Main Content
sections = data[selected_paper]

st.markdown(f"### 📄 **{selected_paper}**")
st.caption(f"Found {len(sections)} experimental sections extracted.")
st.divider()

if not sections:
    st.warning("⚠️ This paper has entries in the content map, but the content dictionary is empty.")
else:
    # Iterate through sections
    for header, content in sections.items():
        # Clean header for display (remove markdown hashes)
        display_header = header.strip().lstrip('#').strip()
        
        with st.expander(f"📌 {display_header}", expanded=True):
            if content.strip():
                st.markdown(content)
            else:
                st.info("*No content extracted for this header (possibly only a title)*")

# Sidebar Stats
st.sidebar.divider()
st.sidebar.markdown(f"**Total Papers:** {len(paper_ids)}")
st.sidebar.markdown("**Section Stats:**")
if sections:
    for h in sections.keys():
        st.sidebar.text(f"- {h[:30]}..." if len(h)>30 else f"- {h}")
