import { useEffect, useRef } from 'react';

const HOUR_LABELS = ['12a','','','','','','6a','','','','','','12p','','','','','','6p','','','','','11p'];

export default function TimeSlider({ hour, onChange, playing, onTogglePlay }) {
  const intervalRef = useRef(null);

  useEffect(() => {
    if (playing) {
      intervalRef.current = setInterval(() => {
        onChange(h => (h + 1) % 24);
      }, 600);
    } else {
      clearInterval(intervalRef.current);
    }
    return () => clearInterval(intervalRef.current);
  }, [playing, onChange]);

  const fmt = (h) => {
    const ampm = h < 12 ? 'AM' : 'PM';
    const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
    return { h12: String(h12).padStart(2, ' '), ampm };
  };
  const { h12, ampm } = fmt(hour);

  return (
    <div className="time-control">
      <div className="time-control-row">
        <button className="play-btn" onClick={onTogglePlay} title={playing ? 'Pause' : 'Play animation'}>
          {playing ? '⏸' : '▶'}
        </button>
        <span className="time-label">{h12}</span>
        <span className="time-ampm">{ampm}</span>
        <div className="slider-wrap">
          <input
            type="range"
            min={0}
            max={23}
            value={hour}
            onChange={e => onChange(parseInt(e.target.value))}
          />
          <div className="hour-ticks">
            {HOUR_LABELS.map((l, i) => <span key={i}>{l}</span>)}
          </div>
        </div>
      </div>
    </div>
  );
}
