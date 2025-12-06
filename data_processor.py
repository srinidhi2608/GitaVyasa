"""
MODULE 1: Data Ingestion and Structuring

This module handles extraction and structuring of Sanskrit commentaries from PDF files.

Functions:
- extract_text_from_pdf: Extract text from PDF using OCR
- structure_commentary: Parse and structure commentary into DataFrame
- process_all_commentaries: Batch process all three commentaries

Author: GitaVyasa Project
Date: 2025-12-05
"""

import re
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import pandas as pd
import PyPDF2
from tqdm import tqdm

try:
    import pytesseract
    from PIL import Image
    from pdf2image import convert_from_path
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    logging.warning("OCR libraries not available")

from config import VERSES_PER_CHAPTER
from utils.sanskrit_processor import is_devanagari, normalize_devanagari

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from PDF file using PyPDF2 with OCR fallback."""
    pdf_path = Path(pdf_path)
    
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    
    logger.info(f"Extracting text from {pdf_path.name}")
    raw_text = ""
    
    try:
        with open(pdf_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            total_pages = len(pdf_reader.pages)
            
            logger.info(f"PDF has {total_pages} pages")
            
            for page_num in range(total_pages):
                page = pdf_reader.pages[page_num]
                page_text = page.extract_text()
                
                if page_text and page_text.strip():
                    raw_text += page_text + "\n"
        
        if raw_text.strip() and is_devanagari(raw_text):
            logger.info(f"Successfully extracted {len(raw_text)} characters")
            return normalize_devanagari(raw_text)
        else:
            logger.warning("No Devanagari text found")
            
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        raise RuntimeError(f"All extraction methods failed for {pdf_path}")
    
    return raw_text

def structure_commentary(raw_text: str, acharya_name: str, verse_map_csv: str) -> pd.DataFrame:
    """Structure raw commentary text into a DataFrame with verse-level granularity."""
    logger.info(f"Structuring commentary for {acharya_name}")
    
    verse_map_csv = Path(verse_map_csv)
    if verse_map_csv.exists():
        verse_map_df = pd.read_csv(verse_map_csv)
    else:
        logger.warning(f"Verse map not found: {verse_map_csv}")
        verse_map_df = pd.DataFrame()
    
    structured_data = []
    sections = _split_into_verse_sections(raw_text)
    
    logger.info(f"Found {len(sections)} potential verse sections")
    
    for section in sections:
        verse_info = _identify_verse(section, verse_map_df)
        
        if verse_info:
            chapter, verse_num, original_verse, commentary = verse_info
            
            structured_data.append({
                'acharya_name': acharya_name,
                'chapter': chapter,
                'verse_number': verse_num,
                'original_sanskrit_verse': original_verse,
                'commentary_sanskrit_text': commentary
            })
    
    df = pd.DataFrame(structured_data)
    
    if not df.empty:
        df = df.sort_values(['chapter', 'verse_number']).reset_index(drop=True)
        logger.info(f"Structured {len(df)} verses successfully")
    
    return df

def _split_into_verse_sections(text: str) -> List[str]:
    """Split raw text into sections likely corresponding to individual verses."""
    verse_pattern = r'(?:^|\n)\s*(\d+)\.(\d+)\s*[।॥\n]'
    sections = re.split(verse_pattern, text)
    
    structured_sections = []
    i = 0
    while i < len(sections):
        if i + 2 < len(sections) and sections[i+1].isdigit() and sections[i+2].isdigit():
            chapter = sections[i+1]
            verse = sections[i+2]
            content = sections[i+3] if i+3 < len(sections) else ""
            
            structured_sections.append(f"{chapter}.{verse}\n{content}")
            i += 4
        else:
            i += 1
    
    if not structured_sections:
        sections = re.split(r'\n\n+|।\s*।', text)
        structured_sections = [s.strip() for s in sections if s.strip() and len(s.strip()) > 50]
    
    return structured_sections

def _identify_verse(section: str, verse_map_df: pd.DataFrame) -> Optional[Tuple[int, int, str, str]]:
    """Identify verse information from a text section."""
    match = re.search(r'(\d+)\.(\d+)', section)
    
    if not match:
        return None
    
    chapter = int(match.group(1))
    verse_num = int(match.group(2))
    
    if chapter < 1 or chapter > 18:
        return None
    
    if verse_num < 1 or verse_num > VERSES_PER_CHAPTER.get(chapter, 0):
        return None
    
    if not verse_map_df.empty:
        verse_row = verse_map_df[(verse_map_df['chapter'] == chapter) & (verse_map_df['verse_number'] == verse_num)]
        
        if not verse_row.empty:
            original_verse = verse_row.iloc[0]['verse_full_text']
        else:
            lines = section.split('\n')
            original_verse = '\n'.join(lines[1:5]) if len(lines) > 1 else ""
    else:
        lines = section.split('\n')
        original_verse = '\n'.join(lines[1:5]) if len(lines) > 1 else ""
    
    commentary = section[match.end():].strip()
    
    return chapter, verse_num, original_verse, commentary

def process_all_commentaries(pdf_paths: Optional[Dict] = None, verse_map_csv: Optional[str] = None, output_dir: Optional[str] = None) -> pd.DataFrame:
    """Process all three commentary PDFs and save structured data."""
    all_dataframes = []
    
    if pdf_paths is None:
        pdf_paths = {}
    
    for acharya_name, pdf_path in pdf_paths.items():
        logger.info(f"Processing {acharya_name} commentary")
        
        try:
            if not Path(pdf_path).exists():
                logger.warning(f"PDF not found: {pdf_path}")
                continue
            
            raw_text = extract_text_from_pdf(pdf_path)
            df = structure_commentary(raw_text, acharya_name, verse_map_csv or 'data/verse_map.csv')
            
            if df.empty:
                logger.warning(f"No data extracted for {acharya_name}")
                continue
            
            if output_dir:
                output_path = Path(output_dir) / f"{acharya_name.lower()}_processed.csv"
                df.to_csv(output_path, index=False, encoding='utf-8')
                logger.info(f"Saved to {output_path}")
            
            all_dataframes.append(df)
            
        except Exception as e:
            logger.error(f"Error processing {acharya_name}: {e}")
            continue
    
    if all_dataframes:
        combined_df = pd.concat(all_dataframes, ignore_index=True)
        logger.info(f"Successfully processed {len(combined_df)} total verses")
        return combined_df
    else:
        return pd.DataFrame()

if __name__ == '__main__':
    print("GitaVyasa Data Processor")
    print("Place PDF files in data/pdfs/ and run this script")
