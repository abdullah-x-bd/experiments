#!/usr/bin/env python3
"""Budget-aware diagnostic review of DOCX manuscripts with OpenRouter."""
import argparse
import base64
import json
import os
import posixpath
import re
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
def q(ns, name):
    return '{' + ns + '}' + name

MODEL = 'anthropic/claude-fable-5.2'
SYSTEM = (
    "Act as an exacting, adversarial anonymous peer reviewer for Jindal Global Law Review. "
    "Diagnose, never rewrite. Scrutinise each paragraph, sentence, consequential word, legal claim, "
    "inference, authority, footnote, definition and methodological choice. Assess comparative-law "
    "doctrine, novelty, alternative institutional designs, counterarguments, JGLR suitability and "
    "OSCOLA-style citations. During granular reviews, assess EVERY assigned paragraph ID explicitly, including "
    "when no meaningful problem is found. For each issue: exact ID and quote, severity "
    "(critical/major/minor/editorial), detailed explanation, strongest objection, and actionable "
    "correction direction. Differentiate source-derived claims, inferences and material needing "
    "verification. You have no browsing tools: never claim to have independently verified a "
    "citation or law. Never fabricate sources. Be precise, severe and complete, not complimentary. "
    "Apply JGLR's 2023 stylesheet: indicative 8,000-10,000 words for long articles excluding "
    "footnotes; British spelling; at most 250 words in abstract; five keywords; numbered headings; "
    "OSCOLA with JGLR house-style priority, and careful quotation and pinpoint conventions. "
    "Judge suitability as comparative, interdisciplinary legal scholarship engaging India and "
    "the Global South. Do not claim to have inspected visual formatting not in the extracted text."
)

def node_text(node):
    parts = []
    for el in node.iter():
        if el.tag == q(W, 't'):
            parts.append(el.text or '')
        elif el.tag == q(W, 'tab'):
            parts.append('\t')
        elif el.tag in (q(W, 'br'), q(W, 'cr')):
            parts.append('\n')
        elif el.tag == q(W, 'footnoteReference'):
            parts.append(' [FN:' + str(el.get(q(W, 'id'))) + '] ')
    return ''.join(parts).strip()

def extract_docx(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read('word/document.xml'))
        footnotes = {}
        if 'word/footnotes.xml' in z.namelist():
            fn_root = ET.fromstring(z.read('word/footnotes.xml'))
            footnotes = {
                n.get(q(W, 'id')): ' '.join(node_text(p) for p in n.iter(q(W, 'p'))).strip()
                for n in fn_root.iter(q(W, 'footnote'))
                if (n.get(q(W, 'id')) or '').isdigit()
            }
        rels = {}
        if 'word/_rels/document.xml.rels' in z.namelist():
            rel_root = ET.fromstring(z.read('word/_rels/document.xml.rels'))
            for relation in rel_root:
                if relation.tag != q(PKG, 'Relationship'):
                    continue
                target = relation.get('Target', '')
                if target.startswith('http'):
                    continue
                rels[relation.get('Id')] = (
                    target.lstrip('/') if target.startswith('/')
                    else posixpath.normpath(posixpath.join('word', target))
                )
        media, paragraphs = {}, []
        body = root.find(q(W, 'body'))
        if body is None:
            raise ValueError('DOCX has no Word document body')

        def append_paragraph(p, prefix=''):
            t = node_text(p)
            for fn in sorted(set(re.findall(r'\[FN:(\d+)\]', t)), key=int):
                t += '\n  [FN:' + fn + ' text] ' + footnotes.get(fn, 'MISSING FOOTNOTE TEXT')
            images = []
            for el in p.iter():
                if el.tag != q(A, 'blip'):
                    continue
                path = rels.get(el.get(q(R, 'embed')))
                if path and path in z.namelist():
                    images.append(path)
                    if path not in media:
                        media[path] = z.read(path)
            if images:
                t += '\n  [EMBEDDED FIGURE: ' + ', '.join(sorted(set(images))) + ']'
            if not t.strip():
                return
            props = p.find(q(W, 'pPr'))
            style = ''
            if props is not None:
                tag = props.find(q(W, 'pStyle'))
                if tag is not None:
                    style = tag.get(q(W, 'val'), '')
            paragraphs.append({
                'id': 'P' + str(len(paragraphs) + 1).zfill(4),
                'text': prefix + (('[' + style + '] ') if style else '') + t,
                'images': images,
            })

        for block in body:
            if block.tag == q(W, 'p'):
                append_paragraph(block)
            elif block.tag == q(W, 'tbl'):
                for row_no, row in enumerate(block.findall(q(W, 'tr')), 1):
                    for col_no, cell in enumerate(row.findall(q(W, 'tc')), 1):
                        for p in cell.iter(q(W, 'p')):
                            append_paragraph(p, '[TABLE r' + str(row_no) + ' c' + str(col_no) + '] ')
        return paragraphs, media

