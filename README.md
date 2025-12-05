# GitaVyasa

## Project Overview
GitaVyasa is a computational analysis tool designed to explore and analyze commentaries on the Bhagavad Gita by renowned philosophers Shankara, Ramanuja, and Madhva. The tool aims to provide insights into various interpretations and facilitate comparative studies among different commentaries.

## Installation Instructions
To set up the GitaVyasa project, ensure you have Python 3.11 or higher installed. Follow these steps:
1. Clone the repository:
   ```bash
   git clone https://github.com/srinidhi2608/GitaVyasa.git
   cd GitaVyasa
   ```
2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```
3. Install required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Install Tesseract OCR:
   - For Windows, download from [Tesseract at UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) and follow the installation instructions.
   - For Mac, use Homebrew: `brew install tesseract`
   - For Ubuntu, run: `sudo apt install tesseract-ocr`

## Usage Guide
Once installed, you can use GitaVyasa as follows:
1. Activate your virtual environment if not already done.
2. Run the application:
   ```bash
   streamlit run app.py
   ```
3. Follow the on-screen instructions to upload and analyze commentaries.

## Tech Stack
- **Pandas**: For data manipulation and analysis.
- **Streamlit**: For creating web applications.
- **Plotly**: For interactive visualizations.
- **PyPDF2**: For reading PDF files.
- **Pytesseract**: For optical character recognition (OCR).

## Project Structure
```
GitaVyasa/
│
├── app.py            # Main application file
├── requirements.txt   # List of dependencies
├── data/             # Directory for storing data files
├── analyses/         # Directory for analysis scripts
└── README.md         # Project documentation
```

## Known Limitations
- The current implementation of Sanskrit NLP is a mock and may not accurately process and analyze Sanskrit text. Further improvements and refinements are necessary to enhance accuracy.

## Future Enhancements
- Implement full-fledged Sanskrit NLP capabilities.
- Expand the tool to include commentaries from additional philosophers.
- Enhance visualization options and interactivity for a better user experience.
- Incorporate user feedback to refine features and usability.

## Contact
For questions or contributions, please open an issue or pull request in this repository.