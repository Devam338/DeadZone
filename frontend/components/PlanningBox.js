import { useState, useRef, useEffect } from 'react';

const SUGGESTED = [
  'What are the worst dead zones right now?',
  'Where should we add late-night TTC service?',
  'Which areas need more healthcare coverage after 10pm?',
  'How does Rexdale compare to downtown after midnight?',
  'Where are overdose hotspots with no nearby services?',
];

const SOURCE_LABELS = {
  'nemotron-local-dgx':  { label: '⚡ Nemotron on DGX Spark', color: '#76b900', bg: '#76b90018' },
  'nemotron-openrouter': { label: 'Nemotron via OpenRouter',   color: '#76b900', bg: '#76b90018' },
  'nemotron-nim':        { label: 'Nemotron via NIM',          color: '#76b900', bg: '#76b90018' },
  nemotron:              { label: 'Nemotron',                  color: '#76b900', bg: '#76b90018' },
  openai:                { label: 'GPT-4o',                    color: '#3fb950', bg: '#3fb95018' },
  mock:                  { label: 'Mock',                      color: '#8b949e', bg: '#8b949e18' },
};

function TypingDots() {
  return (
    <div style={{ display: 'flex', gap: 4, padding: '8px 4px', alignItems: 'center' }}>
      {[0, 1, 2].map(i => (
        <div key={i} style={{
          width: 6, height: 6, borderRadius: '50%',
          background: '#76b900',
          animation: `bounce 1s ease-in-out ${i * 0.15}s infinite`,
        }} />
      ))}
      <style>{`
        @keyframes bounce {
          0%,80%,100% { transform: translateY(0); opacity:.4 }
          40% { transform: translateY(-6px); opacity:1 }
        }
      `}</style>
    </div>
  );
}

function Message({ msg }) {
  const isUser = msg.role === 'user';
  const src    = SOURCE_LABELS[msg.source] || SOURCE_LABELS.mock;

  // Convert **bold** markdown to <b> tags
  const formatted = msg.content
    .split(/\*\*(.*?)\*\*/g)
    .map((part, i) => i % 2 === 1 ? <b key={i}>{part}</b> : part);

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: isUser ? 'flex-end' : 'flex-start',
      marginBottom: 10,
    }}>
      <div style={{
        maxWidth: '88%',
        padding: '9px 13px',
        borderRadius: isUser ? '14px 14px 4px 14px' : '14px 14px 14px 4px',
        background: isUser ? '#1f6feb33' : 'var(--surface2)',
        border: `1px solid ${isUser ? '#1f6feb55' : 'var(--border)'}`,
        fontSize: 13,
        lineHeight: 1.65,
        color: 'var(--text)',
        whiteSpace: 'pre-wrap',
      }}>
        {formatted}
      </div>
      {!isUser && msg.source && (
        <div style={{ fontSize: 10, color: src.color, marginTop: 3, paddingLeft: 4 }}>
          ⬡ {src.label}
          {msg.latency_ms && ` · ${msg.latency_ms.toFixed(0)}ms`}
          {msg.turn && ` · turn ${msg.turn}`}
        </div>
      )}
    </div>
  );
}

