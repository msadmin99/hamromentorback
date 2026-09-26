"""Word (.docx) import — walks paragraphs looking for a documented
question/option/answer pattern (numbered questions, lettered options,
"Answer:"/"Explanation:" markers). Preserves superscript/subscript (critical
for Physics/Chemistry notation — x², H₂O) and bold/italic by walking run
properties; extracts embedded images via the paragraph's own drawing
relationships. This is a documented-convention parser, not free-form NLP —
faculty following the template's structure get reliable results; documents
that don't follow the pattern degrade to "no questions found," not silent
corruption.

Subject, Chapter, Topic and Course are NOT part of the file — the admin
selects them for the whole batch on the Preview & Validate screen. Marks and
negative marks aren't part of the file either — those are set later in Exam
Management.

BOLD IN OPTIONS: some source documents bold the correct option as the
author's own visual convention while typing — this parser never reads that
bold as the answer signal (the explicit "Answer: <letter>" line below is
the only source of truth for is_correct), and per product requirement,
bold is never legitimate student-facing formatting on an option either way
— full-word or partial, correct option or distractor. Option paragraphs are
therefore always rendered with bold suppressed (see `keep_bold=False`
below); question and explanation paragraphs are unaffected and keep bold
exactly as authored.

Expected structure (also shown in the downloadable .docx template):

    Q1. A ball is thrown vertically upward. What is its acceleration
    at the highest point?
    A) Zero
    B) g, downward
    C) g, upward
    D) Depends on mass
    Answer: B
    Explanation: At the highest point velocity is zero but gravity
    still acts downward.
"""
import re

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table

from .base import ParsedQuestion, save_temp_image

QUESTION_RE = re.compile(r'^\s*(Q\.?\s*)?(\d+)\s*[.):]\s*')
OPTION_RE = re.compile(r'^\s*([A-Da-d])\s*[.):]\s*')
ANSWER_RE = re.compile(r'^\s*Answer\s*:?\s*([A-Da-d])', re.IGNORECASE)
EXPLANATION_RE = re.compile(r'^\s*Explanation\s*:?\s*')

# Bulk-import "false Error" audit: a question/option/explanation authored
# via Word's native Insert -> Equation tool (e.g. a gravity formula like
# G*R_E^2/g) is stored as OMML (<m:oMath>), a completely separate XML
# structure from the plain <w:t> text runs python-docx's `Run.text`
# reads. `para.runs` never sees an OMML equation's content at all — not
# because the run is empty, but because it isn't a `<w:r>` text run in
# the first place — so a paragraph consisting ONLY of such an equation
# previously produced an empty `_paragraph_html()`, which
# validate_parsed_question then reported as "Question text is blank": a
# false Error for a genuinely non-blank, faculty-authored question.
_MATH_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'


def _paragraph_math_text(para):
    """Best-effort textual recovery of any OMML equation(s) in this
    paragraph, via OMML's own text nodes (<m:t>, distinct from <w:t>).
    This is NOT a full OMML->LaTeX structural conversion — fraction bars,
    exponent/subscript layout, radicals etc. are not reconstructed, only
    the underlying symbols/characters — but it is enough that an
    equation's content is never silently dropped to nothing. Returns ''
    if the paragraph has no OMML content at all (the overwhelmingly
    common case), so normal text-only paragraphs are completely
    unaffected by this function's existence."""
    texts = para._element.findall(f'.//{_MATH_NS}t')  # noqa: SLF001 - python-docx has no public OMML accessor
    return ''.join(t.text or '' for t in texts)


def _run_html(run, text, keep_bold=True):
    if not text:
        return ''
    escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # A soft line break (Shift+Enter, python-docx's Run.text renders it as a
    # literal "\n" WITHIN one run/paragraph — a real reported bug: a
    # "Statement: ... / Arguments: / I. ... / II. ..." reasoning question
    # authored as ONE paragraph with manual line breaks (a very common way
    # to compose exactly this style of question) previously ran together
    # into a single unbroken line on the student view, because a bare "\n"
    # inside an HTML text node is just collapsed to whitespace by the
    # browser's default rendering — the line structure the source document
    # actually had was silently destroyed on the way in. This must happen
    # BEFORE the sup/sub/em/strong wrapping below, not after, so a <br>
    # that falls mid-way through a bold/italic run still nests correctly
    # (e.g. "<strong>line one<br>line two</strong>", not a <br> stranded
    # outside its own run's formatting).
    escaped = escaped.replace('\n', '<br>')
    if run.font.superscript:
        escaped = f'<sup>{escaped}</sup>'
    elif run.font.subscript:
        escaped = f'<sub>{escaped}</sub>'
    if run.italic:
        escaped = f'<em>{escaped}</em>'
    if run.bold and keep_bold:
        escaped = f'<strong>{escaped}</strong>'
    return escaped


