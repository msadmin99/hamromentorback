from .base import ParsedQuestion, save_temp_image
from .csv_parser import parse_csv
from .docx_parser import parse_docx
from .json_parser import parse_json
from .xlsx_parser import parse_xlsx

PARSERS = {
    'docx': parse_docx,
    'xlsx': parse_xlsx,
    'csv': parse_csv,
    'json': parse_json,
}

__all__ = ['ParsedQuestion', 'save_temp_image', 'PARSERS']