export default function PlanningBox({ topCells, selectedHour, serviceType = 'all' }) {
  const [messages, setMessages]     = useState([]);   // {role, content, source?, latency_ms?, turn?}
  const [input, setInput]           = useState('');
  const [loading, setLoading]       = useState(false);
  const [sessionId, setSessionId]   = useState(null);
  const [error, setError]           = useState(null);
  const bottomRef                   = useRef(null);

  // Auto-scroll to latest message
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const send = async (text) => {
    const msg = (text || input).trim();
    if (!msg || loading) return;
    setInput('');
    setError(null);

    // Optimistically show user message
    setMessages(prev => [...prev, { role: 'user', content: msg }]);
    setLoading(true);

    try {
      const res = await fetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: msg,
          context_cells: topCells?.slice(0, 8),
          hour: selectedHour,
          service_type: serviceType,
          // Pass session_id via query param since legacy endpoint doesn't have it
          // — the /chat endpoint does support it
        }),
      });
      if (!res.ok) throw new Error(`API ${res.status}`);
      const data = await res.json();

      // Update session ID for continuity
      if (data.session_id) setSessionId(data.session_id);

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: data.answer,
        source: data.source,
        latency_ms: data.latency_ms,
        turn: prev.filter(m => m.role === 'user').length + 1,
      }]);
    } catch (err) {
      setError(`Request failed: ${err.message}`);
      setMessages(prev => prev.slice(0, -1)); // remove optimistic user msg on error
    } finally {
      setLoading(false);
    }
  };

  const newChat = async () => {
    if (sessionId) {
      try { await fetch(`/api/query`, { method: 'DELETE' }); } catch {}
    }
    setMessages([]);
    setSessionId(null);
    setError(null);
  };

  const exportBrief = () => {
    if (messages.length === 0) return;
    const lines = [
      `DeadZone Planning Brief`,
      `${'='.repeat(40)}`,
      `Generated: ${new Date().toISOString()}`,
      `Hour: ${selectedHour != null ? `${String(selectedHour).padStart(2,'0')}:00` : 'all hours'}`,
      `Service filter: ${serviceType}`,
      ``,
    ];
    messages.forEach(m => {
      lines.push(`[${m.role.toUpperCase()}${m.source ? ` via ${m.source}` : ''}]`);
      lines.push(m.content);
      lines.push('');
    });
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `deadzone-brief-${Date.now()}.txt`;
    a.click();
  };

  return (
    <div className="sidebar-section" style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <div className="panel-title" style={{ margin: 0 }}>⬡ Nemotron Planning AI</div>
        <div style={{ display: 'flex', gap: 6 }}>
          {messages.length > 0 && (
            <>
              <button onClick={exportBrief} title="Export brief" style={btnStyle}>↓ Brief</button>
              <button onClick={newChat}    title="New conversation" style={btnStyle}>+ New</button>
            </>
          )}
        </div>
      </div>

      {/* Suggested prompts (shown when no messages) */}
      {messages.length === 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 6 }}>Try asking:</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {SUGGESTED.map((s, i) => (
              <button key={i} onClick={() => send(s)} style={{
                background: 'var(--surface2)', border: '1px solid var(--border)',
                borderRadius: 8, padding: '6px 10px', fontSize: 11, color: 'var(--text-muted)',
                cursor: 'pointer', textAlign: 'left', transition: 'border-color 0.15s',
              }}
              onMouseOver={e => e.currentTarget.style.borderColor = '#76b900'}
              onMouseOut={e => e.currentTarget.style.borderColor = 'var(--border)'}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Message thread */}
      {messages.length > 0 && (
        <div style={{
          flex: 1, overflowY: 'auto', marginBottom: 10,
          minHeight: 0, paddingRight: 2,
        }}>
          {messages.map((m, i) => <Message key={i} msg={m} />)}
          {loading && (
            <div style={{ display: 'flex', alignItems: 'flex-start' }}>
              <div style={{
                background: 'var(--surface2)', border: '1px solid var(--border)',
                borderRadius: '14px 14px 14px 4px', padding: '4px 12px',
              }}>
                <TypingDots />
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      )}

      {/* Error */}
      {error && (
        <div style={{ fontSize: 11, color: '#f85149', background: '#f8514918', border: '1px solid #f8514944',
          borderRadius: 6, padding: '6px 10px', marginBottom: 8 }}>
          {error}
        </div>
      )}

      {/* Input */}
      <div style={{ display: 'flex', gap: 6 }}>
        <textarea
          className="planning-textarea"
          placeholder={messages.length > 0 ? 'Ask a follow-up…' : 'Ask about dead zones, transit, services…'}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
          style={{ flex: 1, minHeight: 52, resize: 'none' }}
        />
        <button
          onClick={() => send()}
          disabled={loading || !input.trim()}
          style={{
            background: loading ? 'var(--surface2)' : '#76b900',
            border: 'none', borderRadius: 8, color: '#fff',
            cursor: loading || !input.trim() ? 'not-allowed' : 'pointer',
            fontSize: 18, width: 44, flexShrink: 0,
            opacity: loading || !input.trim() ? 0.4 : 1,
            transition: 'all 0.15s',
          }}
          title="Send (Enter)"
        >
          {loading ? '…' : '↑'}
        </button>
      </div>
      <div style={{ fontSize: 9, color: 'var(--text-muted)', marginTop: 4, textAlign: 'right' }}>
        Enter to send · Shift+Enter for new line
      </div>
    </div>
  );
}

const btnStyle = {
  background: 'var(--surface2)', border: '1px solid var(--border)',
  borderRadius: 6, color: 'var(--text-muted)', cursor: 'pointer',
  fontSize: 10, padding: '3px 8px',
};