def group_paragraphs(paragraphs, target_words):
    batches, current, count = [], [], 0
    for p in paragraphs:
        words = len(p['text'].split())
        if current and count + words > target_words:
            batches.append(current)
            current, count = [], 0
        current.append(p)
        count += words
    if current:
        batches.append(current)
    return batches

def lines(paragraphs):
    return '\n\n'.join(p['id'] + ' | ' + p['text'] for p in paragraphs)

def multimodal_content(text, img_paths, media):
    blocks = [{'type': 'text', 'text': text}]
    for path in sorted(set(img_paths)):
        b = media.get(path, b'')
        extension = path.rsplit('.', 1)[-1].lower()
        mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                'webp': 'image/webp', 'gif': 'image/gif'}.get(extension)
        if mime and 0 < len(b) < 1_500_000:
            blocks.append({'type': 'image_url', 'image_url': {
                'url': 'data:' + mime + ';base64,' + base64.b64encode(b).decode('ascii')
            }})
    return blocks

def complete(key, user_content, max_tokens):
    payload = {
        'model': MODEL,
        'messages': [{'role': 'system', 'content': SYSTEM},
                     {'role': 'user', 'content': user_content}],
        'reasoning': {'effort': 'max', 'exclude': True},
        'max_completion_tokens': max_tokens,
        'stream': False,
    }
    request = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key,
                 'Content-Type': 'application/json',
                 'X-Title': 'Internal model experiment'},
        method='POST',
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=1200) as response:
                data = json.load(response)
            if data.get('error'):
                raise RuntimeError(str(data['error'])[:400])
            content = data['choices'][0]['message'].get('content') or ''
            if isinstance(content, list):
                answer = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict))
            else:
                answer = str(content)
            usage = data.get('usage') or {}
            in_tokens = int(usage.get('prompt_tokens') or 0)
            out_tokens = int(usage.get('completion_tokens') or 0)
            cost = usage.get('cost')
            if cost is None:
                cost = in_tokens * 8 / 1_000_000 + out_tokens * 40 / 1_000_000
            return answer.strip(), float(cost), {
                'input_tokens': in_tokens, 'output_tokens': out_tokens,
                'reasoning_tokens': (usage.get('completion_tokens_details') or {}).get('reasoning_tokens'),
                'cost_usd': float(cost),
            }
        except urllib.error.HTTPError as ex:
            err = ex.read().decode('utf-8', 'replace')[:500]
            if ex.code not in (429, 502, 503) or attempt == 2:
                raise RuntimeError('OpenRouter HTTP ' + str(ex.code) + ': ' + err) from ex
            time.sleep(5 * (attempt + 1))
    raise RuntimeError('No API response')

