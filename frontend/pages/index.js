import { useState, useEffect, useCallback } from 'react';
import dynamic from 'next/dynamic';
import DrillDown from '../components/DrillDown';
import PlanningBox from '../components/PlanningBox';
import TimeSlider from '../components/TimeSlider';

const MapView = dynamic(() => import('../components/MapView'), {
  ssr: false,
  loading: () => null,
});

const LEGEND_ITEMS = [
  { color: '#f85149', label: '80–100  Critical' },
  { color: '#f0883e', label: '65–80   High' },
  { color: '#e3b341', label: '50–65   Moderate' },
  { color: '#d29922', label: '30–50   Low' },
  { color: '#3fb950', label: '15–30   Minimal' },
  { color: '#388bfd', label: '0–15    Good access' },
];

const SERVICE_TYPES = [
  { key: 'all',        label: 'All Services',   icon: '⬡', color: '#e6edf3' },
  { key: 'healthcare', label: 'Healthcare',      icon: '🏥', color: '#388bfd' },
  { key: 'shelter',    label: 'Shelter',         icon: '🏠', color: '#f0883e' },
  { key: 'food',       label: 'Food Banks',      icon: '🍎', color: '#3fb950' },
  { key: 'community',  label: 'Community',       icon: '🤝', color: '#e3b341' },
];

