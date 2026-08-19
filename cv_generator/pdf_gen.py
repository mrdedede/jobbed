"""Render one stored CV into the PDF a human reads.

The docx already lays out everything correctly -- name, contact line, the
template's anchored photo, fonts, spacing -- so this does not redraw any of
it. It converts the filled docx to PDF with LibreOffice headless
(`soffice --convert-to pdf`), which renders that same layout faithfully
instead of re-implementing it with a second, drifting set of styles.

No ATS trade-off applies to a PDF the way it does to a docx: a parser skips
the photo either way, so this module has no `photo` flag. The ATS-safe copy
stays `docx_gen.render_docx(..., photo=False)`.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List

from cv_generator.docx_gen import Line
from cv_generator import docx_gen
from job_scraper import paths

_CONVERT_TIMEOUT_S = 60


def render_pdf(blocks: Dict[str, List[Line]], l10n: Dict[str, str],
               template: Path = paths.CV_TEMPLATE_DOCX) -> bytes:
    """Fill `template` into a docx, then convert that docx to PDF, photo kept.

    Args:
        blocks: Output of `docx_gen.sections`.
        l10n: Output of `docx_gen.load_l10n`, for the section titles.
        template: The .docx to fill -- never mutated.

    Returns:
        The PDF, as bytes -- what `st.download_button` wants.

    Raises:
        FileNotFoundError: If the template is not there.
        RuntimeError: If the template is invalid, or LibreOffice is missing
            or fails to convert the filled docx.
    """
    docx_bytes = docx_gen.render_docx(blocks, l10n, photo=True,
                                      template=template)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        docx_path = tmpdir / "cv.docx"
        docx_path.write_bytes(docx_bytes)

        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf",
                 "--outdir", str(tmpdir), str(docx_path)],
                check=True, capture_output=True, timeout=_CONVERT_TIMEOUT_S)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "LibreOffice (soffice) is not installed -- install it to "
                "generate a PDF, e.g. `brew install --cask libreoffice`"
            ) from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"docx to PDF conversion failed: {exc}") from exc

        return (tmpdir / "cv.pdf").read_bytes()


if __name__ == "__main__":
    from tests.test_docx_gen import CV, TEMPLATE

    _blocks = docx_gen.sections(CV)
    _l10n = docx_gen.load_l10n("en")
    _pdf = render_pdf(_blocks, _l10n, template=TEMPLATE)
    assert _pdf.startswith(b"%PDF-"), "output is not a PDF"
    print(f"OK -- rendered {len(_pdf)} bytes")
