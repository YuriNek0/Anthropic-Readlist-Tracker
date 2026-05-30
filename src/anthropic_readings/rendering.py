from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .core.link_rewrite import rewrite_markdown_links, rewrite_notebook_markdown_cells
from .core.output_paths import (
    build_output_relative_path,
    build_render_output_path as _build_render_output_path,
    resolve_output_date,
    slugify_title as _slugify_title,
)
from .models import RenderedDocument, TrackedDocument

RENDER_TIMEOUT_SECONDS = 600

PDF_RENDER_STYLESHEET = """
@page {
  margin: 0.75in;
}

*,
*::before,
*::after {
  box-sizing: border-box !important;
}

html,
body {
  max-width: 100% !important;
  min-width: 0 !important;
  width: auto !important;
  overflow: visible !important;
  overflow-wrap: anywhere !important;
  hyphens: auto;
}

body {
  margin: 0 !important;
  padding: 0 !important;
}

body > div,
main,
article,
section,
.container,
.jp-Notebook,
.jp-NotebookPanel,
.jp-Cell,
.jp-CodeCell,
.jp-MarkdownCell,
.jp-Cell-inputWrapper,
.jp-Cell-outputWrapper,
.jp-InputArea-editor,
.jp-OutputArea-output,
.jp-RenderedHTMLCommon,
.jp-RenderedText,
.highlight,
.cm-editor,
.cm-scroller,
.cm-content,
.cm-line {
  max-width: 100% !important;
  min-width: 0 !important;
  width: auto !important;
  overflow: visible !important;
}

.jp-InputArea,
.jp-OutputArea,
.jp-OutputArea-child {
  display: block !important;
  max-width: 100% !important;
  min-width: 0 !important;
  width: auto !important;
  overflow: visible !important;
  table-layout: auto !important;
}

.jp-InputPrompt,
.jp-OutputPrompt,
.jp-InputArea-prompt,
.jp-OutputArea-prompt {
  display: none !important;
  width: 0 !important;
  min-width: 0 !important;
  max-width: 0 !important;
  padding: 0 !important;
  overflow: hidden !important;
}

img,
svg,
canvas,
video {
  max-width: 100% !important;
  height: auto !important;
}

table {
  max-width: 100% !important;
  width: 100% !important;
  border-collapse: collapse !important;
  table-layout: fixed !important;
}

th,
td {
  overflow-wrap: anywhere !important;
  word-break: break-all !important;
}

p,
li,
a,
pre,
code,
kbd,
samp {
  overflow-wrap: anywhere !important;
}

pre,
.highlight pre,
.jp-OutputArea-output pre,
.jp-RenderedText pre,
.cm-editor.cm-s-jupyter .highlight pre {
  max-width: 100% !important;
  white-space: pre-wrap !important;
  overflow: visible !important;
  word-break: break-all !important;
}

.jp-RenderedText,
.jp-OutputArea-output,
.output_text,
.output_stream {
  overflow-wrap: anywhere !important;
  white-space: pre-wrap !important;
}
""".strip()


def slugify_title(title: str) -> str:
    return _slugify_title(title)


def _run_render_command(
    cmd: list[str],
    render_timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=render_timeout_seconds,
    )


def _log_render_output(
    result: subprocess.CompletedProcess[str],
    doc_path: str,
    logger: logging.Logger,
) -> None:
    if result.stderr:
        logger.error("Render stderr for %s:\n%s", doc_path, result.stderr)
    if result.stdout:
        logger.error("Render stdout for %s:\n%s", doc_path, result.stdout)


def _format_render_failure(result: subprocess.CompletedProcess[str]) -> str:
    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    if stderr_lines:
        return stderr_lines[-1]

    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if stdout_lines:
        return stdout_lines[-1]

    return f"Render command failed with exit code {result.returncode}"


def _write_pdf_stylesheet(temp_dir: Path) -> Path:
    stylesheet_path = temp_dir / "pdf-render.css"
    stylesheet_path.write_text(PDF_RENDER_STYLESHEET, encoding="utf-8")
    return stylesheet_path


def build_render_output_path(
    output_root: Path, date_str: str, repo_name: str, doc: TrackedDocument
) -> tuple[Path, str]:
    return _build_render_output_path(output_root, date_str, repo_name, doc)


def build_output_relpath_by_doc(
    docs: dict[str, TrackedDocument],
    repo_name: str,
) -> dict[str, str]:
    relpaths: dict[str, str] = {}
    for doc in docs.values():
        date_str = resolve_output_date(doc.date)
        relpaths[doc.path] = str(build_output_relative_path(date_str, repo_name, doc))
    return relpaths


