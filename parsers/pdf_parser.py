from pathlib import Path

from PyPDF2 import PdfReader


def parse_pdf(file_path: str | Path) -> str:
    """Extract text from PDF file."""
    reader = PdfReader(str(file_path))
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text.strip())
    return "\n\n".join(parts)