def write_usage(out, details):
    with (out / 'usage.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps(details) + '\n')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=Path, default=Path('input/manuscript.docx'))
    parser.add_argument('--out', type=Path, default=Path('results'))
    parser.add_argument('--budget', type=float, default=7.0)
    parser.add_argument('--words-per-batch', type=int, default=750)
    parser.add_argument('--start-batch', type=int, default=1)
    parser.add_argument('--max-output-tokens', type=int, default=11000)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.budget <= 0 or not 350 <= args.words_per_batch <= 2500 or args.start_batch < 1:
        parser.error('Invalid budget, words-per-batch or start-batch values')

    paragraphs, media = extract_docx(args.file)
    batches = group_paragraphs(paragraphs, args.words_per_batch)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'inventory.json').write_text(json.dumps({
        'paragraphs': len(paragraphs), 'batch_count': len(batches),
        'word_count': sum(len(p['text'].split()) for p in paragraphs),
        'image_count': len(media),
        'ranges': [{'batch': i, 'first': b[0]['id'], 'last': b[-1]['id'],
                    'paragraphs': len(b)}
                   for i, b in enumerate(batches, 1)]
    }, indent=2), encoding='utf-8')
    if args.dry_run:
        print('Checked', len(paragraphs), 'paragraphs,', len(batches), 'batches,',
              len(media), 'embedded images')
        return
    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key:
        raise RuntimeError('Missing OPENROUTER_API_KEY secret')
    spent = 0.0
    mapping_file = args.out / 'document_map.md'
    if args.start_batch == 1:
        if args.budget < 0.65:
            raise ValueError('Budget too small for initial mapping')
        prompt = ('Read the FULL manuscript to prepare a compact structural map of its thesis, '
                  'argument, comparisons, methods, central objections and JGLR priorities. '
                  'This is NOT the detailed review. Limit visible map to 1500 words.\n\n' +
                  lines(paragraphs))
        images = [img for p in paragraphs for img in p['images']]
        result, cost, usage = complete(api_key, multimodal_content(prompt, images, media), 6500)
        write_usage(args.out, {'stage': 'map', **usage})
        spent += cost
        if len(result) < 80:
            raise RuntimeError('Mapping returned too little visible content; no batch started')
        mapping_file.write_text(result + '\n', encoding='utf-8')
        print('Document map complete; billed $' + format(spent, '.4f'), flush=True)
    else:
        headings = [p['text'][:80] for p in paragraphs if re.search(r'\[Heading', p['text'])]
        mapping_file.write_text('Structural headings: ' + '; '.join(headings), encoding='utf-8')
    map_text = mapping_file.read_text(encoding='utf-8')[:10000]

    missing_coverage = []
    for i, batch in enumerate(batches, 1):
        if i < args.start_batch:
            continue
        # Reserve more than the maximum configured output cost plus an input allowance.
        if spent + (0.000040 * args.max_output_tokens) + 0.25 > args.budget:
            print('Stopping before batch', i, 'to stay within the spending cap.', flush=True)
            break
        ids = [p['id'] for p in batch]
        message = (
            'PART ' + str(i) + ' OF ' + str(len(batches)) + '. Review ONLY paragraphs ' +
            ids[0] + ' through ' + ids[-1] + ' exhaustively, in order. '
            'For EVERY paragraph ID, include an explicit record even if there is no issue. '
            'Examine each substantive sentence, consequential word and claim. '
            'For each weakness provide separate numbered findings with a quote, severity, '
            'strongest counterargument, detailed reasoning and recommended remedy. '
            'Review all relevant footnotes and any embedded image supplied. '
            'Do not rewrite. Mark legal authorities unverified absent independent access.\n\n'
            'GLOBAL ARGUMENT MAP:\n' + map_text + '\n\nREVIEW THESE PARAGRAPHS:\n' +
            lines(batch)
        )
        images = [img for p in batch for img in p['images']]
        text, cost, usage = complete(api_key, multimodal_content(message, images, media),
                                     args.max_output_tokens)
        write_usage(args.out, {'stage': 'batch', 'batch': i, **usage})
        spent += cost
        if len(text) < 80:
            (args.out / ('batch-' + str(i).zfill(2) + '-empty.txt')).write_text(
                text, encoding='utf-8')
            print('Batch', i, 'returned insufficient visible output; stopping.', flush=True)
            break
        filename = args.out / ('batch-' + str(i).zfill(2) + '.md')
        filename.write_text('# Batch ' + str(i) + '/' + str(len(batches)) +
                            ' (' + ids[0] + ' to ' + ids[-1] + ')\n\n' + text + '\n',
                            encoding='utf-8')
        cited_ids = set(re.findall(r'\bP\d{4}\b', text))
        missing = sorted(set(ids) - cited_ids)
        if missing:
            missing_coverage.append({'batch': i, 'missing_paragraph_ids': missing})
        print('Batch', i, '/', len(batches), 'saved; cumulative $' +
              format(spent, '.4f') + '; IDs explicitly mentioned ' +
              str(len(set(ids) & cited_ids)) + '/' + str(len(ids)), flush=True)

    done = []
    for file in args.out.glob('batch-*.md'):
        match = re.fullmatch(r'batch-(\d+)\.md', file.name)
        if match:
            done.append(int(match.group(1)))
    done.sort()
    next_batch = next((i for i in range(args.start_batch, len(batches) + 1)
                       if i not in done), None)
    status = {'completed_batches_this_run': done, 'total_batches': len(batches),
              'spent_usd_this_run': round(spent, 6),
              'missing_paragraph_coverage': missing_coverage,
              'next_batch': next_batch}
    (args.out / 'status.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
    full = ['# Diagnostic review', '',
            'Completed ' + str(len(done)) + ' of ' + str(len(batches)) +
            ' batches in this run. Billed $' + format(spent, '.4f') + '.']
    for i in done:
        full.append((args.out / ('batch-' + str(i).zfill(2) + '.md')).read_text(
            encoding='utf-8'))
    (args.out / 'diagnostic_report.md').write_text('\n\n'.join(full), encoding='utf-8')
    print('Status:', json.dumps(status), flush=True)

if __name__ == '__main__':
    main()
