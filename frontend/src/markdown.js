/**
 * Tiny markdown renderer shared by the AI-analysis, Briefs and Results
 * panels. Deliberately not a library -- the model and the audits only
 * ever emit headings, paragraphs, lists, bold/italic/code and (from the
 * audits) simple pipe tables, and one fewer dependency is one fewer
 * thing to maintain.
 */
export function renderMarkdown(text) {
  const blocks = []
  let list = null
  let table = null

  const inline = (s) =>
    s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/`(.+?)`/g, '<code>$1</code>')

  const flush = () => {
    if (list) {
      blocks.push(`<ul>${list.join('')}</ul>`)
      list = null
    }
    if (table) {
      const [head, ...body] = table
      const th = head.map((c) => `<th>${inline(c)}</th>`).join('')
      const rows = body
        .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`)
        .join('')
      blocks.push(`<table><thead><tr>${th}</tr></thead><tbody>${rows}</tbody></table>`)
      table = null
    }
  }

  for (const raw of (text || '').split('\n')) {
    const line = raw.trim()
    if (!line) {
      flush()
      continue
    }
    if (line.startsWith('|')) {
      const cells = line.replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue // separator row
      if (list) flush()
      table = table || []
      table.push(cells)
      continue
    }
    if (table) flush()
    if (line.startsWith('#')) {
      flush()
      blocks.push(`<h2>${inline(line.replace(/^#+\s*/, ''))}</h2>`)
    } else if (/^[-*]\s+/.test(line)) {
      list = list || []
      list.push(`<li>${inline(line.replace(/^[-*]\s+/, ''))}</li>`)
    } else if (/^\d+\.\s+/.test(line)) {
      list = list || []
      list.push(`<li>${inline(line.replace(/^\d+\.\s+/, ''))}</li>`)
    } else {
      flush()
      blocks.push(`<p>${inline(line)}</p>`)
    }
  }
  flush()
  return blocks.join('')
}
