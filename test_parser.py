"""
Unit tests and pipeline validation for document-intelligent-parser
"""

import json
import os
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent))

import server
from server import (
    ContentBlock,
    DocumentParserEngine,
    DocumentResult,
    PageData,
    VisualCrop,
    clean_text,
    format_table_to_markdown,
    is_page_unreadable,
    merge_bounding_boxes,
    parse_page_ranges,
)


class TestDocumentIntelligentParser(unittest.TestCase):
    def test_clean_text(self):
        raw = "Hello\x00\x08World!  \n\tThis is a test.   "
        cleaned = clean_text(raw)
        self.assertEqual(cleaned, "HelloWorld!  \n\tThis is a test.")

    def test_is_page_unreadable(self):
        # Case 1: Less than 30 characters
        short_text = "Page 1. Draft only."
        is_unreadable, count = is_page_unreadable(short_text, min_char_threshold=30)
        self.assertTrue(is_unreadable)
        self.assertLess(count, 30)

        # Case 2: Greater than 30 characters
        long_text = "This is a full document section containing enough characters to be considered selectable text."
        is_unreadable, count = is_page_unreadable(long_text, min_char_threshold=30)
        self.assertFalse(is_unreadable)
        self.assertGreaterEqual(count, 30)

        # Case 3: Empty text (scanned page)
        is_unreadable, count = is_page_unreadable("")
        self.assertTrue(is_unreadable)
        self.assertEqual(count, 0)

    def test_format_table_to_markdown(self):
        table_grid = [
            ["Item", "Quantity", "Price"],
            ["Apple", "10", "$2.50"],
            ["Banana | Yellow", "5", "$1.00"],
        ]
        md = format_table_to_markdown(table_grid)
        self.assertIn("| Item | Quantity | Price |", md)
        self.assertIn("| --- | --- | --- |", md)
        self.assertIn("| Banana \\| Yellow | 5 | $1.00 |", md)

    def test_markdown_rendering(self):
        p1 = PageData(
            page_number=1,
            is_scanned=False,
            character_count=120,
            width=612.0,
            height=792.0,
            text_content="Introduction to Intelligent Document Parsing",
            blocks=[
                ContentBlock(
                    block_id=0,
                    block_type="heading",
                    bbox=[50, 50, 400, 75],
                    content="1. Executive Summary",
                ),
                ContentBlock(
                    block_id=1,
                    block_type="text",
                    bbox=[50, 80, 500, 150],
                    content="This system extracts figures and text streams.",
                ),
                ContentBlock(
                    block_id=2,
                    block_type="visual_snippet",
                    bbox=[50, 160, 500, 300],
                    content="![Figure 1](crops/page_1_crop_1.png)",
                    metadata={"vision_transcription": "$$E = mc^2$$"},
                ),
            ],
            crops=[
                VisualCrop(
                    crop_id="page_1_crop_1",
                    page_number=1,
                    crop_type="figure",
                    bbox=[50, 160, 500, 300],
                    relative_path="crops/page_1_crop_1.png",
                    absolute_path="/tmp/crops/page_1_crop_1.png",
                    dpi=200,
                    caption="Figure 1",
                    vision_transcription="$$E = mc^2$$",
                )
            ],
        )

        p2 = PageData(
            page_number=2,
            is_scanned=True,
            character_count=10,
            width=612.0,
            height=792.0,
            text_content="Scanned doc",
            crops=[
                VisualCrop(
                    crop_id="page_2_full_page",
                    page_number=2,
                    crop_type="scanned_page",
                    bbox=[0, 0, 612, 792],
                    relative_path="crops/page_2_scanned.png",
                    absolute_path="/tmp/crops/page_2_scanned.png",
                    dpi=200,
                    caption="Full scanned page 2",
                )
            ],
        )

        # Mock engine without PyMuPDF to test rendering
        class DummyEngine(DocumentParserEngine):
            def __init__(self):
                self.dpi = 200
                self.min_char_threshold = 30

        engine = DummyEngine()
        md = engine._render_markdown(
            filename="sample.pdf",
            total_pages=2,
            scanned_pages=1,
            crops_count=2,
            timestamp="2026-10-04T05:00:00Z",
            pages=[p1, p2],
        )

        self.assertIn("# Document Analysis: sample.pdf", md)
        self.assertIn("## Page 1", md)
        self.assertIn("### 1. Executive Summary", md)
        self.assertIn("$$E = mc^2$$", md)
        self.assertIn("## Page 2", md)
        self.assertIn("![Full scanned page 2](crops/page_2_scanned.png)", md)

    def test_bilingual_english_hindi_handling(self):
        english_q = "Question 1: What is the unit of electric current? (A) Ampere (B) Volt"
        hindi_q = "प्रश्न 1: विद्युत धारा का मात्रक क्या है? (क) एम्पीयर (ख) वोल्ट"
        bilingual_text = f"{english_q}\n{hindi_q}"

        cleaned = clean_text(bilingual_text)
        self.assertIn("विद्युत धारा", cleaned)
        self.assertIn("Question 1", cleaned)

        # Character count preserves Devanagari characters
        is_unreadable, char_count = is_page_unreadable(bilingual_text, min_char_threshold=30)
        self.assertFalse(is_unreadable)
        self.assertGreater(char_count, 50)

        # JSON serialization preserves Devanagari UTF-8 characters without escaping
        json_str = json.dumps({"hindi": hindi_q, "english": english_q}, ensure_ascii=False)
        self.assertIn("विद्युत धारा", json_str)

    def test_json_rpc_fallback_tools_definition(self):
        # Test that stdio definitions are valid JSON schema
        self.assertTrue(callable(server.run_stdio_jsonrpc_fallback))

    def test_parse_page_ranges(self):
        # None or empty returns all pages
        self.assertEqual(parse_page_ranges(None, 5), [0, 1, 2, 3, 4])
        self.assertEqual(parse_page_ranges("", 5), [0, 1, 2, 3, 4])
        self.assertEqual(parse_page_ranges("   ", 5), [0, 1, 2, 3, 4])

        # Single page (1-indexed to 0-indexed)
        self.assertEqual(parse_page_ranges("3", 5), [2])

        # Range
        self.assertEqual(parse_page_ranges("1-3", 5), [0, 1, 2])

        # Mixed comma-separated ranges and singles
        self.assertEqual(parse_page_ranges("1-2, 4, 5", 5), [0, 1, 3, 4])
        self.assertEqual(parse_page_ranges("1-3, 5", 10), [0, 1, 2, 4])

        # Deduplication and sorting
        self.assertEqual(parse_page_ranges("3, 1-2, 2", 5), [0, 1, 2])

        # Invalid page range outside document raises ValueError
        with self.assertRaises(ValueError):
            parse_page_ranges("99-100", 5)

    def test_markdown_rendering_with_metadata_and_toc(self):
        class DummyEngine(DocumentParserEngine):
            def __init__(self):
                self.dpi = 200
                self.min_char_threshold = 30

        engine = DummyEngine()
        p1 = PageData(
            page_number=1,
            is_scanned=False,
            character_count=50,
            width=612.0,
            height=792.0,
            text_content="Page 1 sample content",
            blocks=[],
        )

        md = engine._render_markdown(
            filename="report.pdf",
            total_pages=1,
            scanned_pages=0,
            crops_count=0,
            timestamp="2026-10-04T05:00:00Z",
            pages=[p1],
            metadata={"title": "Annual Report", "author": "Crash Softwares"},
            toc=[{"level": 1, "title": "Overview", "page": 1}],
            selected_pages=[1],
        )

        self.assertIn("# Document Analysis: report.pdf", md)
        self.assertIn("**Title**: Annual Report", md)
        self.assertIn("**Author**: Crash Softwares", md)
        self.assertIn("## Table of Contents", md)
        self.assertIn("**Overview** (Page 1)", md)
        self.assertIn("**Selected Pages Analyzed**: 1", md)


if __name__ == "__main__":
    unittest.main()
