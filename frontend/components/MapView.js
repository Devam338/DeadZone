import { useEffect, useRef, useState } from 'react';

// Absolute thresholds (default)
function scoreToColorAbsolute(score) {
  if (score >= 80) return { fill: '#f85149', opacity: 0.60 };
  if (score >= 65) return { fill: '#f0883e', opacity: 0.50 };
  if (score >= 50) return { fill: '#e3b341', opacity: 0.40 };
  if (score >= 30) return { fill: '#d29922', opacity: 0.28 };
  if (score >= 15) return { fill: '#3fb950', opacity: 0.20 };
  return { fill: '#388bfd', opacity: 0.10 };
}

// Adaptive — compute thresholds from the actual distribution each hour
// so the map always shows a gradient regardless of overall score level.
function buildAdaptiveColorFn(scoresObj) {
  const vals = Object.values(scoresObj).map(v => v.dead_zone_score ?? 0).sort((a,b) => a-b);
  if (vals.length === 0) return scoreToColorAbsolute;
  const pct = (p) => vals[Math.floor((p / 100) * (vals.length - 1))];
  const p20 = pct(20), p40 = pct(40), p60 = pct(60), p80 = pct(80);
  return (score) => {
    if (score >= p80) return { fill: '#f85149', opacity: 0.60 };
    if (score >= p60) return { fill: '#f0883e', opacity: 0.50 };
    if (score >= p40) return { fill: '#e3b341', opacity: 0.40 };
    if (score >= p20) return { fill: '#d29922', opacity: 0.28 };
    return { fill: '#3fb950', opacity: 0.18 };
  };
}

const hourCache = {};

async function fetchHourScores(serviceType, hour) {
  const key = `${serviceType}/${hour}`;
  if (hourCache[key]) return hourCache[key];
  // Try per-type subdir first, fall back to flat legacy
  const urls = [
    `/data/scores_by_hour/${serviceType}/hour_${String(hour).padStart(2,'0')}.json`,
    `/data/scores_by_hour/hour_${String(hour).padStart(2,'0')}.json`,
  ];
  for (const url of urls) {
    try {
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        hourCache[key] = data;
        return data;
      }
    } catch {}
  }
  return {};
}

export default function MapView({ gridGeojson, hour, serviceType = 'all', onCellClick, selectedCellId, services = [], adaptiveColor = true }) {
  const mapRef   = useRef(null);
  const layerRef = useRef(null);
  const svcLayerRef = useRef(null);
  const LRef     = useRef(null);
  const initRef  = useRef(false);
  const [hourScores, setHourScores] = useState({});

  // Prefetch current + adjacent hours
  useEffect(() => {
    [hour, (hour + 1) % 24, (hour + 23) % 24].forEach(h => {
      if (!hourCache[`${serviceType}/${h}`]) {
        fetchHourScores(serviceType, h).then(d => {
          if (h === hour) setHourScores(d);
        }).catch(() => {});
      }
    });
    const cached = hourCache[`${serviceType}/${hour}`];
    if (cached) { setHourScores(cached); return; }
    fetchHourScores(serviceType, hour).then(setHourScores).catch(console.error);
  }, [hour, serviceType]);

  // Init map once
  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;

    import('leaflet').then(L => {
      LRef.current = L.default || L;
      const Lf = LRef.current;
      const map = Lf.map('leaflet-map', { center: [43.710, -79.390], zoom: 11, zoomControl: true, preferCanvas: true });
      Lf.tileLayer(
        'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
        { attribution: '&copy; OpenStreetMap contributors &copy; CARTO', subdomains: 'abcd', maxZoom: 19 }
      ).addTo(map);
      mapRef.current = map;
    });

    return () => { if (mapRef.current) { mapRef.current.remove(); mapRef.current = null; initRef.current = false; } };
  }, []);

  // Redraw hex layer
  useEffect(() => {
    if (!mapRef.current || !LRef.current || !gridGeojson?.features || Object.keys(hourScores).length === 0) return;
    const L = LRef.current;
    if (layerRef.current) { layerRef.current.remove(); layerRef.current = null; }

    const colorFn = adaptiveColor ? buildAdaptiveColorFn(hourScores) : scoreToColorAbsolute;

    layerRef.current = L.geoJSON(gridGeojson, {
      style: (feature) => {
        const cellId = feature.properties?.cell_id || feature.properties?.hex_id;
        const scores = hourScores[cellId] || {};
        const score = scores.dead_zone_score ?? 0;
        const { fill, opacity } = colorFn(score);
        const isSelected = cellId === selectedCellId;
        return {
          fillColor: fill, fillOpacity: isSelected ? 0.92 : opacity,
          color: isSelected ? '#fff' : fill,
          weight: isSelected ? 2.5 : 0.4, opacity: isSelected ? 1 : 0.7,
        };
      },
      onEachFeature: (feature, lyr) => {
        lyr.on('click', (e) => {
          L.DomEvent.stopPropagation(e);
          const cellId = feature.properties?.cell_id || feature.properties?.hex_id;
          const scores = hourScores[cellId] || {};
          onCellClick({ ...feature.properties, ...scores, hour });
        });
        lyr.on('mouseover', (e) => {
          const cellId = feature.properties?.cell_id || feature.properties?.hex_id;
          const sc = hourScores[cellId] || {};
          const hood = sc.neighbourhood || feature.properties.neighbourhood || '';
          lyr.bindTooltip(
            `${hood ? `<b>${hood}</b><br>` : ''}` +
            `Dead Zone: <b>${(sc.dead_zone_score ?? 0).toFixed(0)}/100</b><br>` +
            `Accessibility: ${(sc.accessibility_score ?? 0).toFixed(0)}/100<br>` +
            `TTC: ${sc.ttc_headway_minutes > 0 ? sc.ttc_headway_minutes + ' min headway' : 'no service'}<br>` +
            `<span style="font-size:10px;color:#8b949e">click for full details</span>`,
            { sticky: true }
          ).openTooltip(e.latlng);
        });
        lyr.on('mouseout', () => lyr.closeTooltip());
      },
    }).addTo(mapRef.current);
  }, [gridGeojson, hourScores, selectedCellId, onCellClick, hour]);

  return <div id="leaflet-map" style={{ width: '100%', height: '100%' }} />;
}
