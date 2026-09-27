"""Duplicate-option validation: case sensitivity for LaTeX/math options.

The validator used to lower-case every option before comparing, which made
mathematically different expressions look identical: \\frac{G}{g} vs
\\frac{g}{G}, or R_E^2 vs r_e^2. Options containing math/LaTeX notation are
now compared with case preserved; ordinary prose keeps the long-standing
case-insensitive comparison. Nothing else about the comparison changed —
HTML tags are still stripped, surrounding whitespace still trimmed, and
spacing / delimiter style are deliberately NOT normalised (tests below pin
that so a future change to them has to be intentional).
"""
from django.test import SimpleTestCase

from academics.import_validation import (
    BYPASSABLE_ISSUE_CODES, ISSUE_BLANK_QUESTION, ISSUE_DUPLICATE_OPTION, ISSUE_NO_CORRECT_OPTION,
    ISSUE_TOO_FEW_OPTIONS, collect_validation_issues, validate_parsed_question,
)


def _pq(*option_html, correct=0):
    return {
        'text_html': '<p>Q</p>',
        'options': [{'text_html': h, 'is_correct': i == correct, 'image_path': ''} for i, h in enumerate(option_html)],
        'explanation_html': '<p>e</p>',
    }


def _dup_errors(*option_html):
    errors, _ = validate_parsed_question(_pq(*option_html))
    return [e for e in errors if 'duplicates' in e]


class LatexDuplicateCaseTests(SimpleTestCase):
    def test_identical_latex_options_are_duplicates(self):
        errs = _dup_errors(r'<p>$\frac{G}{g}R_E^2$</p>', r'<p>$\frac{G}{g}R_E^2$</p>')
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith('Option 2 duplicates option 1'))

    def test_latex_differing_only_in_case_is_not_a_duplicate(self):
        self.assertEqual(_dup_errors(r'<p>$\frac{G}{g}R_E^2$</p>', r'<p>$\frac{g}{G}R_E^2$</p>'), [])

    def test_subscript_case_difference_is_not_a_duplicate(self):
        self.assertEqual(_dup_errors('<p>$R_E^2$</p>', '<p>$r_e^2$</p>'), [])

    def test_latex_without_delimiters_is_also_treated_as_math(self):
        self.assertEqual(_dup_errors('<p>R_E^2</p>', '<p>r_e^2</p>'), [])
        self.assertEqual(len(_dup_errors('<p>R_E^2</p>', '<p>R_E^2</p>')), 1)

    def test_paren_delimited_latex_is_case_sensitive_too(self):
        self.assertEqual(_dup_errors(r'<p>\(\frac{G}{g}\)</p>', r'<p>\(\frac{g}{G}\)</p>'), [])
        self.assertEqual(len(_dup_errors(r'<p>\(\frac{G}{g}\)</p>', r'<p>\(\frac{G}{g}\)</p>')), 1)

    def test_error_message_shows_the_original_case_not_a_lowercased_key(self):
        errs = _dup_errors(r'<p>$\frac{G}{g}R_E^2$</p>', r'<p>$\frac{G}{g}R_E^2$</p>')
        self.assertIn(r'$\frac{G}{g}R_E^2$', errs[0])
        self.assertNotIn(r'$\frac{g}{g}r_e^2$', errs[0])


class ExistingBehaviourUnchangedTests(SimpleTestCase):
    """Behaviour that must NOT have changed."""

    def test_ordinary_text_is_still_compared_case_insensitively(self):
        errs = _dup_errors('<p>Newton</p>', '<p>newton</p>')
        self.assertEqual(len(errs), 1)
        self.assertIn('"newton"', errs[0])     # shows the option's own text as typed

    def test_html_only_difference_is_still_a_duplicate(self):
        self.assertEqual(len(_dup_errors('<p><strong>Newton</strong></p>', '<p>Newton</p>')), 1)
        self.assertEqual(len(_dup_errors(r'<p>$x^2$</p>', r'<p><em>$x^2$</em></p>')), 1)

    def test_surrounding_whitespace_is_still_trimmed(self):
        self.assertEqual(len(_dup_errors('<p>  Newton  </p>', '<p>Newton</p>')), 1)
        self.assertEqual(len(_dup_errors(r'<p>  $x^2$ </p>', r'<p>$x^2$</p>')), 1)

    def test_internal_spacing_differences_are_still_not_normalised(self):
        self.assertEqual(_dup_errors(r'<p>$\frac{G}{g}R_E^2$</p>', r'<p>$\frac{G}{g} R_E^2$</p>'), [])

    def test_delimiter_style_differences_are_still_not_normalised(self):
        self.assertEqual(_dup_errors(r'<p>$x^2$</p>', r'<p>\(x^2\)</p>'), [])

    def test_different_math_is_not_a_duplicate(self):
        self.assertEqual(_dup_errors(r'<p>$\frac{G}{g}R_E^2$</p>', r'<p>$\frac{G}{g}R_E^3$</p>'), [])

    def test_image_only_options_are_never_flagged(self):
        pq = {
            'text_html': '<p>Q</p>',
            'options': [
                {'text_html': '', 'is_correct': True, 'image_path': 'a.png'},
                {'text_html': '', 'is_correct': False, 'image_path': 'b.png'},
            ],
            'explanation_html': '<p>e</p>',
        }
        errors, _ = validate_parsed_question(pq)
        self.assertEqual([e for e in errors if 'duplicates' in e], [])

    def test_validate_parsed_question_keeps_its_public_shape(self):
        errors, warnings = validate_parsed_question({'text_html': '', 'options': []})
        self.assertIsInstance(errors, list)
        self.assertTrue(all(isinstance(e, str) for e in errors))
        self.assertIn('Question text is blank.', errors)
        self.assertIn('No option is marked correct — check the answer key.', errors)


class IssueCodeTests(SimpleTestCase):
    def test_each_error_has_a_stable_code(self):
        issues, _ = collect_validation_issues({'text_html': '', 'options': []})
        self.assertEqual({i.code for i in issues}, {ISSUE_BLANK_QUESTION, ISSUE_TOO_FEW_OPTIONS, ISSUE_NO_CORRECT_OPTION})
        dup, _ = collect_validation_issues(_pq('<p>A</p>', '<p>A</p>'))
        self.assertEqual([i.code for i in dup], [ISSUE_DUPLICATE_OPTION])

    def test_only_a_duplicate_option_is_bypassable(self):
        self.assertEqual(BYPASSABLE_ISSUE_CODES, frozenset({ISSUE_DUPLICATE_OPTION}))
        issues, _ = collect_validation_issues({'text_html': '', 'options': []})
        self.assertFalse(any(i.bypassable for i in issues))
        dup, _ = collect_validation_issues(_pq('<p>A</p>', '<p>A</p>'))
        self.assertTrue(all(i.bypassable for i in dup))