def _paragraph_inline_html(para, strip_prefix_len=0, keep_bold=True):
    """The same run-by-run rendering `_paragraph_html` wraps in `<p>` —
    split out so a table cell (which needs the bare inline content, not a
    block-level paragraph nested inside a <td>) can reuse the exact same
    bold/italic/sup-sub/equation-recovery logic instead of duplicating it."""
    parts = []
    consumed = 0
    for run in para.runs:
        text = run.text or ''
        if consumed < strip_prefix_len:
            cut = min(strip_prefix_len - consumed, len(text))
            text = text[cut:]
            consumed += cut
        if text:
            parts.append(_run_html(run, text, keep_bold=keep_bold))
    inline = ''.join(parts).strip()
    if not inline:
        # No plain-text runs at all — check for a Word-native equation
        # before concluding the paragraph is genuinely blank. See
        # _paragraph_math_text()'s docstring above for why this is
        # necessary and what it does/doesn't reconstruct.
        math_text = _paragraph_math_text(para).strip()
        if math_text:
            inline = math_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return inline


def _paragraph_html(para, strip_prefix_len=0, keep_bold=True):
    """Renders one paragraph's runs to an HTML fragment.

    `keep_bold=False` (used for OPTION paragraphs only — see module
    docstring) unconditionally drops any `<strong>` wrapping a bold run
    would otherwise get, regardless of whether the bold covers the whole
    option or just part of it, and regardless of whether the option turns
    out to be the correct answer. Sup/sub/italic are never affected by
    this flag. Question and explanation paragraphs always call this with
    the default `keep_bold=True`, so bold there is completely unchanged."""
    inline = _paragraph_inline_html(para, strip_prefix_len=strip_prefix_len, keep_bold=keep_bold)
    return f'<p>{inline}</p>' if inline else ''


def _table_html(table):
    """Renders a native Word table (a docx.table.Table encountered while
    walking the document body) as a plain HTML <table> fragment — cell
    formatting (bold/italic/sup/sub, even an OMML equation) goes through
    the exact same per-run rendering as a normal paragraph via
    _paragraph_inline_html, so a table cell is never a second, lesser-
    featured text path. Multiple paragraphs within one cell join on
    <br> rather than nesting block-level <p> tags inside a <td>, matching
    how a real Word table cell reads (a soft return between short lines,
    not separate document sections). Produces plain <table>/<tr>/<td>
    markup with no wrapper of its own — this is what both the Admin's
    TipTap-based Preview & Validate editor (RichEditor.js's Table
    extension) and the student Frontend's DOMPurify-sanitized RichContent
    renderer already round-trip natively, so no new storage field or
    schema change is needed: it lives in the same text_html/explanation_html
    string as everything else."""
    rows_html = []
    for row in table.rows:
        cells_html = []
        for cell in row.cells:
            inline = '<br>'.join(h for h in (_paragraph_inline_html(p) for p in cell.paragraphs) if h)
            cells_html.append(f'<td>{inline}</td>')
        rows_html.append(f'<tr>{"".join(cells_html)}</tr>')
    return f'<table>{"".join(rows_html)}</table>' if rows_html else ''


def _paragraph_image(para, document, batch_id, slot):
    if not batch_id:
        return ''
    for run in para.runs:
        for blip in run._element.findall('.//' + qn('a:blip')):  # noqa: SLF001 - python-docx has no public run-image accessor
            rid = blip.get(qn('r:embed'))
            if not rid:
                continue
            try:
                blob = document.part.related_parts[rid].blob
            except KeyError:
                continue
            return save_temp_image(batch_id, slot, blob, ext='png')
    return ''


def _attach_section_image_if_missing(para, document, batch_id, current, mode, questions):
    """Fills whichever section's single image slot (question stem /
    current option / explanation) is still empty, from an embedded image
    on `para` — used for BOTH a paragraph that also carries real caption
    text (a continuation line) and a paragraph that is ONLY an image with
    no text at all (a bare diagram on its own line), which would
    otherwise never reach any image-extraction call at all: the caller's
    `if not raw_text: continue` guard skips a text-less paragraph before
    ever checking it for an image. A diagram/photo is just as likely to
    sit on a section's second (or later) line as its very first — only
    the first line's image used to be checked. Each slot still holds at
    most one image (the model fields are single-valued), so this only
    fills a slot that's still empty — it never overwrites an image
    already found on an earlier line of the same section."""
    if mode == 'question' and not current.get('question_image_path'):
        img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_question')
        if img:
            current['question_image_path'] = img
    elif mode == 'option' and current['options'] and not current['options'][-1].get('image_path'):
        img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_option{len(current["options"])}')
        if img:
            current['options'][-1]['image_path'] = img
    elif mode == 'explanation' and not current.get('explanation_image_path'):
        img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_explanation')
        if img:
            current['explanation_image_path'] = img


