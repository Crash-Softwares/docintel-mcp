"""
Document Intelligent Parser - Production MCP Server
===================================================
A Model Context Protocol (MCP) server that parses PDF documents using PyMuPDF (fitz)
and Pillow, detecting unreadable/scanned pages, cropping visual snippets and diagrams,
providing multimodal vision hooks for LaTeX and chart extraction, and exporting
both human-readable Markdown and database-ready JSON schemas.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

def _load_env_fallback():
    for p in [
        Path.cwd() / ".env",
        Path("/home/dextor/projects/code/objective/.env"),
        Path.home() / ".env",
    ]:
        if p.is_file():
            try:
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and (k not in os.environ or os.environ[k].startswith("${")):
                            os.environ[k] = v
            except Exception:
                pass
_load_env_fallback()

# Configure logging to stderr (stdout is reserved for MCP JSON-RPC protocol)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("docintel-mcp")

# Graceful library imports
try:
    import pymupdf as fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    try:
        import fitz
        HAS_PYMUPDF = True
    except ImportError:
        fitz = None  # type: ignore
        HAS_PYMUPDF = False
        logger.warning("PyMuPDF ('pymupdf') is not installed. Run: pip install pymupdf")

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    Image = None  # type: ignore
    HAS_PILLOW = False
    logger.warning("Pillow ('PIL') is not installed. Run: pip install pillow")

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
    HAS_FASTMCP = True
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
        HAS_FASTMCP = True
    except ImportError:
        FastMCP = None  # type: ignore
        HAS_FASTMCP = False
        logger.warning("FastMCP ('mcp') is not installed. Run: pip install mcp")


# ==============================================================================
# Data Structures
# ==============================================================================

@dataclass
class VisualCrop:
    crop_id: str
    page_number: int
    crop_type: str  # "scanned_page", "figure", "drawing", "diagram", "table_image"
    bbox: List[float]  # [x0, y0, x1, y1]
    relative_path: str
    absolute_path: str
    dpi: int
    caption: str = ""
    vision_transcription: Optional[str] = None


@dataclass
class ContentBlock:
    block_id: int
    block_type: str  # "text", "heading", "table", "visual_snippet", "scanned_notice"
    bbox: List[float]
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PageData:
    page_number: int
    is_scanned: bool
    character_count: int
    width: float
    height: float
    text_content: str
    blocks: List[ContentBlock] = field(default_factory=list)
    crops: List[VisualCrop] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DocumentResult:
    filename: str
    file_path: str
    output_dir: str
    total_pages: int
    scanned_pages: int
    searchable_pages: int
    total_blocks: int
    total_crops: int
    markdown_path: str
    json_path: str
    created_at: str
    pages: List[PageData] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    toc: List[Dict[str, Any]] = field(default_factory=list)
    selected_pages: Optional[List[int]] = None


# ==============================================================================
# Helper Utilities
# ==============================================================================

def parse_page_ranges(pages_str: Optional[str], total_pages: int) -> List[int]:
    """
    Parse a page range string (e.g., '1-5, 8, 11-13') into a sorted list of unique 0-indexed page indices.
    Human page numbers are 1-indexed.
    If pages_str is None or empty, returns all page indices [0, ..., total_pages - 1].
    """
    if not pages_str or not pages_str.strip():
        return list(range(total_pages))

    indices: set[int] = set()
    parts = pages_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            sub = part.split("-", 1)
            try:
                start = int(sub[0].strip())
                end = int(sub[1].strip())
                for p in range(start, end + 1):
                    if 1 <= p <= total_pages:
                        indices.add(p - 1)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    indices.add(p - 1)
            except ValueError:
                continue

    sorted_indices = sorted(indices)
    if not sorted_indices:
        raise ValueError(
            f"Requested pages '{pages_str}' are out of range for document with {total_pages} pages."
        )
    return sorted_indices


def ensure_directory(path: Union[str, Path]) -> Path:
    """Ensure directory exists and return Path object."""
    p = Path(path).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def clean_text(text: str) -> str:
    """Normalize text whitespace and strip unprintable characters."""
    if not text:
        return ""
    # Strip non-printable control characters except standard whitespace
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return cleaned.strip()


def is_page_unreadable(page_text: str, min_char_threshold: int = 30) -> Tuple[bool, int]:
    """
    Stage 1: Check whether a page has selectable text or is unreadable.
    Returns (is_unreadable, clean_character_count).
    """
    cleaned = clean_text(page_text)
    # Count alphanumeric and readable symbols (ignore pure whitespace)
    non_ws_chars = len(re.sub(r"\s+", "", cleaned))
    
    if non_ws_chars < min_char_threshold:
        return True, non_ws_chars

    # Detect high ratio of Unicode replacement characters or non-ASCII noise
    replacement_count = cleaned.count("\ufffd")
    if replacement_count > 10 and (replacement_count / max(non_ws_chars, 1)) > 0.25:
        return True, non_ws_chars

    # Detect high count of Unicode Private Use Area (PUA) characters (\ue000-\uf8ff)
    # Common in Indian exam PDFs (CBSE/CTET/State Boards) where Hindi is mapped to custom PUA glyphs
    pua_count = len(re.findall(r"[\ue000-\uf8ff]", cleaned))
    if pua_count > 15:
        return True, non_ws_chars

    return False, non_ws_chars


def has_legacy_indic_fonts(page: Any) -> bool:
    """
    Detect non-Unicode 8-bit Indic fonts (Kruti Dev, Devlys, Chanakya, Walkman, etc.)
    that encode Hindi into Latin character ranges resulting in unreadable text streams.
    """
    if not hasattr(page, "get_fonts"):
        return False
    try:
        legacy_names = ("kruti", "devlys", "chanakya", "walkman", "shusha", "shivaji", "aps", "kundli")
        for f in page.get_fonts():
            basefont = str(f[3] or "").lower()
            if any(k in basefont for k in legacy_names):
                return True
    except Exception:
        pass
    return False


def merge_bounding_boxes(rects: List[fitz.Rect], padding: float = 8.0, page_rect: Optional[fitz.Rect] = None) -> List[fitz.Rect]:
    """
    Cluster and merge nearby or overlapping bounding boxes.
    """
    if not rects:
        return []

    # Sort rects by y0 then x0
    sorted_rects = sorted(rects, key=lambda r: (r.y0, r.x0))
    merged: List[fitz.Rect] = []

    for r in sorted_rects:
        # Expand slightly for proximity clustering
        test_r = fitz.Rect(r.x0 - padding, r.y0 - padding, r.x1 + padding, r.y1 + padding)
        matched = False
        for i, existing in enumerate(merged):
            # Check intersection
            if (
                test_r.intersects(existing)
                or (existing.x0 <= test_r.x1 and existing.x1 >= test_r.x0 and abs(existing.y0 - test_r.y0) < 25)
            ):
                merged[i] = existing | r  # Union bounding boxes
                matched = True
                break
        if not matched:
            merged.append(fitz.Rect(r))

    # Clamp to page boundary if provided
    results: List[fitz.Rect] = []
    for r in merged:
        if page_rect:
            clamped = fitz.Rect(
                max(0.0, r.x0 - padding),
                max(0.0, r.y0 - padding),
                min(page_rect.width, r.x1 + padding),
                min(page_rect.height, r.y1 + padding),
            )
        else:
            clamped = r
        # Filter out tiny slivers (< 20px in width or height)
        if clamped.width >= 20 and clamped.height >= 20:
            results.append(clamped)

    return results


def format_table_to_markdown(table_data: List[List[Any]]) -> str:
    """Convert a 2D table grid into standard GitHub-Flavored Markdown table."""
    if not table_data or len(table_data) == 0:
        return ""

    # Clean cell values
    cleaned_rows: List[List[str]] = []
    for row in table_data:
        cleaned_rows.append([str(cell or "").strip().replace("\n", " ").replace("|", "\\|") for cell in row])

    if not cleaned_rows:
        return ""

    header = cleaned_rows[0]
    num_cols = len(header)
    
    # If header is completely blank, generate default column names
    if all(cell == "" for cell in header):
        header = [f"Col {i+1}" for i in range(num_cols)]

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * num_cols) + " |"
    ]

    for row in cleaned_rows[1:]:
        # Ensure row matches number of header columns
        if len(row) < num_cols:
            row = row + [""] * (num_cols - len(row))
        elif len(row) > num_cols:
            row = row[:num_cols]
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


# ==============================================================================
# Stage 3: Multimodal Vision Hook / Helper
# ==============================================================================

def call_vision(image_path: str, prompt: Optional[str] = None) -> str:
    """
    Stage 3 Multimodal Vision Call:
    Accepts an image snippet, transcribes complex formulas into LaTeX,
    summarizes charts or diagrams, and handles bilingual text.
    Supports any model provider:
      - OpenAI / OpenRouter / OpenAI-Compatible (OPENAI_API_KEY / VISION_BASE_URL)
      - Google Gemini (GEMINI_API_KEY / gemini-3.8-flash)
      - Anthropic Claude (ANTHROPIC_API_KEY)
    """
    path = Path(image_path).resolve()
    if not path.is_file():
        return f"[Error: Image file not found at {path}]"

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("VISION_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    provider = os.environ.get("VISION_PROVIDER", "").strip().lower()

    if not provider:
        if openai_key:
            provider = "openai"
        elif gemini_key:
            provider = "gemini"
        elif anthropic_key:
            provider = "anthropic"

    if not provider or (provider == "openai" and not openai_key) or (provider == "gemini" and not gemini_key) or (provider == "anthropic" and not anthropic_key):
        return (
            f"[Visual snippet: {path.name} | Dimensions: {get_image_dimensions(str(path))}]. "
            "Note: No vision API key configured (set OPENAI_API_KEY, GEMINI_API_KEY, or ANTHROPIC_API_KEY) "
            "to enable automatic LaTeX, bilingual transcription, and diagram vision extraction."
        )

    # Prepare image base64
    try:
        with open(path, "rb") as img_file:
            img_b64 = base64.b64encode(img_file.read()).decode("utf-8")
    except Exception as e:
        return f"[Error reading image file {path.name}: {e}]"

    ext = path.suffix.lower()
    mime_type = "image/png" if ext == ".png" else "image/jpeg"

    default_instruction = (
        "You are an expert document, scientific, and multilingual vision parser specializing in bilingual documents. "
        "Analyze this image snippet extracted from a document:\n"
        "1. Multilingual & Bilingual Precision: Accurately transcribe both English and Hindi (Devanagari script) text. "
        "Preserve bilingual question/section pairings, parallel translations, and options (e.g., Question / प्रश्न, (A)/(क)). "
        "Ensure accurate Devanagari matras, conjuncts (संयुक्ताक्षर), halants, and punctuation.\n"
        "2. Mathematical Formulas: If it contains mathematical formulas, equations, or scientific notation, "
        "transcribe them into precise LaTeX enclosed in $$ ... $$ (display) or $ ... $ (inline).\n"
        "3. Charts & Graphs: If it contains a chart, plot, or diagram, summarize the type of graph, axes, units, "
        "key data points, and conclusions.\n"
        "4. Diagrams: If it contains diagrams or architectural illustrations, describe the components and labels.\n"
        "5. Output clear, concise GitHub-flavored Markdown. Do not repeat greeting fluff."
    )
    user_prompt = prompt.strip() if prompt else default_instruction

    def _call_openai() -> Tuple[bool, str]:
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or os.environ.get("VISION_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model_name = os.environ.get("OPENAI_MODEL", "").strip() or os.environ.get("VISION_MODEL", "gpt-4o")
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{img_b64}", "detail": "high"}}
                    ]
                }
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
        }
        headers = {
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json"
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if choices:
                    return True, choices[0].get("message", {}).get("content", "").strip()
                return False, "[OpenAI Vision returned empty response]"
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            logger.warning(f"OpenAI Vision HTTP {e.code}: {err_msg[:120]}")
            return False, f"[OpenAI HTTP {e.code}: {err_msg[:120]}]"
        except Exception as e:
            logger.warning(f"OpenAI Vision error: {e}")
            return False, f"[OpenAI error: {str(e)}]"

    def _call_gemini() -> Tuple[bool, str]:
        model_name = os.environ.get("GEMINI_VISION_MODEL", "gemini-3.8-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": user_prompt},
                        {"inline_data": {"mime_type": mime_type, "data": img_b64}},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 2048,
            },
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text_parts = [p.get("text", "") for p in parts if "text" in p]
                    result = "\n".join(text_parts).strip()
                    if result:
                        return True, result
                return False, "[Gemini Vision returned empty response]"
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            logger.warning(f"Gemini Vision HTTP {e.code}: {err_msg[:120]}")
            return False, f"[Gemini HTTP {e.code}: {err_msg[:120]}]"
        except Exception as e:
            logger.warning(f"Gemini Vision error: {e}")
            return False, f"[Gemini error: {str(e)}]"

    # Execute with automatic fallback
    if provider in ("openai", "openrouter", "custom"):
        if openai_key:
            ok, text = _call_openai()
            if ok:
                return text
            if gemini_key:
                logger.info("OpenAI Vision failed/limited; triggering automatic fallback to Gemini...")
                ok_g, text_g = _call_gemini()
                if ok_g:
                    return text_g
            return text
        elif gemini_key:
            ok_g, text_g = _call_gemini()
            return text_g
        return "[No valid API key available for vision processing]"

    elif provider == "anthropic":
        model_name = os.environ.get("ANTHROPIC_MODEL", "claude-3-7-sonnet-20250219")
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": model_name,
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": img_b64
                            }
                        },
                        {"type": "text", "text": user_prompt}
                    ]
                }
            ]
        }
        headers = {
            "x-api-key": anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("content", [])
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return "\n".join(texts).strip()
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            logger.error(f"Anthropic Vision API HTTP error: {e.code} - {err_msg}")
            if openai_key:
                logger.info("Anthropic Vision failed; falling back to OpenAI...")
                ok_o, text_o = _call_openai()
                if ok_o:
                    return text_o
            return f"[Vision extraction HTTP error {e.code}: {err_msg[:200]}]"
        except Exception as e:
            return f"[Vision extraction error: {str(e)}]"

    else:
        # Default Gemini
        if gemini_key:
            ok_g, text_g = _call_gemini()
            if ok_g:
                return text_g
            if openai_key:
                logger.info("Gemini Vision failed/limited; triggering automatic fallback to OpenAI...")
                ok_o, text_o = _call_openai()
                if ok_o:
                    return text_o
            return text_g
        elif openai_key:
            ok_o, text_o = _call_openai()
            return text_o
        return "[No valid API key available for vision processing]"


def call_gemini_vision(image_path: str, prompt: Optional[str] = None) -> str:
    """Backward compatibility alias for call_vision."""
    return call_vision(image_path=image_path, prompt=prompt)


def get_image_dimensions(image_path: str) -> str:
    """Return width x height string of an image file."""
    if HAS_PILLOW:
        try:
            with Image.open(image_path) as im:
                return f"{im.width}x{im.height}"
        except Exception:
            pass
    return "unknown"


# ==============================================================================
# Core Document Parsing Engine
# ==============================================================================

class DocumentParserEngine:
    """
    Implements Stages 1-4 of the Document Intelligent Parser pipeline.
    """

    def __init__(self, dpi: int = 200, min_char_threshold: int = 30):
        self.dpi = dpi
        self.min_char_threshold = min_char_threshold
        if not HAS_PYMUPDF:
            raise RuntimeError(
                "PyMuPDF ('fitz') is required but not installed. Install via: pip install pymupdf"
            )

    def parse_document(
        self,
        file_path: str,
        output_dir: Optional[str] = None,
        auto_inspect_crops: bool = False,
        pages: Optional[str] = None,
    ) -> DocumentResult:
        """
        Execute full end-to-end parsing pipeline on a PDF file.
        """
        src_path = Path(os.path.abspath(os.path.expanduser(file_path)))
        if not src_path.is_file():
            raise FileNotFoundError(f"PDF document not found: {src_path}")

        # Default output directory: <stem>_parsed_output adjacent to file or current working dir
        if output_dir:
            out_path = Path(os.path.abspath(os.path.expanduser(output_dir)))
        else:
            out_path = src_path.parent / f"{src_path.stem}_parsed_output"

        ensure_directory(out_path)
        crops_dir = ensure_directory(out_path / "crops")

        logger.info(f"Opening document: {src_path} (Output: {out_path})")

        try:
            doc = fitz.open(str(src_path))
        except Exception as e:
            raise ValueError(f"Corrupt, encrypted, or invalid PDF document: {e}")

        total_pages = len(doc)
        if total_pages == 0:
            doc.close()
            raise ValueError("PDF document contains 0 pages.")

        # Extract native PDF metadata
        raw_meta = doc.metadata or {}
        doc_metadata = {
            "title": clean_text(str(raw_meta.get("title") or "")),
            "author": clean_text(str(raw_meta.get("author") or "")),
            "subject": clean_text(str(raw_meta.get("subject") or "")),
            "keywords": clean_text(str(raw_meta.get("keywords") or "")),
            "creator": clean_text(str(raw_meta.get("creator") or "")),
            "producer": clean_text(str(raw_meta.get("producer") or "")),
            "creation_date": str(raw_meta.get("creationDate") or ""),
            "mod_date": str(raw_meta.get("modDate") or ""),
        }
        doc_metadata = {k: v for k, v in doc_metadata.items() if v}

        # Extract Table of Contents / Outline bookmarks
        try:
            raw_toc = doc.get_toc()
            doc_toc = [
                {"level": item[0], "title": clean_text(str(item[1])), "page": item[2]}
                for item in raw_toc
            ] if raw_toc else []
        except Exception as e:
            logger.debug(f"TOC extraction skipped: {e}")
            doc_toc = []

        target_page_indices = parse_page_ranges(pages, total_pages)
        if pages:
            logger.info(f"Targeting selective page indices: {target_page_indices} (from '{pages}')")

        scanned_count = 0
        searchable_count = 0
        total_blocks_count = 0
        all_crops_count = 0
        parsed_pages: List[PageData] = []

        # Zoom matrix for high-fidelity 200 DPI rendering
        # Default PDF resolution is 72 DPI. Zoom factor = DPI / 72.0
        zoom = self.dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        for page_idx in target_page_indices:
            page_num = page_idx + 1
            page = doc[page_idx]
            page_rect = page.rect
            raw_text = page.get_text("text")

            # ------------------------------------------------------------------
            # Stage 1: Text Stream & Layout Check
            # ------------------------------------------------------------------
            is_unreadable, char_count = is_page_unreadable(
                raw_text, min_char_threshold=self.min_char_threshold
            )

            # Check if page relies on legacy non-Unicode Indic fonts (e.g. Kruti Dev)
            legacy_indic = has_legacy_indic_fonts(page)
            if legacy_indic and not is_unreadable:
                logger.info(
                    f"Page {page_num} detected with legacy non-Unicode Indic font encoding. "
                    "Routing to 200 DPI visual OCR pipeline for accurate Hindi Devanagari transcription."
                )
                is_unreadable = True

            page_data = PageData(
                page_number=page_num,
                is_scanned=is_unreadable,
                character_count=char_count,
                width=page_rect.width,
                height=page_rect.height,
                text_content=clean_text(raw_text),
            )

            # ------------------------------------------------------------------
            # Stage 2: Visual & Diagram Cropping
            # ------------------------------------------------------------------
            if is_unreadable:
                scanned_count += 1
                logger.info(f"Page {page_num} detected as scanned/unreadable (<30 chars). Rendering full page at {self.dpi} DPI.")
                
                # Render full page at 200 DPI
                pix = page.get_pixmap(matrix=mat, alpha=False)
                crop_filename = f"page_{page_num}_scanned.png"
                crop_path = crops_dir / crop_filename
                pix.save(str(crop_path))
                all_crops_count += 1

                # Multimodal inspection hook if enabled
                vision_text = None
                if auto_inspect_crops:
                    vision_text = call_vision(
                        str(crop_path),
                        prompt="Transcribe the full contents of this scanned document page into structured Markdown.",
                    )

                crop_item = VisualCrop(
                    crop_id=f"page_{page_num}_full_page",
                    page_number=page_num,
                    crop_type="scanned_page",
                    bbox=[0.0, 0.0, page_rect.width, page_rect.height],
                    relative_path=f"crops/{crop_filename}",
                    absolute_path=str(crop_path),
                    dpi=self.dpi,
                    caption=f"Full scanned page {page_num}",
                    vision_transcription=vision_text,
                )
                page_data.crops.append(crop_item)

                block = ContentBlock(
                    block_id=total_blocks_count,
                    block_type="scanned_notice",
                    bbox=[0.0, 0.0, page_rect.width, page_rect.height],
                    content=f"Page {page_num} is an unreadable or scanned page. Rendered at {self.dpi} DPI.",
                    metadata={"crop_path": f"crops/{crop_filename}", "vision_transcription": vision_text},
                )
                page_data.blocks.append(block)
                total_blocks_count += 1

            else:
                searchable_count += 1
                # 1. Extract and format tables
                table_rects: List[fitz.Rect] = []
                try:
                    tabs = page.find_tables()
                    for t_idx, tab in enumerate(tabs):
                        t_rect = fitz.Rect(tab.bbox)
                        table_rects.append(t_rect)
                        t_data = tab.extract()
                        md_table = format_table_to_markdown(t_data)
                        page_data.tables.append({
                            "table_index": t_idx,
                            "bbox": [t_rect.x0, t_rect.y0, t_rect.x1, t_rect.y1],
                            "data": t_data,
                            "markdown": md_table,
                        })
                        page_data.blocks.append(
                            ContentBlock(
                                block_id=total_blocks_count,
                                block_type="table",
                                bbox=[t_rect.x0, t_rect.y0, t_rect.x1, t_rect.y1],
                                content=md_table,
                                metadata={"table_index": t_idx},
                            )
                        )
                        total_blocks_count += 1
                except Exception as e:
                    logger.debug(f"Table detection on page {page_num} skipped: {e}")

                # 2. Extract text blocks
                raw_blocks = page.get_text("blocks")
                visual_candidate_rects: List[fitz.Rect] = []

                for b in raw_blocks:
                    # b format: (x0, y0, x1, y1, text_or_bytes, block_no, block_type)
                    b_rect = fitz.Rect(b[0], b[1], b[2], b[3])
                    b_type = b[6] if len(b) > 6 else 0
                    
                    if b_type == 1:
                        # Image block!
                        visual_candidate_rects.append(b_rect)
                    else:
                        # Text block
                        b_text = clean_text(b[4] if len(b) > 4 else "")
                        # Check if this text block overlaps with a detected table
                        is_inside_table = any(b_rect.intersects(tr) for tr in table_rects)
                        if b_text and not is_inside_table:
                            # Classify heading vs body text
                            b_kind = "heading" if (b_rect.height > 20 and len(b_text) < 120 and "\n" not in b_text) else "text"
                            page_data.blocks.append(
                                ContentBlock(
                                    block_id=total_blocks_count,
                                    block_type=b_kind,
                                    bbox=[b_rect.x0, b_rect.y0, b_rect.x1, b_rect.y1],
                                    content=b_text,
                                )
                            )
                            total_blocks_count += 1

                # 3. Detect embedded raster images
                for img_info in page.get_images(full=True):
                    xref = img_info[0]
                    for img_rect in page.get_image_rects(xref):
                        visual_candidate_rects.append(fitz.Rect(img_rect))

                # 4. Detect vector drawings (mathematical curves, graphs, charts, geometric figures)
                try:
                    drawings = page.get_drawings()
                    drawing_rects = [
                        fitz.Rect(d["rect"])
                        for d in drawings
                        if "rect" in d and fitz.Rect(d["rect"]).width > 15 and fitz.Rect(d["rect"]).height > 15
                    ]
                    # Filter out drawings that are simply lines in detected tables
                    drawing_rects = [
                        dr for dr in drawing_rects
                        if not any(dr.intersects(tr) for tr in table_rects)
                    ]
                    visual_candidate_rects.extend(drawing_rects)
                except Exception as e:
                    logger.debug(f"Drawings detection on page {page_num} skipped: {e}")

                # 5. Cluster and merge visual candidate bounding boxes
                merged_visual_regions = merge_bounding_boxes(
                    visual_candidate_rects, padding=10.0, page_rect=page_rect
                )

                # 6. Crop each detected visual region at 200 DPI
                for crop_idx, v_rect in enumerate(merged_visual_regions):
                    # Ignore regions overlapping significantly with tables
                    if any(v_rect.intersects(tr) and (v_rect & tr).get_area() > 0.6 * v_rect.get_area() for tr in table_rects):
                        continue

                    # Render snippet
                    pix = page.get_pixmap(matrix=mat, clip=v_rect, alpha=False)
                    crop_filename = f"page_{page_num}_crop_{crop_idx + 1}.png"
                    crop_path = crops_dir / crop_filename
                    pix.save(str(crop_path))
                    all_crops_count += 1

                    # Multimodal hook if auto-inspect is requested
                    vision_text = None
                    if auto_inspect_crops:
                        vision_text = call_vision(str(crop_path))

                    crop_item = VisualCrop(
                        crop_id=f"page_{page_num}_crop_{crop_idx + 1}",
                        page_number=page_num,
                        crop_type="figure",
                        bbox=[v_rect.x0, v_rect.y0, v_rect.x1, v_rect.y1],
                        relative_path=f"crops/{crop_filename}",
                        absolute_path=str(crop_path),
                        dpi=self.dpi,
                        caption=f"Visual snippet (Page {page_num}, Region {crop_idx + 1})",
                        vision_transcription=vision_text,
                    )
                    page_data.crops.append(crop_item)

                    page_data.blocks.append(
                        ContentBlock(
                            block_id=total_blocks_count,
                            block_type="visual_snippet",
                            bbox=[v_rect.x0, v_rect.y0, v_rect.x1, v_rect.y1],
                            content=f"![Visual snippet (Page {page_num}, Region {crop_idx + 1})](crops/{crop_filename})",
                            metadata={
                                "crop_path": f"crops/{crop_filename}",
                                "vision_transcription": vision_text,
                            },
                        )
                    )
                    total_blocks_count += 1

            parsed_pages.append(page_data)

        doc.close()

        # ----------------------------------------------------------------------
        # Stage 4: Structured Storage & Export
        # ----------------------------------------------------------------------
        md_filename = f"{src_path.stem}_parsed.md"
        json_filename = f"{src_path.stem}_structured.json"
        md_file_path = out_path / md_filename
        json_file_path = out_path / json_filename

        timestamp_str = datetime.now(timezone.utc).isoformat()

        # 1. Generate Markdown output (<filename>_parsed.md)
        selected_pages_list = [p + 1 for p in target_page_indices] if pages else None
        markdown_content = self._render_markdown(
            filename=src_path.name,
            total_pages=total_pages,
            scanned_pages=scanned_count,
            crops_count=all_crops_count,
            timestamp=timestamp_str,
            pages=parsed_pages,
            metadata=doc_metadata,
            toc=doc_toc,
            selected_pages=selected_pages_list,
        )
        with open(md_file_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        # 2. Generate JSON schema output (<filename>_structured.json)
        structured_dict = {
            "document_metadata": {
                "filename": src_path.name,
                "file_path": str(src_path),
                "output_directory": str(out_path),
                "total_pages": total_pages,
                "created_at": timestamp_str,
                "parser_version": "1.0.0",
                "pdf_metadata": doc_metadata,
                "toc": doc_toc,
                "selected_pages": selected_pages_list,
            },
            "summary_stats": {
                "total_pages": total_pages,
                "scanned_pages": scanned_count,
                "searchable_pages": searchable_count,
                "parsed_blocks": total_blocks_count,
                "crops_generated": all_crops_count,
                "dpi": self.dpi,
            },
            "pages": [
                {
                    "page_number": p.page_number,
                    "is_scanned": p.is_scanned,
                    "character_count": p.character_count,
                    "layout": {"width": p.width, "height": p.height},
                    "text_content": p.text_content,
                    "blocks": [asdict(b) for b in p.blocks],
                    "crops": [asdict(c) for c in p.crops],
                    "tables": p.tables,
                }
                for p in parsed_pages
            ],
        }
        with open(json_file_path, "w", encoding="utf-8") as f:
            json.dump(structured_dict, f, indent=2, ensure_ascii=False)

        logger.info(f"Parsing complete. Generated {all_crops_count} crops across {total_pages} pages.")

        return DocumentResult(
            filename=src_path.name,
            file_path=str(src_path),
            output_dir=str(out_path),
            total_pages=total_pages,
            scanned_pages=scanned_count,
            searchable_pages=searchable_count,
            total_blocks=total_blocks_count,
            total_crops=all_crops_count,
            markdown_path=str(md_file_path),
            json_path=str(json_file_path),
            created_at=timestamp_str,
            pages=parsed_pages,
            metadata=doc_metadata,
            toc=doc_toc,
            selected_pages=selected_pages_list,
        )

    def _render_markdown(
        self,
        filename: str,
        total_pages: int,
        scanned_pages: int,
        crops_count: int,
        timestamp: str,
        pages: List[PageData],
        metadata: Optional[Dict[str, Any]] = None,
        toc: Optional[List[Dict[str, Any]]] = None,
        selected_pages: Optional[List[int]] = None,
    ) -> str:
        """Render complete structured document as clean GitHub-Flavored Markdown."""
        lines: List[str] = [
            f"# Document Analysis: {filename}",
            "",
            "> **Parsed by**: `docintel-mcp` Document Intelligence Server  ",
            f"> **Date**: {timestamp}  ",
            f"> **Total Pages**: {total_pages} | **Scanned Pages**: {scanned_pages} | **Visual Crops Extracted**: {crops_count}  ",
        ]
        if selected_pages:
            lines.append(f"> **Selected Pages Analyzed**: {', '.join(str(p) for p in selected_pages)}  ")

        if metadata:
            meta_items = [f"**{k.replace('_', ' ').title()}**: {v}" for k, v in metadata.items()]
            lines.append(f"> **Document Metadata**: {' | '.join(meta_items)}  ")

        lines.extend(["", "---", ""])

        if toc:
            lines.append("## Table of Contents")
            lines.append("")
            for item in toc:
                indent = "  " * max(0, item["level"] - 1)
                lines.append(f"{indent}- **{item['title']}** (Page {item['page']})")
            lines.extend(["", "---", ""])

        for p in pages:
            lines.append(f"## Page {p.page_number}")
            lines.append("")

            if p.is_scanned:
                lines.append(
                    f"> [!WARNING]\n"
                    f"> **Scanned / Image Page**: Page {p.page_number} contains fewer than {self.min_char_threshold} "
                    f"selectable text characters. Rendered in high-resolution ({self.dpi} DPI) below."
                )
                lines.append("")
                for c in p.crops:
                    lines.append(f"![{c.caption}]({c.relative_path})")
                    lines.append("")
                    if c.vision_transcription:
                        lines.append("#### Vision Transcription")
                        lines.append(c.vision_transcription)
                        lines.append("")
            else:
                # Render content blocks in vertical document order
                # Sort blocks by y0 (top to bottom)
                sorted_blocks = sorted(p.blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
                for b in sorted_blocks:
                    if b.block_type == "heading":
                        lines.append(f"### {b.content}")
                        lines.append("")
                    elif b.block_type == "table":
                        lines.append(b.content)
                        lines.append("")
                    elif b.block_type == "visual_snippet":
                        lines.append(b.content)
                        lines.append("")
                        vision_txt = b.metadata.get("vision_transcription")
                        if vision_txt:
                            lines.append(f"> **Vision Analysis / LaTeX**:\n> {vision_txt.replace(chr(10), chr(10) + '> ')}")
                            lines.append("")
                    else:
                        lines.append(b.content)
                        lines.append("")

            lines.append("---")
            lines.append("")

        return "\n".join(lines)


# ==============================================================================
# FastMCP Server Definition
# ==============================================================================

if HAS_FASTMCP:
    mcp_app = FastMCP(
        name="docintel-mcp",
        dependencies=["pymupdf", "pillow", "pydantic"],
    )

    @mcp_app.tool()
    def parse_document(
        file_path: str,
        output_dir: Optional[str] = None,
        dpi: int = 200,
        auto_inspect_crops: bool = False,
        pages: Optional[str] = None,
    ) -> str:
        """
        Parse a PDF document using layout analysis, figure extraction, and structured export.

        Args:
            file_path: Absolute or relative local path to the PDF document.
            output_dir: Optional output directory (defaults to <filename>_parsed_output).
            dpi: Resolution for visual crop rendering (default: 200 DPI).
            auto_inspect_crops: If True, automatically transcribe LaTeX formulas and diagram
                                summaries for crops using multimodal vision models.
            pages: Optional page range to parse (e.g., '1-5, 8, 11-13'). If omitted, all pages are parsed.

        Returns:
            JSON-formatted status string with statistics and output file locations.
        """
        try:
            engine = DocumentParserEngine(dpi=dpi)
            res = engine.parse_document(
                file_path=file_path,
                output_dir=output_dir,
                auto_inspect_crops=auto_inspect_crops,
                pages=pages,
            )
            return json.dumps(
                {
                    "status": "success",
                    "filename": res.filename,
                    "file_path": res.file_path,
                    "output_dir": res.output_dir,
                    "total_pages": res.total_pages,
                    "scanned_pages": res.scanned_pages,
                    "searchable_pages": res.searchable_pages,
                    "selected_pages": res.selected_pages,
                    "parsed_blocks": res.total_blocks,
                    "crops_generated": res.total_crops,
                    "metadata": res.metadata,
                    "toc_entries": len(res.toc),
                    "markdown_path": res.markdown_path,
                    "json_path": res.json_path,
                    "created_at": res.created_at,
                },
                indent=2,
            )
        except Exception as e:
            logger.error(f"Failed to parse document '{file_path}': {e}", exc_info=True)
            return json.dumps(
                {
                    "status": "error",
                    "error_code": type(e).__name__,
                    "message": str(e),
                    "file_path": file_path,
                },
                indent=2,
            )

    @mcp_app.tool()
    def inspect_crop(image_path: str, prompt: Optional[str] = None) -> str:
        """
        Stage 3 Multimodal Vision Inspection:
        Accepts an image snippet (crop or full scan), transcribes complex mathematical
        formulas into LaTeX ($ / $$), and summarizes visual charts, graphs, or diagrams.

        Args:
            image_path: Path to the image file (e.g., from the crops/ directory).
            prompt: Optional custom prompt to override the default transcription instruction.

        Returns:
            Markdown-formatted transcription, LaTeX formulas, or chart breakdown.
        """
        try:
            return call_vision(image_path=image_path, prompt=prompt)
        except Exception as e:
            return f"[Error inspecting crop {image_path}: {e}]"

    @mcp_app.tool()
    def batch_parse(
        directory_path: str,
        output_dir: Optional[str] = None,
        recursive: bool = False,
        dpi: int = 200,
    ) -> str:
        """
        Scan a folder for PDF documents and parse each one.

        Args:
            directory_path: Directory path containing PDF files (e.g., ~/projects/code).
            output_dir: Optional central directory to store parsed outputs.
            recursive: Whether to scan subdirectories for PDFs.
            dpi: Resolution for rendered image crops (default: 200 DPI).

        Returns:
            JSON summary of all processed documents.
        """
        target_dir = Path(os.path.abspath(os.path.expanduser(directory_path)))
        if not target_dir.is_dir():
            return json.dumps({
                "status": "error",
                "message": f"Directory not found: {target_dir}"
            })

        pattern = "**/*.pdf" if recursive else "*.pdf"
        pdf_files = list(target_dir.glob(pattern))
        
        if not pdf_files:
            return json.dumps({
                "status": "warning",
                "message": f"No PDF files found in {target_dir}",
                "count": 0
            })

        results = []
        engine = DocumentParserEngine(dpi=dpi)
        for pdf in pdf_files:
            try:
                doc_out = Path(output_dir) / pdf.stem if output_dir else None
                res = engine.parse_document(str(pdf), output_dir=str(doc_out) if doc_out else None)
                results.append({
                    "filename": res.filename,
                    "status": "success",
                    "total_pages": res.total_pages,
                    "crops_generated": res.total_crops,
                    "markdown_path": res.markdown_path,
                })
            except Exception as e:
                results.append({
                    "filename": pdf.name,
                    "status": "error",
                    "message": str(e),
                })

        return json.dumps({
            "status": "completed",
            "directory": str(target_dir),
            "total_files": len(pdf_files),
            "results": results,
        }, indent=2)

    @mcp_app.tool()
    def health_check() -> str:
        """
        Verify the server status, environment, and library dependencies.
        """
        return json.dumps(
            {
                "status": "healthy",
                "server_name": "docintel-mcp",
                "has_pymupdf": HAS_PYMUPDF,
                "has_pillow": HAS_PILLOW,
                "has_fastmcp": HAS_FASTMCP,
                "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
                "has_openai_key": bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("VISION_API_KEY")),
                "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "platform": sys.platform,
                "python_version": sys.version,
            },
            indent=2,
        )


# ==============================================================================
# Standalone Fallback & Direct Execution CLI
# ==============================================================================

def run_cli_mode():
    """CLI mode for direct testing and headless document processing."""
    import argparse

    parser = argparse.ArgumentParser(
        description="DocIntel MCP Server / CLI"
    )
    parser.add_argument("file_path", nargs="?", help="Path to PDF file to parse")
    parser.add_argument("--output", "-o", help="Output directory path")
    parser.add_argument("--dpi", type=int, default=200, help="DPI for crops (default: 200)")
    parser.add_argument("--pages", help="Optional page range to parse (e.g. '1-5,10')")
    parser.add_argument("--auto-inspect", action="store_true", help="Auto-inspect crops with multimodal vision")
    parser.add_argument("--inspect-crop", help="Inspect a single crop image file")
    parser.add_argument("--health", action="store_true", help="Run health check")
    args = parser.parse_args()

    if args.health:
        print(json.dumps({
            "status": "healthy",
            "has_pymupdf": HAS_PYMUPDF,
            "has_pillow": HAS_PILLOW,
            "has_fastmcp": HAS_FASTMCP,
            "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
            "has_openai_key": bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("VISION_API_KEY")),
            "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        }, indent=2))
        return

    if args.inspect_crop:
        print(call_vision(args.inspect_crop))
        return

    if not args.file_path:
        parser.print_help()
        return

    engine = DocumentParserEngine(dpi=args.dpi)
    res = engine.parse_document(
        args.file_path,
        output_dir=args.output,
        auto_inspect_crops=args.auto_inspect,
        pages=args.pages,
    )
    print("\n--- Parsing Result ---")
    print(f"Document: {res.filename}")
    print(f"Total Pages: {res.total_pages} (Scanned: {res.scanned_pages})")
    if res.selected_pages:
        print(f"Selected Pages: {res.selected_pages}")
    print(f"Blocks: {res.total_blocks} | Crops: {res.total_crops}")
    if res.metadata:
        print(f"Metadata: {res.metadata}")
    print(f"Markdown: {res.markdown_path}")
    print(f"JSON: {res.json_path}")


def run_stdio_jsonrpc_fallback():
    """
    Fallback Standard MCP stdio JSON-RPC loop if FastMCP is not installed.
    Guarantees that the server responds to MCP initialize, tools/list, and tools/call!
    """
    logger.info("Starting docintel-mcp via stdio JSON-RPC protocol...")
    
    tools_definitions = [
        {
            "name": "parse_document",
            "description": "Parse a PDF document with layout analysis, figure extraction, and structured export.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Local path to PDF document"},
                    "output_dir": {"type": "string", "description": "Optional output directory"},
                    "dpi": {"type": "integer", "description": "DPI for rendering crops (default 200)", "default": 200},
                    "auto_inspect_crops": {"type": "boolean", "description": "Call vision model on crops", "default": False},
                    "pages": {"type": "string", "description": "Optional page range to parse (e.g. '1-5,10')"}
                },
                "required": ["file_path"]
            }
        },
        {
            "name": "inspect_crop",
            "description": "Inspect visual crop image snippet to transcribe LaTeX formulas or summarize charts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Path to image crop"},
                    "prompt": {"type": "string", "description": "Optional custom prompt"}
                },
                "required": ["image_path"]
            }
        },
        {
            "name": "batch_parse",
            "description": "Scan a directory and batch parse all PDF documents.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "directory_path": {"type": "string", "description": "Path to directory with PDFs"},
                    "output_dir": {"type": "string", "description": "Optional central output directory"},
                    "recursive": {"type": "boolean", "description": "Scan subdirectories recursively", "default": False},
                    "dpi": {"type": "integer", "description": "DPI for crops", "default": 200}
                },
                "required": ["directory_path"]
            }
        },
        {
            "name": "health_check",
            "description": "Check MCP server status and dependencies.",
            "inputSchema": {"type": "object", "properties": {}}
        }
    ]

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {
                            "name": "docintel-mcp",
                            "version": "1.0.0"
                        },
                        "capabilities": {
                            "tools": {}
                        }
                    }
                }
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": tools_definitions}
                }
            elif method == "tools/call":
                t_name = params.get("name")
                args = params.get("arguments", {})
                
                if t_name == "parse_document":
                    engine = DocumentParserEngine(dpi=args.get("dpi", 200))
                    res = engine.parse_document(
                        file_path=args.get("file_path"),
                        output_dir=args.get("output_dir"),
                        auto_inspect_crops=args.get("auto_inspect_crops", False),
                        pages=args.get("pages"),
                    )
                    content_str = json.dumps({
                        "status": "success",
                        "filename": res.filename,
                        "output_dir": res.output_dir,
                        "total_pages": res.total_pages,
                        "scanned_pages": res.scanned_pages,
                        "selected_pages": res.selected_pages,
                        "crops_generated": res.total_crops,
                        "metadata": res.metadata,
                        "markdown_path": res.markdown_path,
                        "json_path": res.json_path,
                    }, indent=2)
                elif t_name == "inspect_crop":
                    content_str = call_vision(
                        image_path=args.get("image_path"),
                        prompt=args.get("prompt")
                    )
                elif t_name == "health_check":
                    content_str = json.dumps({
                        "status": "healthy",
                        "has_pymupdf": HAS_PYMUPDF,
                        "has_pillow": HAS_PILLOW,
                        "has_fastmcp": HAS_FASTMCP,
                        "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
                        "has_openai_key": bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("VISION_API_KEY")),
                        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                    }, indent=2)
                else:
                    content_str = f"Unknown tool: {t_name}"

                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": content_str}]
                    }
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }

            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        except Exception as err:
            logger.error(f"Error handling request: {err}", exc_info=True)
            err_resp = {
                "jsonrpc": "2.0",
                "id": req.get("id") if "req" in locals() and isinstance(req, dict) else None,
                "error": {"code": -32603, "message": str(err)}
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


def main():
    # If launched with CLI flags, execute CLI mode
    if len(sys.argv) > 1 and sys.argv[1] not in ("--stdio", "run"):
        run_cli_mode()
        return

    # If FastMCP is available, use its native runner
    if HAS_FASTMCP:
        logger.info("Running via FastMCP server engine...")
        mcp_app.run()
    else:
        # Fallback to stdio JSON-RPC loop
        run_stdio_jsonrpc_fallback()


if __name__ == "__main__":
    main()
