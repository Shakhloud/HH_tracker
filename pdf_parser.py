import io
from typing import Optional
import pdfplumber
from PyPDF2 import PdfReader


class PDFParser:
    """Parse PDF resume files and extract text content"""
    
    @staticmethod
    async def extract_text(file_bytes: bytes) -> Optional[str]:
        """Extract text from PDF file bytes"""
        try:
            # Try pdfplumber first (better for complex layouts)
            text = await PDFParser._extract_with_pdfplumber(file_bytes)
            if text and len(text.strip()) > 50:
                return text.strip()
            
            # Fallback to PyPDF2
            text = await PDFParser._extract_with_pypdf2(file_bytes)
            if text and len(text.strip()) > 50:
                return text.strip()
            
            return None
            
        except Exception as e:
            print(f"Error extracting PDF text: {e}")
            return None
    
    @staticmethod
    async def _extract_with_pdfplumber(file_bytes: bytes) -> Optional[str]:
        """Extract text using pdfplumber"""
        try:
            text_parts = []
            
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            
            return "\n".join(text_parts) if text_parts else None
            
        except Exception as e:
            print(f"pdfplumber extraction error: {e}")
            return None
    
    @staticmethod
    async def _extract_with_pypdf2(file_bytes: bytes) -> Optional[str]:
        """Extract text using PyPDF2 as fallback"""
        try:
            text_parts = []
            
            pdf_file = io.BytesIO(file_bytes)
            reader = PdfReader(pdf_file)
            
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
            
            return "\n".join(text_parts) if text_parts else None
            
        except Exception as e:
            print(f"PyPDF2 extraction error: {e}")
            return None
    
    @staticmethod
    def validate_pdf(file_bytes: bytes) -> bool:
        """Validate if file is a valid PDF"""
        try:
            # Check PDF magic number
            if not file_bytes.startswith(b'%PDF'):
                return False
            
            # Try to read with PyPDF2
            pdf_file = io.BytesIO(file_bytes)
            reader = PdfReader(pdf_file)
            
            # Check if we can get number of pages
            num_pages = len(reader.pages)
            return num_pages > 0
            
        except Exception:
            return False


pdf_parser = PDFParser()
