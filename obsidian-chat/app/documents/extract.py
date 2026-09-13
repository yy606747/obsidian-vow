"""Bounded, disposable parser process. No model calls or application imports."""
from __future__ import annotations

import codecs
import json
from pathlib import Path
import sys
import unicodedata
import zipfile

MAX_CHARS = 60_000
MAX_PAGES = 300


def check_length(text: str) -> str:
    if len(text) > MAX_CHARS:
        raise ValueError(f"正文超过 {MAX_CHARS:,} 字，请拆成几个文件再上传。")
    return text


def extract(path: Path) -> dict:
    suffix = path.suffix.lower()
    warnings = []
    pages = None
    if suffix in {'.txt', '.md'}:
        data = path.read_bytes()
        if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            text = data.decode('utf-32')
        elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            text = data.decode('utf-16')
        else:
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = data.decode('gb18030')
        if any(ord(c) < 32 and c not in '\t\n\r' for c in text):
            raise ValueError('文件含有无法识别的字符，请另存为 UTF-8 文本后上传。')
        text = text.replace('\r\n', '\n').replace('\r', '\n')
    elif suffix == '.docx':
        from docx import Document
        from docx.table import Table
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > 80 * 1024 * 1024:
                raise ValueError('Word 文件展开后过大，请移除大图或拆分后上传。')
            if 'word/document.xml' not in archive.namelist():
                raise ValueError('这不是有效的 DOCX 文档。')
        document = Document(path)
        parts = []
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                rows = []
                for row in block.rows:
                    cells = [cell.text.replace('\n', ' / ').replace('|', '\\|') for cell in row.cells]
                    rows.append('| ' + ' | '.join(cells) + ' |')
                if rows:
                    rows.insert(1, '| ' + ' | '.join('---' for _ in block.rows[0].cells) + ' |')
                parts.append('\n'.join(rows))
            else:
                text = block.text
                style = block.style.name if block.style else ''
                if style.startswith('Heading ') and style[8:].isdigit():
                    text = '#' * min(int(style[8:]), 6) + ' ' + text
                parts.append(text)
            check_length('\n\n'.join(parts))
        text = '\n\n'.join(parts)
    elif suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF 有密码保护，请解除密码后再上传。')
        pages = len(reader.pages)
        if pages > MAX_PAGES:
            raise ValueError(f'PDF 超过 {MAX_PAGES} 页，请拆分后上传。')
        parts, empty = [], []
        for number, page in enumerate(reader.pages, 1):
            # Some PDF fonts map ordinary CJK glyphs to compatibility radicals.
            body = unicodedata.normalize('NFKC', page.extract_text() or '').strip()
            if not body:
                empty.append(number)
            parts.append(f'〔第 {number} 页〕\n{body}' if body else f'〔第 {number} 页：未提取到文字〕')
            check_length('\n\n'.join(parts))
        if len(empty) == pages:
            raise ValueError('这份 PDF 没有可提取的文字，可能是扫描版；当前支持文字型 PDF。')
        if empty:
            numbers = '、'.join(map(str, empty[:15])) + ('等' if len(empty) > 15 else '')
            warnings.append(f'第 {numbers} 页未提取到文字，可能是图片或空白页。')
        text = '\n\n'.join(parts)
    else:
        raise ValueError('支持 PDF、TXT、DOCX 和 MD 文件。')
    check_length(text)
    if not text.strip():
        raise ValueError('文件中没有可读取的正文。')
    return {'text': text, 'char_count': len(text), 'page_count': pages, 'warnings': warnings}


if __name__ == '__main__':
    # Limits apply only to this parser, never to the chat service.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 ** 2, 512 * 1024 ** 2))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    try:
        result = extract(Path(sys.argv[1]))
    except (ValueError, UnicodeError) as exc:
        result = {'error': str(exc)}
    except Exception:
        result = {'error': '文件无法解析，请确认文件完整，或另存后重试。'}
    print(json.dumps(result, ensure_ascii=False))