export default function Home() {
  const [gridGeojson, setGridGeojson]     = useState(null);
  const [topDeadZones, setTopDeadZones]   = useState([]);
  const [services, setServices]           = useState([]);
  const [loading, setLoading]             = useState(true);
  const [loadError, setLoadError]         = useState(null);
  const [hour, setHour]                   = useState(22);
  const [playing, setPlaying]             = useState(false);
  const [selectedCell, setSelectedCell]   = useState(null);
  const [graphStats, setGraphStats]       = useState(null);
  const [serviceType, setServiceType]     = useState('all');

  useEffect(() => {
    const loadAll = async () => {
      try {
        const [gjRes, dzRes, svcRes] = await Promise.all([
          fetch('/data/grid.geojson'),
          fetch('/data/top_dead_zones.json'),
          fetch('/data/service_locations.json'),
        ]);
        if (!gjRes.ok) throw new Error(`grid.geojson: ${gjRes.status}`);
        if (!dzRes.ok) throw new Error(`top_dead_zones.json: ${dzRes.status}`);
        const [gj, dz, svc] = await Promise.all([gjRes.json(), dzRes.json(), svcRes.ok ? svcRes.json() : []]);
        setGridGeojson(gj);
        setTopDeadZones(dz.top_dead_zones || dz);
        setServices(Array.isArray(svc) ? svc : []);
        try {
          const gsRes = await fetch('/data/graph_stats.json');
          if (gsRes.ok) setGraphStats(await gsRes.json());
        } catch {}
      } catch (err) {
        setLoadError(`Failed to load data: ${err.message}. Run: python scripts/generate_seed.py`);
      } finally {
        setLoading(false);
      }
    };
    loadAll();
  }, []);

  const handleCellClick = useCallback((props) => setSelectedCell(props), []);
  const handleHourChange = useCallback((val) => setHour(typeof val === 'function' ? val : Number(val)), []);
  const togglePlay = useCallback(() => setPlaying(p => !p), []);

  const handleDzItemClick = (dz) => {
    if (!gridGeojson) return;
    const feature = gridGeojson.features.find(
      f => (f.properties?.cell_id === dz.cell_id || f.properties?.hex_id === dz.cell_id)
    );
    if (feature) { setSelectedCell({ ...feature.properties, ...dz }); setHour(dz.worst_hour ?? hour); }
  };

  const dzColor = (s) => s >= 80 ? '#f85149' : s >= 65 ? '#f0883e' : s >= 50 ? '#e3b341' : '#3fb950';
  const activeType = SERVICE_TYPES.find(t => t.key === serviceType);

  if (loadError) {
    return (
      <div style={{ display:'flex', alignItems:'center', justifyContent:'center', height:'100vh', flexDirection:'column', gap:16, padding:32 }}>
        <div style={{ fontSize:32 }}>⚠️</div>
        <div style={{ color:'var(--accent)', fontWeight:700 }}>Data not found</div>
        <div style={{ color:'var(--text-muted)', textAlign:'center', maxWidth:400 }}>{loadError}</div>
      </div>
    );
  }

  const cellCount = gridGeojson?.features?.length ?? 0;

  return (
    <div className="app-layout">
      {/* ── Header ── */}
      <header className="app-header">
        <h1>⬡ DEADZONE</h1>
        <span className="subtitle">Toronto · After Dark · {cellCount.toLocaleString()} cells</span>

        {/* Service type selector */}
        <div className="type-selector">
          {SERVICE_TYPES.map(t => (
            <button
              key={t.key}
              className={`type-btn ${serviceType === t.key ? 'active' : ''}`}
              style={{ '--type-color': t.color }}
              onClick={() => setServiceType(t.key)}
            >
              <span>{t.icon}</span> {t.label}
            </button>
          ))}
        </div>

        <div className="header-badge" style={{ marginLeft: 'auto' }}>
          {graphStats && (
            <span className="badge badge-gpu">
              {graphStats.backend === 'cugraph' ? '⚡ GPU cuGraph' : '🔷 NetworkX'}
              · {graphStats.node_count?.toLocaleString()} nodes
            </span>
          )}
          <span className="badge badge-live">311 demand</span>
        </div>
      </header>

      {/* ── Map ── */}
      <main className="map-area">
        {loading && (
          <div className="loading-overlay">
            <div className="loading-spinner" />
            <div className="loading-text">Loading DeadZone data…</div>
          </div>
        )}
        {!loading && gridGeojson && (
          <MapView
            gridGeojson={gridGeojson}
            hour={hour}
            serviceType={serviceType}
            onCellClick={handleCellClick}
            selectedCellId={selectedCell?.cell_id || selectedCell?.hex_id}
            services={services}
          />
        )}

        {/* Legend */}
        <div className="legend">
          <div className="legend-title">
            {activeType?.icon} {activeType?.label} Dead Zone
          </div>
          {LEGEND_ITEMS.map(({ color, label }) => (
            <div key={label} className="legend-row">
              <div className="legend-swatch" style={{ background: color }} />
              <span style={{ fontSize:10, color:'var(--text-muted)', fontFamily:'monospace' }}>{label}</span>
            </div>
          ))}
        </div>

        <TimeSlider hour={hour} onChange={handleHourChange} playing={playing} onTogglePlay={togglePlay} />
      </main>

      {/* ── Sidebar ── */}
      <aside className="sidebar">

        {/* Top Dead Zones — compact strip, always visible */}
        <div className="sidebar-section" style={{ paddingBottom: 10 }}>
          <div className="panel-title" style={{ marginBottom: 6 }}>
            Top Dead Zones — {activeType?.label}
          </div>
          {/* Horizontal scrollable pill list — compact, doesn't eat vertical space */}
          <div style={{ display: 'flex', gap: 6, overflowX: 'auto', paddingBottom: 4 }}>
            {topDeadZones.slice(0, 8).map((dz, i) => (
              <div
                key={dz.cell_id}
                onClick={() => handleDzItemClick(dz)}
                style={{
                  flexShrink: 0,
                  background: 'var(--surface2)',
                  border: '1px solid var(--border)',
                  borderRadius: 8,
                  padding: '5px 10px',
                  cursor: 'pointer',
                  minWidth: 90,
                  transition: 'border-color 0.15s',
                }}
                onMouseOver={e => e.currentTarget.style.borderColor = dzColor(dz.dead_zone_score ?? 0)}
                onMouseOut={e => e.currentTarget.style.borderColor = 'var(--border)'}
              >
                <div style={{ fontSize: 9, color: 'var(--text-muted)', marginBottom: 2 }}>#{i+1}</div>
                <div style={{ fontSize: 11, fontWeight: 700, color: dzColor(dz.dead_zone_score ?? 0) }}>
                  {(dz.dead_zone_score ?? 0).toFixed(0)}
                </div>
                <div style={{ fontSize: 10, color: 'var(--text)', fontWeight: 600, lineHeight: 1.3 }}>
                  {(dz.neighbourhood || dz.cell_id || '').split(' ').slice(0,2).join(' ')}
                </div>
                <div style={{ fontSize: 9, color: 'var(--text-muted)' }}>
                  {String(dz.worst_hour ?? 0).padStart(2,'0')}:00
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Selected cell — collapsed when nothing selected */}
        {selectedCell && (
          <div className="sidebar-section" style={{ maxHeight: 260, overflowY: 'auto' }}>
            <div className="panel-title">Selected Cell</div>
            <DrillDown
              cell={selectedCell}
              hour={hour}
              graphStats={graphStats}
              services={services}
              serviceType={serviceType}
            />
          </div>
        )}

        {/* Planning AI — gets most of the remaining space */}
        <PlanningBox topCells={topDeadZones} selectedHour={hour} serviceType={serviceType} />
      </aside>
    </div>
  );
}