def _prepare_markdown_source(
    path: Path,
    doc: TrackedDocument,
    output_relpath_by_doc: dict[str, str] | None,
    logger: logging.Logger,
) -> None:
    if not output_relpath_by_doc:
        return

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug("Failed reading markdown for link rewrite %s: %s", path, e)
        return

    rewritten = rewrite_markdown_links(content, doc.path, output_relpath_by_doc)
    if rewritten == content:
        return

    path.write_text(rewritten, encoding="utf-8")
    logger.debug("Rewrote markdown links for %s", doc.path)


def _prepare_notebook_source(
    path: Path,
    doc: TrackedDocument,
    output_relpath_by_doc: dict[str, str] | None,
    logger: logging.Logger,
) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            nb_data = json.load(f)
    except Exception as e:
        logger.debug("Failed reading notebook for preprocessing %s: %s", path, e)
        return

    changed = False
    metadata = nb_data.get("metadata")
    if isinstance(metadata, dict) and "widgets" in metadata:
        del metadata["widgets"]
        changed = True
        logger.debug("Removed .metadata.widgets from %s", path)

    if rewrite_notebook_markdown_cells(nb_data, doc.path, output_relpath_by_doc):
        changed = True
        logger.debug("Rewrote notebook markdown links for %s", doc.path)

    if not changed:
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(nb_data, f)


def render_document_to_pdf(
    doc: TrackedDocument,
    repo_path: Path,
    output_dir: Path,
    logger: logging.Logger,
    repo_name: str = "",
    output_relpath_by_doc: dict[str, str] | None = None,
    render_timeout_seconds: int = RENDER_TIMEOUT_SECONDS,
) -> RenderedDocument:
    source_path = repo_path / doc.path
    if not source_path.is_file():
        warning = f"Source file not found: {source_path}"
        logger.warning(warning)
        return RenderedDocument(doc=doc, repo_name=repo_name, error=warning)

    date_str = resolve_output_date(doc.date)

    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="anthropic-render-"))
        temp_source = temp_dir / source_path.name
        stylesheet_path = _write_pdf_stylesheet(temp_dir)
        shutil.copy2(source_path, temp_source)

        if source_path.suffix == ".ipynb":
            _prepare_notebook_source(temp_source, doc, output_relpath_by_doc, logger)
        elif source_path.suffix == ".md":
            _prepare_markdown_source(temp_source, doc, output_relpath_by_doc, logger)

        folder_dir, name = build_render_output_path(
            output_dir, date_str, repo_name, doc
        )

        pdf_path = folder_dir / name

        if source_path.suffix == ".md":
            cmd = [
                "pandoc",
                str(temp_source),
                "-o",
                str(pdf_path),
                "--pdf-engine=weasyprint",
                f"--css={stylesheet_path}",
            ]
            logger.info(f"Rendering {source_path} to PDF via pandoc")

            result = _run_render_command(cmd, render_timeout_seconds)
        else:
            html_stem = Path(name).stem
            html_path = folder_dir / f"{html_stem}.html"
            html_cmd = [
                "jupyter",
                "nbconvert",
                "--to=html",
                "--embed-images",
                f"--output={html_stem}",
                f"--output-dir={folder_dir}",
                str(temp_source),
            ]
            logger.info(f"Rendering {source_path} to HTML via nbconvert")

            result = _run_render_command(html_cmd, render_timeout_seconds)
            if result.returncode == 0 and not html_path.is_file():
                error_msg = "Notebook HTML file not created"
                logger.error(error_msg)
                return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)

            if result.returncode == 0:
                pdf_cmd = [
                    "weasyprint",
                    "--stylesheet",
                    str(stylesheet_path),
                    str(html_path),
                    str(pdf_path),
                ]
                logger.info(f"Rendering {source_path} HTML to PDF via weasyprint")
                result = _run_render_command(pdf_cmd, render_timeout_seconds)

        if result.returncode != 0:
            error_msg = _format_render_failure(result)
            _log_render_output(result, doc.path, logger)
            logger.error(f"PDF rendering failed for {doc.path}: {error_msg}")
            return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)

        if not pdf_path.is_file():
            error_msg = "PDF file not created"
            logger.error(error_msg)
            return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)

        logger.info(f"PDF created: {pdf_path}")
        return RenderedDocument(doc=doc, repo_name=repo_name, pdf_path=pdf_path)

    except subprocess.TimeoutExpired:
        error_msg = f"PDF rendering timed out after {render_timeout_seconds} seconds"
        logger.error(f"{doc.path}: {error_msg}")
        return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"PDF rendering failed for {doc.path}: {error_msg}")
        return RenderedDocument(doc=doc, repo_name=repo_name, error=error_msg)
    finally:
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir)
            logger.debug(f"Cleaned up temp dir: {temp_dir}")
