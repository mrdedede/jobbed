"""The PDF is a converted docx, so its own tests only check the conversion:
it produces PDF bytes, and a missing template still fails the same way
`docx_gen.render_docx` fails.
"""

import shutil

import pytest

from cv_generator import docx_gen, pdf_gen
from job_scraper import paths
from tests.test_docx_gen import CV, TEMPLATE

pytestmark = pytest.mark.skipif(
    shutil.which("soffice") is None,
    reason="LibreOffice (soffice) is not installed")


@pytest.fixture
def blocks():
    return docx_gen.sections(CV)


@pytest.fixture
def l10n():
    return docx_gen.load_l10n("en")


def test_renders_bytes_from_the_example_template(blocks, l10n):
    data = pdf_gen.render_pdf(blocks, l10n, template=TEMPLATE)

    assert data.startswith(b"%PDF")


def test_a_missing_template_says_what_to_copy(tmp_path, blocks, l10n):
    with pytest.raises(FileNotFoundError, match="CV_placeholder_example"):
        pdf_gen.render_pdf(blocks, l10n, template=tmp_path / "gone.docx")


@pytest.mark.skipif(not paths.CV_TEMPLATE_DOCX.exists(),
                    reason="real template is gitignored, not present here")
def test_the_real_template_s_photo_survives_into_the_pdf(blocks, l10n):
    data = pdf_gen.render_pdf(blocks, l10n, template=paths.CV_TEMPLATE_DOCX)

    assert len(data) > 10_000  # an embedded jpeg makes an empty-CV PDF this big
