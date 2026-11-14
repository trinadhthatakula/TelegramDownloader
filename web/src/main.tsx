import { StrictMode, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowDownToLine, ArrowRight, ArrowUpRight, Bell, Check, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, Download, File, Folder, FolderOpen, HardDrive, Image, LayoutGrid, LoaderCircle, LogOut, Moon, Music2, Pause, Play, Plus, RefreshCw, Search, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, Square, Sun, Trash2, Users, Video, X } from 'lucide-react';
import { api, categories, connectSession, normalizedScopes, size, type State, type Scope, type Preview, type Inventory, type Directory, type Operation } from './api';
import './style.css';

const icons: Record<string, typeof Image> = { images: Image, videos: Video, animations: Sparkles, audio: Music2, voice: Music2, video_messages: Video, stickers: Sparkles, documents: File };
function IconButton({ label, children, onClick, disabled }: { label: string; children: ReactNode; onClick: () => void; disabled?: boolean }) {
  return <button className="icon-button" aria-label={label} title={label} onClick={onClick} disabled={disabled}>{children}</button>;
}
function Modal({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} onCancel={close} onClick={e => { if (e.target === ref.current) close(); }} className="modal"><header><h2>{title}</h2><IconButton label="Close dialog" onClick={close}><X size={20}/></IconButton></header>{children}</dialog>;
}

