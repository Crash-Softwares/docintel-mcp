<div align="center">

  <h1>⚡ DocIntel MCP</h1>
  <p><strong>Production Document Intelligence & Vision Model Context Protocol (MCP) Server</strong></p>

  <p>
    <a href="https://crashsoftwares.com" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/Crash%20Softwares-crashsoftwares.com-8b6cff?style=flat-square" alt="Crash Softwares"></a>
    <a href="https://github.com/Crash-Softwares/docintel-mcp/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Crash-Softwares/docintel-mcp/ci.yml?branch=main&style=flat-square&label=CI" alt="CI Status"></a>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Protocol-MCP%202024--11--05-brightgreen?style=flat-square" alt="MCP">
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-orange?style=flat-square" alt="License: MIT"></a>
  </p>

</div>

---

## 📌 Overview

**DocIntel MCP** (`docintel-mcp`) is a production-grade Model Context Protocol (MCP) server engineered by [Crash Softwares](https://crashsoftwares.com). It equips AI coding agents, LLM workspaces, and developer tools (Antigravity, Claude Code, Claude Desktop, Cursor, Windsurf, Roo Code, etc.) with human-grade document comprehension.

It intelligently scans local PDFs, detects scanned pages vs. selectable vector layouts, crops mathematical charts, diagrams, and figures into high-resolution visual snippets, transcribes formulas into LaTeX using multimodal vision, extracts native metadata and outlines/TOC, and exports structured Markdown and database-ready JSON.

---

## 🌟 Key Capabilities

- **4-Stage Document Intelligence Pipeline:**
  1. *Text Stream & Layout Check:* Accurately detects character density, unreadable scans, and garbled font encodings.
  2. *Hybrid Page Routing:* Automatically renders scanned pages at high DPI while extracting selectable text and tables from vector layouts.
  3. *Visual & Diagram Cropping:* Identifies vector drawings, charts, and embedded raster figures, clusters bounding boxes, and crops them into clean image snippets.
  4. *Multimodal Vision Extraction:* Transcribes visual formulas into LaTeX (`$`, `$$`) and summarizes charts with multimodal vision models.
- **Selective Page Parsing (`pages="1-5,10"`):**
  - Save compute, time, and tokens on massive multi-page documents by targeting exact page ranges and chapters.
- **Document Metadata & Outline / TOC Extraction:**
  - Extracts native PDF properties (title, author, creation date) and document bookmarks/TOC into Markdown links and structured JSON.
- **Multi-Provider Vision Support:**
  - Works out of the box with **Google Gemini** (`gemini-3.8-flash`), **OpenAI** (`gpt-4o`), **Anthropic Claude** (`claude-3-7-sonnet`), and **OpenRouter** or custom OpenAI-compatible endpoints.
  - Automatic fallback between vision providers if one encounters rate limits or errors.
- **Bilingual & Indic PDF Optimization:**
  - Detects **Unicode Private Use Area (PUA)** glyphs (`\ue000-\uf8ff`) common in non-standard bilingual PDFs and exam papers.
  - Detects legacy 8-bit Indic fonts (Kruti Dev, Devlys, Chanakya, Walkman) that would otherwise yield garbled text.
  - Preserves UTF-8 Devanagari Hindi and English bilingual text side by side.
- **Dual Execution Modes:**
  - Operates as a standard MCP server over stdio (using FastMCP with a resilient JSON-RPC fallback).
  - Also runs standalone from the command line (CLI) for batch processing and local testing.
- **Structured Outputs:**
  - Exports clean GitHub-Flavored Markdown (`<filename>_parsed.md`) with linked image crops and LaTeX equations.
  - Generates strongly typed, database-ready JSON schemas (`<filename>_structured.json`).

---

## 🏗️ 4-Stage Processing Pipeline

```
┌────────────────────────────────────────────────────────┐
│                      Input PDF                         │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ Stage 1: Text Stream & Layout Check                   │
│ - Counts selectable characters per page                │
│ - Flags pages with < 30 chars or garbled text          │
│ - Detects Unicode PUA glyphs and legacy font encodings │
│ - Supports selective page range filtering (--pages)    │
└──────────────┬───────────────────────────┬─────────────┘
               │ (Scanned / Unreadable)    │ (Searchable Text)
               ▼                           ▼
┌──────────────────────────────┐ ┌───────────────────────────────────────┐
│ Stage 2A: Full Page Render   │ │ Stage 2B: Visual & Diagram Cropping   │
│ - Render 200 DPI page image  │ │ - Detect vector drawings & diagrams   │
│ - Save to crops/page_X_scan  │ │ - Detect embedded raster figures      │
└──────────────┬───────────────┘ │ - Cluster & merge bounding boxes      │
               │                 │ - Crop snippets to crops/page_X_crop  │
               │                 └───────────────────┬───────────────────┘
               │                                     │
               └──────────────────┬──────────────────┘
                                  │
                                  ▼
┌────────────────────────────────────────────────────────┐
│ Stage 3: Vision Extraction & Transcribe               │
│ - Multimodal vision inspection (inspect_crop)          │
│ - Transcribes equations into LaTeX ($ / $$)            │
│ - Summarizes charts, axes, and visual structures       │
│ - Automatic fallback across Gemini / OpenAI / Claude   │
└─────────────────────────┬──────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────┐
│ Stage 4: Structured Storage & Export                   │
│ 1. <filename>_parsed.md (GFM Markdown + TOC + images)  │
│ 2. <filename>_structured.json (Schema for DB ingest)   │
└────────────────────────────────────────────────────────┘
```

---

## 🛠️ MCP Tools

### 1. `parse_document`
Parses a PDF file through all 4 pipeline stages.
* `file_path` (`string`, required): Local file path to the PDF document.
* `output_dir` (`string`, optional): Destination directory for outputs (defaults to `<filename>_parsed_output`).
* `dpi` (`int`, optional, default: `200`): Resolution for rendered visual crops.
* `pages` (`string`, optional): Specific page range to parse (e.g., `"1-5, 8, 11-13"`). If omitted, all pages are processed.
* `auto_inspect_crops` (`bool`, optional, default: `false`): Automatically transcribe math formulas and diagrams via multimodal vision.

### 2. `inspect_crop`
Multimodal inspection tool for diagrams, figures, and math equations.
* `image_path` (`string`, required): Path to the cropped image file.
* `prompt` (`string`, optional): Custom vision prompt for the vision model.

### 3. `batch_parse`
Batch parses an entire directory of PDFs.
* `directory_path` (`string`, required): Target directory containing PDF files.
* `output_dir` (`string`, optional): Root output directory.
* `recursive` (`bool`, optional, default: `false`): Whether to recursively scan subdirectories.

### 4. `health_check`
Verifies server health, dependency status (`PyMuPDF`, `Pillow`, `FastMCP`), and API key configuration.

---

## 🚀 Installation & Setup

### Option A: Direct Run via `uvx` (Recommended - Zero Install)
```bash
uvx --from git+https://github.com/Crash-Softwares/docintel-mcp.git docintel-mcp
```

### Option B: Local Clone
```bash
git clone https://github.com/Crash-Softwares/docintel-mcp.git
cd docintel-mcp
pip install -r requirements.txt
python3 server.py --health
```

### Option C: Smithery.ai (1-Click Registry Install)
```bash
npx -y @smithery/cli install @Crash-Softwares/docintel-mcp --client claude
```

### Option D: Docker
```bash
docker build -t docintel-mcp .
docker run -i --rm -e GEMINI_API_KEY="$GEMINI_API_KEY" docintel-mcp
```

---

## ⚙️ MCP Client Configuration

Add `docintel-mcp` to your MCP configuration file.

### Antigravity (`~/.gemini/config/mcp_config.json` or `.antigravity/mcp_config.json`)
```json
{
  "mcpServers": {
    "docintel-mcp": {
      "command": "python3",
      "args": [
        "/absolute/path/to/docintel-mcp/server.py"
      ],
      "env": {
        "GEMINI_API_KEY": "${GEMINI_API_KEY}"
      }
    }
  }
}
```

### Claude Desktop / Claude Code (`claude_desktop_config.json` or `.mcp.json`)
```json
{
  "mcpServers": {
    "docintel-mcp": {
      "command": "python3",
      "args": [
        "/absolute/path/to/docintel-mcp/server.py"
      ],
      "env": {
        "GEMINI_API_KEY": "YOUR_GEMINI_API_KEY",
        "OPENAI_API_KEY": "YOUR_OPENAI_API_KEY",
        "ANTHROPIC_API_KEY": "YOUR_ANTHROPIC_API_KEY"
      }
    }
  }
}
```

### Cursor / Windsurf / Generic MCP (`mcp.json`)
```json
{
  "mcpServers": {
    "docintel-mcp": {
      "command": "python3",
      "args": [
        "/absolute/path/to/docintel-mcp/server.py"
      ],
      "env": {
        "OPENAI_API_KEY": "YOUR_OPENAI_API_KEY"
      }
    }
  }
}
```

---

## 🔑 Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | Google Gemini API key for vision calls | - |
| `GEMINI_VISION_MODEL` | Gemini vision model identifier | `gemini-3.8-flash` |
| `OPENAI_API_KEY` | OpenAI or OpenRouter API key | - |
| `OPENAI_MODEL` | Model name for OpenAI-compatible endpoint | `gpt-4o` |
| `OPENAI_BASE_URL` | Base URL for OpenAI-compatible endpoint | `https://api.openai.com/v1` |
| `ANTHROPIC_API_KEY` | Anthropic Claude API key | - |
| `ANTHROPIC_MODEL` | Anthropic Claude model name | `claude-3-7-sonnet-20250219` |

*Note:* If multiple keys are provided, the server prioritizes the fastest model and automatically falls back if rate limits or errors occur.

---

## 💻 CLI Usage

DocIntel can also be executed directly as a standalone CLI tool without an MCP client:

```bash
# 1. Server health check
python3 server.py --health

# 2. Parse a PDF document
python3 server.py /path/to/document.pdf -o ./output --dpi 200

# 3. Parse specific page ranges (e.g. pages 1 to 5 and page 10)
python3 server.py /path/to/document.pdf --pages 1-5,10

# 4. Parse with automatic vision inspection for equations and diagrams
python3 server.py /path/to/document.pdf --auto-inspect

# 5. Inspect an individual cropped snippet
python3 server.py --inspect-crop ./output/crops/page_1_crop_1.png
```

---

## 🧪 Testing

Run unit tests and verification checks:

```bash
python3 test_parser.py
```

---

## 📄 License

This project is licensed under the **[MIT License](LICENSE)**:

```text
Copyright (c) 2026 crashsoftwares.com
```
