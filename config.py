# Configuration for GitaVyasa Project

# Project Paths
PROJECT_ROOT = '/path/to/gita_vyasa'

# Bhagavad Gita Structure Constants
TOTAL_CHAPTERS = 18
VERSE_PATTERN = r'\b(\d+):(\d+)\b'  # Format: Chapter:Verse

# Vedantic Concepts Dictionary
VEDANTIC_CONCEPTS = {
    'कर्म': 'Karma',
    'ज्ञान': 'Jnana',
    'भक्ति': 'Bhakti',
    'मोक्ष': 'Moksha',
    'ब्रह्मन्': 'Brahman',
    'आत्मन्': 'Atman',
    'माया': 'Maya',
    'प्रकृति': 'Prakriti',
    'पुरुष': 'Purusha',
    'योग': 'Yoga',
    'ध्यान': 'Dhyana',
    ' समाधि': 'Samadhi',
    'गुण': 'Gunas',
    'धर्म': 'Dharma',
    # Add more terms as needed...
}

# Concept Categories
CONCEPT_CATEGORIES = {
    'Paths': ['कर्म', 'ज्ञान', 'भक्ति'],
    'Ultimate Reality': ['ब्रह्मन्', 'आत्मन्'],
    'Liberation': ['मोक्ष'],
    'Gunas': ['गुण'],
    'Illusion/Knowledge': ['माया', 'प्रकृति'],
}

# Verse Identification Regex Patterns
VERSE_REGEX = r'\b\d+:[\d]+\b'

# Streamlit Configuration
STREAMLIT_CONFIG = {
    'theme': 'light',
    'sidebar_position': 'left',
}

# Color Schemes for Acharyas
COLOR_SCHEMES = {
    'Acharya_1': '#FFCC00',
    'Acharya_2': '#CC0000',
}

# OCR Configuration for Tesseract
OCR_CONFIG = {
    'tesseract_cmd': '/usr/bin/tesseract',
    'lang': 'eng+deva',
    'oem': 1,
    'psm': 3,
}

# Logging Settings
LOGGING_SETTINGS = {
    'level': 'DEBUG',
    'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    'datefmt': '%Y-%m-%d %H:%M:%S',
}

# Complete Dictionaries for Verses per Chapter
VERSES_PER_CHAPTER = {
    1: 47,
    2: 72,
    3: 43,
    4: 42,
    5: 30,
    6: 47,
    7: 30,
    8: 28,
    9: 34,
    10: 42,
    11: 55,
    12: 20,
    13: 35,
    14: 27,
    15: 20,
    16: 24,
    17: 28,
    18: 78,
}