function App() {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('tgdl_theme');
    if (saved === 'dark' || saved === 'light') return saved;
    return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });
  const [state, setState] = useState<State | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('tgdl_theme', theme);
  }, [theme]);

  const toggleTheme = () => setTheme(t => (t === 'dark' ? 'light' : 'dark'));
  const [page, setPage] = useState('library');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [ready, setReady] = useState(false);
  const [scopes, setScopes] = useState<Scope[]>([]);
  const [quantities, setQuantities] = useState<Record<string, number | null>>({ images: null, videos: null });
  const [query, setQuery] = useState('');
  const [archived, setArchived] = useState(true);
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [kind, setKind] = useState('all');
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [inventory, setInventory] = useState<Inventory>([]);
  const [order, setOrder] = useState('newest');
  const [extensions, setExtensions] = useState('');
  const [after, setAfter] = useState('');
  const [before, setBefore] = useState('');
  const [minMB, setMinMB] = useState('');
  const [maxMB, setMaxMB] = useState('');
  const [skipExisting, setSkipExisting] = useState(true);
  const [previewOp, setPreviewOp] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [issuesOpen, setIssuesOpen] = useState(false);
  const [queueChat, setQueueChat] = useState('');
  const [queueTopic, setQueueTopic] = useState('');
  const [queueStatus, setQueueStatus] = useState('all');
  const [offset, setOffset] = useState(0);
  const [chatOffset, setChatOffset] = useState(0);
  const [busy, setBusy] = useState('');
  const [confirm, setConfirm] = useState<{ title: string; text: string; run: () => Promise<unknown> } | null>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const shouldFocusSearch = useRef(false);
  const accountRef = useRef<string | null>(null);
  const stateRequest = useRef(0);

  const reload = useCallback(async () => {
    const request = ++stateRequest.current;
    try {
      const params = new URLSearchParams({ offset: String(offset), limit: '40' });
      if (queueChat) params.set('chat_id', queueChat);
      if (queueTopic) params.set('topic_id', queueTopic);
      const next = await api<State>('/state?' + params);
      if (request !== stateRequest.current) return;
      setState(next); setReady(true);
      if (next.account !== accountRef.current) {
        accountRef.current = next.account;
        setScopes(next.selection.scopes || []); setQuantities(next.selection.quantities || { images: null, videos: null });
        setQueueChat(''); setQueueTopic(''); setQueueStatus('all'); setOffset(0); setPreview(null); setPreviewOp(null); setInventory([]);
      }
    } catch (e) { setError(String(e instanceof Error ? e.message : e)); }
  }, [offset, queueChat, queueTopic]);

  useEffect(() => {
    if (page === 'library' && shouldFocusSearch.current) {
      shouldFocusSearch.current = false;
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    }
  }, [page]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const activeEl = document.activeElement;
      const isInput = activeEl instanceof HTMLInputElement || activeEl instanceof HTMLTextAreaElement || (activeEl as HTMLElement)?.isContentEditable;
      if ((e.key === '/' && !isInput) || ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K'))) {
        e.preventDefault();
        shouldFocusSearch.current = true;
        setPage('library');
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => { connectSession().then(reload).catch(e => setError(e.message)); }, []);
  useEffect(() => {
    if (!ready) return;
    void reload();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const events = new EventSource('/api/events');
    const changed = () => { if (!timer) timer = setTimeout(() => { timer = undefined; void reload(); }, 350); };
    events.addEventListener('change', changed);
    const poll = setInterval(reload, 2500);
    return () => { events.close(); clearInterval(poll); if (timer) clearTimeout(timer); };
  }, [ready, reload]);

  async function act<T>(label: string, action: () => Promise<T>) {
    setBusy(label); setError('');
    try { const result = await action(); await reload(); return result; }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); return undefined; }
    finally { setBusy(''); }
  }
  const run = (path: string, body: unknown = {}) => act(path, () => api<{ operation: string }>(path, body));
  const spec = { scopes: normalizedScopes(scopes), quantities, order, skip_existing: skipExisting, filters: {
    extensions: extensions.split(',').map(e => e.trim().toLowerCase()).filter(Boolean).map(e => e.startsWith('.') ? e : '.' + e),
    after: after ? after + 'T00:00:00+00:00' : undefined, before: before ? before + 'T23:59:59+00:00' : undefined,
    min_size: minMB ? Number(minMB) * 1024 ** 2 : undefined, max_size: maxMB ? Number(maxMB) * 1024 ** 2 : undefined,
  } };
  const scopeKey = JSON.stringify(spec);
  const inventoryRevision = state?.operations.map(o => o.status).join(',') + ':' + state?.chats.map(c => c.scan?.scanned).join(',');
  useEffect(() => {
    let alive = true;
    if (state?.authorized && scopes.length) {
      api<Inventory>('/inventory', spec).then(rows => { if (alive) setInventory(rows); }).catch(e => { if (alive) setError(e.message); });
    } else setInventory([]);
    return () => { alive = false; };
  }, [scopeKey, state?.account, inventoryRevision]);
  useEffect(() => {
    if (!state?.authorized) return;
    const timer = setTimeout(() => { void api('/selection', { scopes, quantities }).catch(e => setError(e.message)); }, 500);
    return () => clearTimeout(timer);
  }, [JSON.stringify(scopes), JSON.stringify(quantities), state?.account]);
  useEffect(() => {
    if (!previewOp) return;
    const op = state?.operations.find(o => o.id === previewOp);
    if (op?.status === 'complete') { setPreview(op.result as Preview); setPreviewOp(null); }
    else if (op?.status === 'failed' || op?.status === 'cancelled') { setError(op.message || 'Selection preparation cancelled'); setPreviewOp(null); }
  }, [state?.operations, previewOp]);

  const isFlooded = (state?.flood_seconds ?? 0) > 0;
  const selectedChats = state?.chats.filter(c => scopes.some(s => s.chat_id === c.id)) || [];
  const complete = selectedChats.length > 0 && selectedChats.every(c => c.scan?.status === 'complete');
  const scanning = state?.operations.some(o => o.label === 'Scan history' && o.status === 'running');
  const found = inventory.reduce((sum, row) => sum + row.found, 0);
  const bytes = inventory.reduce((sum, row) => sum + row.bytes, 0);
  const selected = (id: number, topic?: number) => scopes.some(s => s.chat_id === id && (s.topic_id == null || s.topic_id === topic));
  const toggle = (id: number, topic?: number) => setScopes(old => {
    const exists = old.some(s => s.chat_id === id && s.topic_id === topic);
    return normalizedScopes(exists ? old.filter(s => !(s.chat_id === id && s.topic_id === topic)) : [...old, { chat_id: id, topic_id: topic }]);
  });
  const visibleChats = (state?.chats || []).filter(c => c.name.toLowerCase().includes(query.toLowerCase()) && (archived || !c.archived) && (!selectedOnly || scopes.some(s => s.chat_id === c.id)) && (kind === 'all' || c.kind === kind));
  const activeOps = state?.operations.filter(o => o.status === 'running') || [];
  const title = { library: 'Media library', downloads: 'Downloads', accounts: 'Accounts', settings: 'Settings' }[page];
  const control = (action: string) => run('/control', { action, chat_id: queueChat ? Number(queueChat) : null, topic_id: queueTopic ? Number(queueTopic) : null });

  const filteredQueue = (state?.queue || []).filter(item => {
    if (queueStatus === 'all') return true;
    if (queueStatus === 'Active') return item.status === 'Active' || item.status === 'Downloading' || item.status === 'Queued';
    if (queueStatus === 'Completed') return item.status === 'Completed' || item.status.startsWith('Completed') || item.status.startsWith('Skipped');
    if (queueStatus === 'Failed') return item.status.startsWith('Failed');
    if (queueStatus === 'Paused') return item.status === 'Paused';
    return item.status === queueStatus;
  });

  return <div className="app-shell">
    <aside className="navigation">
      <a className="brand" href="#" onClick={e => { e.preventDefault(); setPage('library'); }}><span className="brand-icon"><ArrowDownToLine size={23}/></span><span>Telegram<span className="brand-sub">DOWNLOADER</span></span></a>
      <div className="nav-caption">WORKSPACE</div>
      <nav>{[['library', LayoutGrid, 'Media library'], ['downloads', Download, 'Downloads'], ['accounts', Users, 'Accounts'], ['settings', Settings2, 'Settings']].map(([key, Icon, label]) => {
        const Glyph = Icon as typeof LayoutGrid;
        return <button key={String(key)} className={'nav-link ' + (page === key ? 'active' : '')} onClick={() => setPage(String(key))}><Glyph size={19}/><span>{String(label)}</span>{key === 'downloads' && !!state?.stats.active && <b className="nav-badge">{state.stats.active}</b>}</button>;
      })}</nav>
      <div className="nav-bottom"><div className="local-note"><ShieldCheck size={19}/><div><strong>Stored on your device</strong><p>Your files stay in your folders.</p></div></div>
        <button className="account-chip" onClick={() => setPage('accounts')}><span className="avatar">{state?.profile?.photo ? <img src={state.profile.photo} alt=""/> : (state?.profile?.name?.[0] || 'T')}</span><span><strong>{state?.profile?.name || 'Your workspace'}</strong><small>{state?.authorized ? 'Telegram connected' : 'Connect an account'}</small></span><ChevronRight size={15}/></button></div>
    </aside>
    <main>
      <header className="topbar"><div className="breadcrumbs">Workspace <ChevronRight size={13}/><strong>{title}</strong></div><div className="top-actions"><IconButton label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'} onClick={toggleTheme}>{theme === 'dark' ? <Sun size={19}/> : <Moon size={19}/>}</IconButton><span className={'connection ' + (state?.authorized ? 'online' : '')}><i/>{state?.authorized ? 'Connected' : 'Not connected'}</span><IconButton label="View issues" onClick={() => setIssuesOpen(true)}><Bell size={19}/>{!!state?.issues.length && <span className="notification-dot"/>}</IconButton></div></header>
      {error && <div role="alert" className="alert error"><span>{error}</span><IconButton label="Dismiss error" onClick={() => setError('')}><X size={17}/></IconButton></div>}
      {notice && <div role="status" className="alert"><span>{notice}</span><IconButton label="Dismiss notice" onClick={() => setNotice('')}><X size={17}/></IconButton></div>}
      {!!state?.flood_seconds && <div className="alert">Telegram asked us to wait {state.flood_seconds}s. Downloads will resume automatically.</div>}
      {!ready ? <section className="empty-state"><LoaderCircle className="spin"/><h2>Opening your workspace</h2><p>Start the local service and open its launcher link.</p></section> : <>
        {page === 'library' && <div className="page-content">
          <div className="page-heading"><div><div className="eyebrow">YOUR COLLECTION, ORGANIZED</div><h1>Media library</h1><p>Choose your chats. Find your media. Download what matters.</p></div><button className="button secondary" onClick={() => run('/chats/refresh')} disabled={!state?.authorized || !!busy || isFlooded}><RefreshCw size={16}/>Refresh chats</button></div>
          {!state?.authorized && <div className="connect-banner"><div><strong>Bring your Telegram media together.</strong><p>Connect your account to browse chats, topics, and files.</p></div><button className="button" onClick={() => setPage('accounts')}>Connect Telegram <ArrowRight size={16}/></button></div>}
          <div className="summary-grid"><Summary label="Chats selected" value={String(selectedChats.length).padStart(2, '0')} caption={`${state?.chats.length || 0} chats in your account`} icon={<Users/>}/><Summary label="Media discovered" value={found.toLocaleString()} caption={complete ? 'Selected history scanned' : scanning ? 'Counting your media…' : 'Scan to discover all media'} icon={<LayoutGrid/>}/><Summary label="Known media size" value={size(bytes)} caption="Before existing-file verification" icon={<HardDrive/>}/></div>
          <div className="library-layout">
            <section className="panel explorer"><div className="panel-heading"><h2>Your chats</h2><span className="count-badge">{visibleChats.length}</span></div><label className="search-input"><Search size={16}/><input ref={searchInputRef} aria-label="Search chats" placeholder="Search chats…" value={query} onChange={e => { setQuery(e.target.value); setChatOffset(0); }}/><kbd>⌕</kbd></label>
              <div className="filter-row"><select aria-label="Chat type" value={kind} onChange={e => { setKind(e.target.value); setChatOffset(0); }}><option value="all">All chat types</option>{['Group','Channel','DM','Bot'].map(k => <option key={k}>{k}</option>)}</select><label><input type="checkbox" checked={archived} onChange={e => setArchived(e.target.checked)}/>Archived</label></div>
              <div className="selection-tools"><label><input type="checkbox" checked={selectedOnly} onChange={e => setSelectedOnly(e.target.checked)}/>Selected only</label><button className="text-button" onClick={() => setScopes(normalizedScopes([...scopes, ...visibleChats.map(c => ({ chat_id: c.id }))]))}>Select shown</button><button className="text-button" onClick={() => setScopes([])}>Clear</button></div>
              <div className="chat-list">{visibleChats.slice(chatOffset, chatOffset + 30).map(c => <div key={c.id}>
                <div className={'chat-row ' + (selected(c.id) ? 'selected' : '')}><input type="checkbox" aria-label={`Select ${c.name}`} checked={scopes.some(s => s.chat_id === c.id && s.topic_id == null)} onChange={() => toggle(c.id)}/><span className="chat-avatar"><Users size={19}/></span><button className="chat-title" onClick={() => setExpanded(old => { const next = new Set(old); next.has(c.id) ? next.delete(c.id) : next.add(c.id); return next; })}><strong>{c.name}</strong><small>{c.forum ? `${c.topics.length || 'Discover'} topics` : c.kind}{c.archived ? ' · Archived' : ''}</small></button>{c.scan?.status === 'scanning' ? <LoaderCircle size={15} className="spin"/> : c.scan?.status === 'complete' ? <Check size={15} className="green"/> : null}{c.forum && <ChevronDown size={14}/>}</div>
                {expanded.has(c.id) && <div className="topic-list">{c.topics.length ? c.topics.map(t => <label key={t.id}><input type="checkbox" checked={selected(c.id, t.id)} disabled={scopes.some(s => s.chat_id === c.id && s.topic_id == null)} onChange={() => toggle(c.id, t.id)}/><span>#</span>{t.title}</label>) : <small>Scan this chat to discover its topics.</small>}{c.scan && <small>{c.scan.scanned.toLocaleString()} messages checked · {c.scan.status}</small>}{c.scan?.error && <small className="red">{c.scan.error}</small>}</div>}
              </div>)}{!visibleChats.length && <div className="small-empty"><Users size={28}/><p>{state?.authorized ? 'No matching chats. Try refreshing or changing your filters.' : 'Your chats will appear here.'}</p></div>}</div>
              {visibleChats.length > 30 && <Pager offset={chatOffset} total={visibleChats.length} pageSize={30} setOffset={setChatOffset}/>}
              <div className="explorer-footer"><ShieldCheck size={14}/>All accessible history. No message limit.</div>
            </section>
            <section className="panel media-panel"><div className="panel-heading"><div><h2>Choose your media</h2><p>{selectedChats.length ? selectedChats.map(c => c.name).join(', ') : 'Select a chat or topic to begin'}</p></div><span className={'status-pill ' + (complete ? 'complete' : '')}>{complete ? <Check size={12}/> : <span className="mini-dot"/>}{complete ? 'Scan complete' : scanning ? 'Counting…' : 'Not scanned'}</span></div>
              <div className="scan-strip"><div><Search size={17}/><span><strong>Whole chat history</strong><small>Find every available file, including older messages.</small></span></div><button className="button secondary small" disabled={!scopes.length || !!busy || isFlooded} onClick={() => scanning ? run('/scans/cancel') : run('/scans', { chat_ids: selectedChats.map(c => c.id) })}>{scanning ? <Square size={14}/> : <RefreshCw size={14}/>} {scanning ? 'Pause scan' : complete ? 'Refresh inventory' : 'Scan history'}</button></div>
              <div className="media-table-wrap"><table className="media-table"><thead><tr><th>MEDIA TYPE</th><th>FOUND</th><th>VERIFIED</th><th>DOWNLOAD</th></tr></thead><tbody>{categories.map(([id, label]) => { const entry = inventory.find(r => r.category === id); const Icon = icons[id]; const enabled = id in quantities; return <tr key={id} className={enabled ? '' : 'muted-row'}><td><label><input type="checkbox" checked={enabled} onChange={e => setQuantities(old => { const next = { ...old }; e.target.checked ? next[id] = null : delete next[id]; return next; })}/><span className={'media-icon ' + id}><Icon size={18}/></span><strong>{label}</strong></label></td><td>{entry ? entry.found.toLocaleString() : '—'}</td><td>{entry ? entry.verified : '—'}</td><td><input className="quantity" type="number" aria-label={`${label} quantity`} min="0" disabled={!enabled} placeholder="All remaining" value={quantities[id] ?? ''} onChange={e => setQuantities(old => ({ ...old, [id]: e.target.value === '' ? null : Math.max(0, Number(e.target.value)) }))}/></td></tr>; })}</tbody></table></div>
              <div className="media-options"><label><input type="checkbox" checked={skipExisting} onChange={e => setSkipExisting(e.target.checked)}/>Skip verified downloads</label><label className="order-label">Order <select value={order} onChange={e => setOrder(e.target.value)}><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label></div>
              <details className="advanced"><summary><SlidersHorizontal size={15}/> Advanced filters <ChevronDown size={14}/></summary><div className="form-grid"><Field label="From date"><input type="date" value={after} onChange={e => setAfter(e.target.value)}/></Field><Field label="Through date"><input type="date" value={before} onChange={e => setBefore(e.target.value)}/></Field><Field label="Minimum MB"><input type="number" min="0" value={minMB} onChange={e => setMinMB(e.target.value)}/></Field><Field label="Maximum MB"><input type="number" min="0" value={maxMB} onChange={e => setMaxMB(e.target.value)}/></Field><Field label="Extensions"><input placeholder="jpg, png, mp4" value={extensions} onChange={e => setExtensions(e.target.value)}/></Field></div><button className="text-button" onClick={() => run('/scans', { chat_ids: selectedChats.map(c => c.id), full: true })} disabled={scanning || !scopes.length}>Rescan all history</button><button className="text-button" onClick={async () => { const s = scopes[0]; if (s) { const result = await act('Quick counts', () => api(`/counters?chat_id=${s.chat_id}${s.topic_id ? '&topic_id=' + s.topic_id : ''}`)); if (result) setNotice('Telegram counters requested. Results are listed in activity.'); } }} disabled={!scopes.length}>Quick Telegram counts</button></details>
              <footer className="selection-footer"><div><Folder size={17}/><span><small>DOWNLOAD TO</small><strong title={state?.config.output_dir}>{state?.config.output_dir}</strong><small>Topics get their own folders.</small></span></div><button className="button" disabled={!complete || !Object.keys(quantities).length || !!previewOp || !!busy || isFlooded} onClick={async () => { const result = await run('/jobs/preview', spec); if (result) setPreviewOp(result.operation); }}>{previewOp ? <LoaderCircle size={17} className="spin"/> : <ArrowDownToLine size={17}/>} {previewOp ? 'Checking files…' : 'Review download'} {!previewOp && <ArrowRight size={15}/>}</button></footer>
            </section>
          </div>
          <div className="page-footnote"><CircleHelp size={14}/> Counts cover media accessible to your account. Existing files are verified when you review a download.</div>
        </div>}
        {page === 'downloads' && <div className="page-content"><div className="page-heading"><div className="heading-copy"><div className="eyebrow">STEADY PROGRESS</div><h1>Downloads</h1><p>Everything in motion, and everything ready for later.</p></div><button className="button secondary" onClick={() => setPage('library')}><Plus size={17}/>Add downloads</button></div>
          <div className="summary-grid"><Summary label="Downloading" value={String(state?.stats.active || 0)} caption={`${size(state?.stats.speed || 0)}/s combined`} icon={<Download/>}/><Summary label="Completed" value={String(state?.stats.done || 0)} caption={`${state?.stats.queued || 0} waiting · ${state?.stats.failed || 0} failed`} icon={<Check/>}/><Summary label="Downloaded" value={size(state?.stats.downloaded || 0)} caption={`${size(state?.stats.total_bytes || 0)} known total`} icon={<HardDrive/>}/></div>
          <section className="panel"><div className="queue-toolbar"><div><select aria-label="Queue chat" value={queueChat} onChange={e => { setQueueChat(e.target.value); setQueueTopic(''); setOffset(0); }}><option value="">All downloads</option>{state?.chats.map(c => <option value={c.id} key={c.id}>{c.name}</option>)}</select>{queueChat && <select aria-label="Queue topic" value={queueTopic} onChange={e => { setQueueTopic(e.target.value); setOffset(0); }}><option value="">All topics</option>{state?.chats.find(c => c.id === Number(queueChat))?.topics.map(t => <option value={t.id} key={t.id}>{t.title}</option>)}</select>}<select aria-label="Queue status" value={queueStatus} onChange={e => { setQueueStatus(e.target.value); setOffset(0); }}><option value="all">All statuses</option><option value="Active">Active</option><option value="Completed">Completed</option><option value="Failed">Failed</option><option value="Paused">Paused</option></select></div><div className="button-group"><button onClick={() => control('pause')}><Pause size={14}/>Pause</button><button onClick={() => control('resume')}><Play size={14}/>Resume</button><button onClick={() => control('retry')}><RefreshCw size={14}/>Retry failed</button><button onClick={() => setConfirm({ title: 'Cancel selected downloads?', text: 'This stops work in the current queue scope. Recognized partial files are retained for resume.', run: () => control('cancel') })}><Square size={14}/>Cancel</button><button onClick={() => control('clear')}><Check size={14}/>Clear completed</button></div></div>
            {!filteredQueue.length ? <div className="empty-state"><span className="empty-icon"><Download size={30}/></span><h2>Your queue is clear</h2><p>Choose media from your library or resume a saved selection below.</p></div> : <div className="queue-list">{filteredQueue.map(item => <article className="download-row" key={item.id}><span className="file-icon"><File size={21}/></span><div className="download-main"><strong title={item.file_name}>{item.file_name}</strong><small>{item.chat_name}{item.topic_name ? ' / ' + item.topic_name : ''} · {size(item.file_size)}</small><div className="progress-track"><i style={{ width: `${Math.min(100, item.progress * 100)}%` }}/></div>{item.last_error && <small className="red">{item.last_error}</small>}<details><summary>File details</summary><small>{item.output_path || 'Destination reserved when the transfer starts'}</small></details></div><div className="download-status"><span className={'status-pill ' + (item.status === 'Completed' ? 'complete' : '')}>{item.status}</span><small>{item.speed > 0 ? `${size(item.speed)}/s · ${Math.ceil(((item.file_size || 0) - item.downloaded_bytes) / item.speed)}s left` : `${Math.round(item.progress * 100)}%`}</small></div>{['Queued','Downloading','Paused'].includes(item.status) && <IconButton label={`Cancel ${item.file_name}`} onClick={() => run(`/items/${item.id}/cancel`)}><X size={16}/></IconButton>}</article>)}</div>}
            <Pager offset={offset} total={state?.queue_total || 0} pageSize={40} setOffset={setOffset}/>
          </section><section className="panel saved-jobs"><div className="panel-heading"><div><h2>Saved selections</h2><p>Your choices survive closing the app. Reconnect the same account to resume.</p></div><ShieldCheck size={20}/></div>{state?.jobs.length ? state.jobs.map(job => <div className="saved-job" key={job.id}><span className="file-icon"><Folder size={19}/></span><div><strong>{new Date(job.created * 1000).toLocaleString()}</strong><small>{Object.entries(job.counts).map(([status, count]) => `${count} ${status.toLowerCase()}`).join(' · ') || 'Empty selection'}</small><small>{job.output_dir}</small></div><button className="button secondary small" onClick={() => run(`/jobs/${job.id}/resume`)}><Play size={14}/>Resume</button></div>) : <div className="small-empty">No saved selections yet.</div>}</section></div>}
        {page === 'accounts' && <Accounts state={state!} act={act} run={run} busy={busy || (activeOps.some(o => o.label === 'Account') ? 'Account' : '')}/>}
        {page === 'settings' && <Settings state={state!} act={act} run={run} setNotice={setNotice} setConfirm={setConfirm} theme={theme} setTheme={setTheme}/>}
      </>}
      {!!activeOps.length && <div className="activity-bar" role="status"><LoaderCircle size={15} className="spin"/>{activeOps.map(o => o.label).join(' · ')}<span>Working in the background</span></div>}
    </main>
    {preview && (() => {
      const lowSpace = preview.free_bytes !== undefined && preview.bytes > preview.free_bytes;
      return <Modal title="Ready to download" close={() => setPreview(null)}>
        {lowSpace && <div className="alert error" role="alert" style={{ marginBottom: '14px' }}>⚠️ Low disk space: {size(preview.bytes)} required, but only {size(preview.free_bytes)} free in destination folder.</div>}
        <p className="modal-intro">{preview.total} files selected · {size(preview.bytes)} known size{preview.unknown_sizes ? ` · ${preview.unknown_sizes} unknown sizes` : ''}</p>
        <div className="preview-counts">{Object.entries(preview.counts).map(([cat, n]) => <span key={cat}>{n} {categories.find(c => c[0] === cat)?.[1].toLowerCase()}</span>)}</div>
        <div className="destination"><FolderOpen size={20}/><span>{preview.output_dir}</span></div>
        <div className="preview-files">{preview.sample.map((f, i) => <div key={i}><File size={15}/><span>{f.chat}{f.topic ? '/' + f.topic : ''}/{f.file}</span></div>)}</div>
        <p className="muted">Your selection is saved. Existing files and recognized fragments retain their locations.</p>
        <div className="modal-actions"><button className="button secondary" onClick={() => setPreview(null)}>Save for later</button><button className="button" disabled={!preview.total || lowSpace} title={lowSpace ? 'Insufficient disk space' : undefined} onClick={async () => { const result = await run(`/jobs/${preview.id}/resume`); if (result) { setPreview(null); setPage('downloads'); } }}><Download size={16}/>Start downloads</button></div>
      </Modal>;
    })()}
    {issuesOpen && <Modal title="Issues & activity" close={() => setIssuesOpen(false)}>{state?.issues.length ? state.issues.map(i => <div className="issue" key={i.id}><small>{new Date(i.time * 1000).toLocaleTimeString()}</small><p>{i.message}</p></div>) : <p className="modal-intro">No issues to report.</p>}{state?.operations.filter(o => o.label === 'Count media' && o.status === 'complete').slice(-3).map(o => <pre key={o.id}>{JSON.stringify(o.result, null, 2)}</pre>)}<div className="modal-actions"><button className="button secondary" onClick={() => run('/issues/clear')}>Clear issues</button></div></Modal>}
    {confirm && <Modal title={confirm.title} close={() => setConfirm(null)}><p className="modal-intro">{confirm.text}</p><div className="modal-actions"><button className="button secondary" onClick={() => setConfirm(null)}>Keep working</button><button className="button danger" onClick={async () => { await confirm.run(); setConfirm(null); }}>Confirm</button></div></Modal>}
  </div>;
}
function Summary({ label, value, caption, icon }: { label: string; value: string; caption: string; icon: ReactNode }) { return <div className="summary-card"><div><span>{label}</span><strong>{value}</strong><small>{caption}</small></div><span className="summary-icon">{icon}</span></div>; }
function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}</label>; }
function Pager({ offset, total, pageSize, setOffset }: { offset: number; total: number; pageSize: number; setOffset: (n: number) => void }) { return <div className="pager"><small>{total ? `${offset + 1}–${Math.min(total, offset + pageSize)} of ${total}` : '0 items'}</small><div><IconButton label="Previous page" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}><ChevronLeft size={16}/></IconButton><IconButton label="Next page" disabled={offset + pageSize >= total} onClick={() => setOffset(offset + pageSize)}><ChevronRight size={16}/></IconButton></div></div>; }
type Act = <T>(label: string, action: () => Promise<T>) => Promise<T | undefined>;
type Run = (path: string, body?: unknown) => Promise<{ operation: string } | undefined>;
function Accounts({ state, run, busy }: { state: State; act: Act; run: Run; busy: string }) {
  const [profile, setProfile] = useState(state.profile_id || '');
  const [setup, setSetup] = useState(!state.profiles.length);
  const [apiId, setApiId] = useState(''); const [hash, setHash] = useState(''); const [label, setLabel] = useState('');
  const [country, setCountry] = useState('+91'); const [phone, setPhone] = useState(''); const [code, setCode] = useState(''); const [password, setPassword] = useState(''); const [showPassword, setShowPassword] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  useEffect(() => { if (state.login.retry_after) setCooldown(state.login.retry_after); }, [state.login.message, state.login.retry_after]);
  useEffect(() => { if (cooldown <= 0) return; const timer = setTimeout(() => setCooldown(n => n - 1), 1000); return () => clearTimeout(timer); }, [cooldown]);
  const auth = (action: string, values: Record<string, unknown> = {}) => run('/auth', { action, ...values });
  return <div className="page-content narrow"><div className="page-heading"><div><div className="eyebrow">YOUR TELEGRAM CONNECTION</div><h1>Accounts</h1><p>Sign in once. Keep your media organized across accounts.</p></div></div><section className="panel account-panel">
    {state.authorized && <div className="profile-hero"><span className="avatar large">{state.profile?.photo ? <img src={state.profile.photo} alt="Profile"/> : state.profile?.name[0]}</span><div><h2>{state.profile?.name}</h2><p>{state.profile?.detail}</p><span className="status-pill complete"><Check size={12}/>Signed in</span></div><button className="button secondary" onClick={() => auth('logout')} disabled={!!busy}><LogOut size={15}/>Log out</button></div>}
    <div className="form-grid"><Field label="Saved account"><select value={profile} onChange={e => setProfile(e.target.value)}><option value="">Choose an account</option>{state.profiles.map(p => <option value={p.id} key={p.id}>{p.label}</option>)}</select></Field><div className="field-actions"><button className="button secondary" disabled={!profile || !!busy} onClick={() => auth('switch', { key: profile })}>Connect saved account</button><button className="button secondary" disabled={!profile || !!busy} onClick={() => { setSetup(false); void auth('add_account', { base_profile: profile }); }}>Add account</button></div></div>
    <div className="account-divider"/><button className="text-button" onClick={() => setSetup(!setup)}><Settings2 size={15}/> {setup ? 'Hide API setup' : 'API setup'}</button>
    {setup && <form onSubmit={e => { e.preventDefault(); void auth('save', { key: profile || null, api_id: apiId || state.profiles.find(p => p.id === profile)?.api_id, api_hash: hash, label }).then(result => { if (result) { setHash(''); setSetup(false); } }); }}><h2>Connect your Telegram application</h2><p>Create an API application on Telegram, then enter its details here.</p><a className="button secondary" href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">Open Telegram API setup <ArrowUpRight size={15}/></a><div className="form-grid"><Field label="API ID"><input inputMode="numeric" value={apiId} placeholder={String(state.profiles.find(p => p.id === profile)?.api_id || '')} onChange={e => setApiId(e.target.value)}/></Field><Field label="API hash"><input type="password" value={hash} placeholder={profile ? 'Leave blank to keep saved hash' : '32-character API hash'} onChange={e => setHash(e.target.value)} autoComplete="off"/></Field><Field label="Account label"><input value={label} onChange={e => setLabel(e.target.value)} placeholder="Personal"/></Field></div><button className="button" disabled={!!busy}>Save API details & continue</button></form>}
    <div className="login-message" role="status">{busy ? <LoaderCircle className="spin" size={17}/> : <ShieldCheck size={17}/>} {state.login.message}</div>
    {!setup && !state.authorized && state.login.stage !== 'credentials' && <>
      <form onSubmit={e => { e.preventDefault(); void auth('send_code', { phone: country + phone.replace(/[\s()-]/g, '') }); }}><h2>Phone number</h2><div className="phone-fields"><Field label="Country code"><input value={country} onChange={e => setCountry(e.target.value)} placeholder="+91" autoComplete="tel-country-code"/></Field><Field label="Phone number"><input value={phone} onChange={e => setPhone(e.target.value)} autoComplete="tel-national"/></Field><button className="button secondary" disabled={!!busy || cooldown > 0}>{cooldown > 0 ? `Resend in ${cooldown}s` : state.login.stage === 'phone' ? 'Send login code' : 'Resend / change number'}</button></div></form>
      {state.login.stage === 'code' && <form onSubmit={e => { e.preventDefault(); void auth('code', { code }).then(() => setCode('')); }}><Field label="Login code"><input autoFocus value={code} onChange={e => setCode(e.target.value)} autoComplete="one-time-code" inputMode="numeric"/></Field><button className="button" disabled={!!busy}>Continue <ArrowRight size={15}/></button></form>}
      {state.login.stage === 'password' && <form onSubmit={e => { e.preventDefault(); void auth('password', { password }).then(() => setPassword('')); }}><Field label="Two-step verification password"><input autoFocus value={password} onChange={e => setPassword(e.target.value)} type={showPassword ? 'text' : 'password'} autoComplete="current-password"/></Field><label className="check-label"><input type="checkbox" checked={showPassword} onChange={e => setShowPassword(e.target.checked)}/>Show password</label><button className="button" disabled={!!busy}>Log in</button></form>}
    </>}
  </section><p className="page-footnote"><ShieldCheck size={15}/>Login codes and passwords are never saved. Switching accounts stops current work and preserves saved selections.</p></div>;
}
function Settings({ state, act, run, setNotice, setConfirm, theme, setTheme }: { state: State; act: Act; run: Run; setNotice: (s: string) => void; setConfirm: (v: { title: string; text: string; run: () => Promise<unknown> }) => void; theme: 'light' | 'dark'; setTheme: (t: 'light' | 'dark') => void }) {
  const [root, setRoot] = useState(state.config.output_dir); const [parallel, setParallel] = useState(state.config.max_parallel_files);
  const [directory, setDirectory] = useState<Directory | null>(null); const [info, setInfo] = useState<Directory | null>(null);
  const [parts, setParts] = useState<{ count: number; bytes: number } | null>(null);
  const browse = (path: string) => act('Browse folders', async () => { const value = await api<Directory>('/directories?path=' + encodeURIComponent(path)); setDirectory(value); return value; });
  return <div className="page-content narrow"><div className="page-heading"><div><div className="eyebrow">MAKE IT YOURS</div><h1>Settings</h1><p>Choose where your files live and how they get there.</p></div></div>
    <section className="panel settings-panel"><div className="setting-heading">{theme === 'dark' ? <Moon size={22}/> : <Sun size={22}/>}<div><h2>Appearance</h2><p>Choose your workspace theme.</p></div></div><div className="button-group"><button className={theme === 'light' ? 'button' : 'button secondary'} onClick={() => setTheme('light')}><Sun size={15}/>Light theme</button><button className={theme === 'dark' ? 'button' : 'button secondary'} onClick={() => setTheme('dark')}><Moon size={15}/>Dark theme</button></div></section>
    <section className="panel settings-panel"><div className="setting-heading"><FolderOpen/><div><h2>Download location</h2><p>New jobs use this folder. Existing jobs keep their original destination.</p></div></div><Field label="Download directory"><div className="input-group"><input value={root} onChange={e => setRoot(e.target.value)}/><button className="button secondary" onClick={() => browse(root)}>Browse</button></div></Field><div className="button-group"><button onClick={() => act('Check folder', async () => { const value = await api<Directory>('/directories/check', { path: root }); setInfo(value); return value; })}><ShieldCheck size={15}/>Check folder</button><button onClick={() => run('/directories/open')}><ArrowUpRight size={15}/>Open saved folder</button></div>{info && <div className="alert"><Check size={15}/>{info.writable ? 'Writable' : 'Read only'} · {size(info.free_bytes)} free</div>}
    <div className="account-divider"/><div className="setting-heading"><SlidersHorizontal/><div><h2>Parallel downloads</h2><p>Share transfer capacity across your chats.</p></div><strong className="large-number">{parallel}</strong></div><input aria-label="Parallel downloads" type="range" min="1" max="10" value={parallel} onChange={e => setParallel(Number(e.target.value))}/><div className="range-labels"><span>1 · Gentle</span><span>10 · More concurrent files</span></div><p className="muted">Telegram rate limits pause dispatch automatically. More connections do not always mean faster downloads.</p><button className="button" onClick={() => act('Save settings', async () => { const value = await api('/settings', { output_dir: root, max_parallel_files: parallel, scan_depth: 0 }); setNotice('Settings saved.'); return value; })}><Check size={16}/>Save settings</button></section>
    <section className="panel settings-panel"><div className="setting-heading"><HardDrive/><div><h2>Partial files</h2><p>Recognized fragments are preserved for resume. Clean only unrecognized leftovers.</p></div></div><button className="button secondary" onClick={() => act('Inspect partial files', async () => { const value = await api<{ count: number; bytes: number }>('/partials'); setParts(value); return value; })}>Inspect folder</button>{parts && <div className="cleanup-row"><span>{parts.count} unrecognized files · {size(parts.bytes)}</span><button className="button secondary" disabled={!parts.count} onClick={() => setConfirm({ title: 'Remove unrecognized partial files?', text: `${parts.count} unrecognized partial files are eligible for cleanup. Active and resumable files are protected.`, run: async () => { const result = await api<{ count: number; bytes: number }>('/partials/clean', { confirm: true }); setNotice(`Removed ${result.count} files.`); setParts(null); return result; } })}><Trash2 size={15}/>Clean up</button></div>}</section>
    <section className="panel settings-panel"><h2>Local service</h2><p>Closing a browser tab leaves downloads running. Quit to stop the local service safely.</p><button className="button secondary" onClick={() => setConfirm({ title: 'Quit the downloader?', text: 'Downloads will stop and recognized fragments will be retained. Saved selections can be resumed next time.', run: async () => { const result = await api('/quit', {}); setNotice('The service has stopped. You can close this tab.'); return result; } })}><LogOut size={15}/>Quit service</button></section>
    {directory && <Modal title="Choose a download folder" close={() => setDirectory(null)}><div className="directory-path"><button className="icon-button" aria-label="Parent directory" onClick={() => browse(directory.parent)}><ChevronLeft size={18}/></button><span>{directory.path}</span></div><div className="directory-list">{directory.directories.map(d => <button key={d.path} onClick={() => browse(d.path)}><Folder size={18}/>{d.name}<ChevronRight size={15}/></button>)}{!directory.directories.length && <p>No subfolders here.</p>}</div><div className="modal-actions"><small>{size(directory.free_bytes)} free</small><button className="button" onClick={() => { setRoot(directory.path); setInfo(directory); setDirectory(null); }}>Use this folder <Check size={16}/></button></div></Modal>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
