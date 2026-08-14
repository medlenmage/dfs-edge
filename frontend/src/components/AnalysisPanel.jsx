import { useState } from 'react'
import { api } from '../api'

/**
 * Renders Claude's written read on the slate.
 *
 * Deliberately a tiny markdown renderer rather than pulling in a library
 * -- the model only ever emits headings, paragraphs, lists and bold, and
 * one fewer dependency is one fewer thing to maintain.
 */
function renderMarkdown(text) {
  const blocks = []
  let list = null

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
  }

  for (const raw of (text || '').split('\n')) {
    const line = raw.trim()
    if (!line) {
      flush()
      continue
    }
    if (line.startsWith('###')) {
      flush()
      blocks.push(`<h2>${inline(line.replace(/^#+\s*/, ''))}</h2>`)
    } else if (line.startsWith('##')) {
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

export function AnalysisPanel({ date, enabled }) {
  const [state, setState] = useState({ status: 'idle' })
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState(null)
  const [asking, setAsking] = useState(false)

  async function run(refresh = false) {
    setState({ status: 'loading' })
    try {
      const data = await api.analysis(date, { refresh })
      if (!data.available) {
        setState({ status: 'error', message: data.reason })
      } else {
        setState({ status: 'ready', data })
      }
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  async function ask(e) {
    e.preventDefault()
    if (!question.trim()) return
    setAsking(true)
    setAnswer(null)
    try {
      const data = await api.ask(date, question)
      setAnswer(data.available ? data.text : data.reason)
    } catch (err) {
      setAnswer(`Failed: ${err.message}`)
    } finally {
      setAsking(false)
    }
  }

  if (!enabled) {
    return (
      <div className="notice">
        AI analysis is off. Add <code>ANTHROPIC_API_KEY</code> to your{' '}
        <code>.env</code> file and restart the backend to turn it on.
      </div>
    )
  }

  return (
    <div className="card">
      {state.status === 'idle' && (
        <>
          <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
            Claude reads the whole slate — every split, line, park factor and
            forecast on this page — and writes up where the edges are.
          </p>
          <button className="primary" onClick={() => run(false)}>
            Analyse this slate
          </button>
        </>
      )}

      {state.status === 'loading' && (
        <div>
          <div className="skeleton" style={{ width: '60%', marginBottom: 10 }} />
          <div className="skeleton" style={{ width: '92%', marginBottom: 10 }} />
          <div className="skeleton" style={{ width: '84%', marginBottom: 10 }} />
          <div className="skeleton" style={{ width: '70%' }} />
          <p className="dim" style={{ fontSize: 13, marginTop: 14 }}>
            Reading the slate… this takes 15–40 seconds.
          </p>
        </div>
      )}

      {state.status === 'error' && (
        <>
          <div className="notice error">{state.message}</div>
          <button style={{ marginTop: 12 }} onClick={() => run(false)}>
            Try again
          </button>
        </>
      )}

      {state.status === 'ready' && (
        <>
          <div
            className="analysis"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(state.data.text) }}
          />

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              marginTop: 18,
              paddingTop: 12,
              borderTop: '1px solid var(--gridline)',
            }}
          >
            <button onClick={() => run(true)}>Regenerate</button>
            <span className="dim" style={{ fontSize: 12 }}>
              {state.data.model} · {state.data.input_tokens?.toLocaleString()} in /{' '}
              {state.data.output_tokens?.toLocaleString()} out
              {state.data.estimated_cost_usd != null &&
                ` · ~$${state.data.estimated_cost_usd.toFixed(3)}`}
            </span>
          </div>

          <form className="ask-row" onSubmit={ask}>
            <input
              type="text"
              placeholder="Ask a follow-up — e.g. 'who's the best cheap bat in a good park?'"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <button className="primary" disabled={asking}>
              {asking ? 'Thinking…' : 'Ask'}
            </button>
          </form>

          {answer && (
            <div
              className="analysis"
              style={{ marginTop: 14 }}
              dangerouslySetInnerHTML={{ __html: renderMarkdown(answer) }}
            />
          )}
        </>
      )}
    </div>
  )
}
