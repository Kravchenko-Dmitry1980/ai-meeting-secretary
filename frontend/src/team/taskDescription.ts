/** Display-only projection of Vikunja HTML; never creates a DOM or changes its source. */
const BLOCKS = new Set(['p', 'div', 'li', 'pre']);
const NAMED_ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
// Match the backend's lowercase UUID grammar and Python visible-line whitespace.
// eslint-disable-next-line no-control-regex -- Python str.strip includes U+001C through U+001F.
const ORIGIN_LINE = /^[\t\v\f\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*secretary-origin:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}[\t\v\f\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/;
// eslint-disable-next-line no-control-regex -- Python str.splitlines includes U+001C through U+001E.
const LINE_BREAK = /(\r\n|[\n\v\f\r\u001c-\u001e\u0085\u2028\u2029])/;

function decodeText(value: string) {
  // Decode exactly once, after tokenizing the ORIGINAL markup. Secretary escapes
  // literal user entities as &amp;name;, so unknown named entities stay literal.
  return value.replace(/&(?:amp|lt|gt|quot|apos|nbsp|#[xX][0-9a-fA-F]+|#[0-9]+);/g, (entity) => {
    const name = entity.slice(1, -1);
    if (name[0] !== '#') return NAMED_ENTITIES[name];
    const hex = name[1]?.toLowerCase() === 'x';
    const code = Number.parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10);
    return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : '\ufffd';
  });
}

interface Part { text: string; structural: boolean }
interface Line { text: string; after: string; structural: boolean }

export function taskDescriptionText(description: string): string {
  const source = description.replace(/\r\n?/g, '\n');
  const parts: Part[] = [];
  const append = (text: string, structural = false) => {
    if (text && !(structural && parts.at(-1)?.structural)) parts.push({ text, structural });
  };
  let cursor = 0;
  let hidden: 'script' | 'style' | null = null;
  while (cursor < source.length) {
    if (hidden) {
      // HTML raw-text contents do not parse nested tags or their attributes.
      const closing = (hidden === 'script' ? /<\/script\s*>/i : /<\/style\s*>/i).exec(source.slice(cursor));
      if (!closing) break;
      cursor += closing.index + closing[0].length;
      hidden = null;
      continue;
    }
    const start = source.indexOf('<', cursor);
    if (start === -1) {
      if (!hidden) append(decodeText(source.slice(cursor)));
      break;
    }
    if (!hidden) append(decodeText(source.slice(cursor, start)));
    if (source.startsWith('<!--', start)) {
      const end = source.indexOf('-->', start + 4);
      if (end === -1) {
        if (!hidden) append(decodeText(source.slice(start)));
        break;
      }
      cursor = end + 3;
      continue;
    }
    const match = /^<(\/?)([a-z][a-z0-9:-]*)(?=[\s/>])/i.exec(source.slice(start));
    if (!match) {
      if (!hidden) append('<');
      cursor = start + 1;
      continue;
    }
    // Attribute values may contain '>'. No attribute is read or instantiated.
    let end = start + match[0].length;
    let quote = '';
    for (; end < source.length; end += 1) {
      const char = source[end];
      if (quote) { if (char === quote) quote = ''; }
      else if (char === '"' || char === "'") quote = char;
      else if (char === '>') break;
    }
    if (end === source.length) {
      // An incomplete tag remains literal; consume once rather than retrying it.
      if (!hidden) append(decodeText(source.slice(start)));
      break;
    }
    const name = match[2].toLowerCase();
    const closing = match[1] === '/';
    const selfClosing = /\/\s*$/.test(source.slice(start, end));
    if (name === 'script' || name === 'style') {
      if (!closing && !selfClosing) hidden = name;
    } else if (!hidden) {
      if (BLOCKS.has(name)) append('\n', true);
      else if (name === 'br' && !closing) append('\n');
    }
    cursor = end + 1;
  }
  // Remove only wrapper framing, never literal whitespace or <br> line breaks.
  if (parts[0]?.structural) parts.shift();
  if (parts.at(-1)?.structural) parts.pop();
  const lines: Line[] = [{ text: '', after: '', structural: false }];
  for (const part of parts) {
    const chunks = part.text.split(LINE_BREAK);
    for (let index = 0; index < chunks.length; index += 1) {
      const line = lines.at(-1)!;
      if (index % 2 === 0) line.text += chunks[index];
      else {
        line.after = chunks[index];
        line.structural = part.structural;
        lines.push({ text: '', after: '', structural: false });
      }
    }
  }
  const visible: Line[] = [];
  for (const line of lines) {
    if (!ORIGIN_LINE.test(line.text)) { visible.push(line); continue; }
    // A final service paragraph owns its separator; real trailing user breaks
    // stay intact. An interior marker leaves one separator between its neighbors.
    const previous = visible.at(-1);
    if (!line.after && previous?.structural) previous.after = '';
  }
  return visible.map((line) => line.text + line.after).join('');
}