def parse_docx(file_obj, batch_id=None):
    try:
        document = Document(file_obj)
    except Exception as exc:  # noqa: BLE001 - surface any python-docx failure as a clean validation error
        raise ValueError(f'Could not read that Word document: {exc}') from exc

    questions = []
    current = None
    mode = None

    def flush():
        nonlocal current
        if current is not None and current['text_html']:
            questions.append(current)
        current = None

    for block in document.iter_inner_content():
        if isinstance(block, Table):
            # A native Word table (Insert > Table, e.g. a reasoning-question
            # matrix/grid) — document.paragraphs alone structurally excludes
            # these (a <w:tbl> is a body-level sibling of <w:p>, not a
            # paragraph), so without this branch its cell contents were
            # never visited at all and the question silently lost its data.
            # iter_inner_content() (python-docx >= 1.1) walks paragraphs and
            # tables together in true document order, so a table is
            # attached to whichever section was open at the point it
            # actually appears in the file — same as any other continuation
            # content below.
            if current is None:
                continue  # stray table before the first recognized question marker
            table_html = _table_html(block)
            if not table_html:
                continue
            if mode == 'question':
                current['text_html'] += table_html
            elif mode == 'option' and current['options']:
                current['options'][-1]['text_html'] += table_html
            elif mode == 'explanation':
                current['explanation_html'] += table_html
            continue

        para = block
        raw_text = para.text.strip()
        if not raw_text:
            if current is not None:
                _attach_section_image_if_missing(para, document, batch_id, current, mode, questions)
            continue

        m = QUESTION_RE.match(raw_text)
        # A bare "12." (no "Q" prefix) is ambiguous with a decimal value like
        # "0.5 N — ..." that a rich explanation's per-option breakdown often
        # starts a line with — only trust it as a genuine new question when
        # it's explicitly "Q12." or we're not already inside an explanation
        # (a real new question never starts mid-explanation without a marker).
        if m and (m.group(1) or mode != 'explanation'):
            flush()
            current = ParsedQuestion()
            mode = 'question'
            current['text_html'] = _paragraph_html(para, strip_prefix_len=m.end())
            img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_question')
            if img:
                current['question_image_path'] = img
            continue

        if current is None:
            continue  # stray text before the first recognized question marker

        m = OPTION_RE.match(raw_text)
        # Same ambiguity as above: a "Detailed Option Analysis" breakdown
        # inside the explanation commonly repeats "A) ... / B) ... / C) ..."
        # to justify each choice — that must stay part of the explanation,
        # never be read as a second set of real options for the question.
        if m and mode != 'explanation':
            mode = 'option'
            html_val = _paragraph_html(para, strip_prefix_len=m.end(), keep_bold=False)
            img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_option{len(current["options"]) + 1}')
            current['options'].append({'text_html': html_val, 'is_correct': False, 'image_path': img})
            continue

        m = ANSWER_RE.match(raw_text)
        if m and mode != 'explanation':
            letter = m.group(1).upper()
            for i, opt in enumerate(current['options']):
                opt['is_correct'] = chr(ord('A') + i) == letter
            mode = None
            continue

        m = EXPLANATION_RE.match(raw_text)
        if m and mode != 'explanation' and raw_text.lower().startswith('explanation'):
            mode = 'explanation'
            current['explanation_html'] = _paragraph_html(para, strip_prefix_len=m.end())
            img = _paragraph_image(para, document, batch_id, f'q{len(questions) + 1}_explanation')
            if img:
                current['explanation_image_path'] = img
            continue

        # plain continuation of whichever section is currently open
        html_val = _paragraph_html(para, keep_bold=(mode != 'option'))
        _attach_section_image_if_missing(para, document, batch_id, current, mode, questions)
        if not html_val:
            continue
        if mode == 'question':
            current['text_html'] += html_val
        elif mode == 'option' and current['options']:
            current['options'][-1]['text_html'] += html_val
        elif mode == 'explanation':
            current['explanation_html'] += html_val

    flush()
    return questions


def build_template_docx():
    document = Document()
    document.add_paragraph('Q1. A ball is thrown vertically upward. What is its acceleration at the highest point?')
    document.add_paragraph('A) Zero')
    document.add_paragraph('B) g, downward')
    document.add_paragraph('C) g, upward')
    document.add_paragraph('D) Depends on mass')
    document.add_paragraph('Answer: B')
    document.add_paragraph('Explanation: At the highest point velocity is zero but gravity still acts downward.')
    document.add_paragraph('')
    document.add_paragraph('Q2. Which ion is formed when sulfuric acid (H2SO4) fully dissociates, alongside SO4²⁻?')
    document.add_paragraph('A) H⁺')
    document.add_paragraph('B) OH⁻')
    document.add_paragraph('C) NH4⁺')
    document.add_paragraph('D) Fe³⁺')
    document.add_paragraph('Answer: A')
    document.add_paragraph('Explanation: H2SO4 -> 2H+ + SO4^2-')
    return document
