from pathlib import Path

from docx import Document


def parse_docx(file_path: str | Path) -> str:
    """Extract text from DOCX file (paragraphs + tables)."""
    doc = Document(str(file_path))
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text.strip())
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
            if row_text:
                parts.append(row_text)
    return "\n".join(parts)
