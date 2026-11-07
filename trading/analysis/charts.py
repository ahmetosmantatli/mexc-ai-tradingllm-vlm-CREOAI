"""Headless charts rendered from the same immutable bars sent to the model.

Matplotlib is lazy-imported, so the text-only engine has no renderer requirement.
No website screenshots or browser sessions are involved. Artifacts are evidence
of the inspected market view, not screenshots proving an exchange fill.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import os
import threading

_RENDER_LOCK = threading.RLock()


@dataclass(frozen=True)
class ChartArtifact:
    path: str
    timeframe: str
    sha256: str
    closed_at: int

    def as_dict(self):
        return dict(path=self.path, timeframe=self.timeframe, sha256=self.sha256, closed_at=self.closed_at)


def render_bundle(bundle, output_dir):
    """Render three PNGs (one per timeframe), returning immutable hashed assets.

    Each title includes source, symbol, timeframe and last closed bar. Filenames
    are content-addressed, preventing a newer chart overwriting trade evidence.
    """
    if not re.fullmatch(r'[A-Z0-9]+_USDT', bundle.symbol):
        raise ValueError('invalid chart symbol')
    with _RENDER_LOCK:
        return _render(bundle, output_dir)


def _render(bundle, output_dir):
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Server/container and sandbox users may have no writable home directory.
    os.environ.setdefault('MPLCONFIGDIR', str(root / '.matplotlib'))
    os.environ.setdefault('XDG_CACHE_HOME', str(root / '.cache'))
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.patches import Rectangle

    result = []
    for view in bundle.views:
        if view.label not in ('4h', '15m', '5m'):
            raise ValueError('invalid chart timeframe')
        path = root/f'{bundle.symbol}-{view.label}-{view.fingerprint[:20]}.png'
        if not path.exists():
            fig = Figure(figsize=(10, 6), dpi=110, facecolor='#0f172a')
            FigureCanvasAgg(fig)
            ax, volume_ax = fig.subplots(2, 1, sharex=True, gridspec_kw={'height_ratios': [4, 1]})
            for axis in (ax, volume_ax):
                axis.set_facecolor('#0f172a')
                axis.tick_params(colors='#cbd5e1', labelsize=8)
                axis.grid(color='#334155', alpha=.4)
                for spine in axis.spines.values():
                    spine.set_color('#334155')
            for i, candle in enumerate(view.candles):
                color = '#34d399' if candle.close >= candle.open else '#fb7185'
                ax.vlines(i, candle.low, candle.high, color=color, linewidth=.8)
                height = abs(candle.close-candle.open)
                # Doji receives a visible horizontal tick, never fabricated height.
                if height == 0:
                    ax.hlines(candle.open, i-.32, i+.32, color=color, linewidth=1)
                else:
                    ax.add_patch(Rectangle((i-.32, min(candle.open, candle.close)), .64, height, facecolor=color, edgecolor=color))
                volume_ax.bar(i, candle.volume, width=.64, color=color, alpha=.7)
            for key, color in [('last_swing_high', '#fbbf24'), ('last_swing_low', '#60a5fa')]:
                swing = view.features.get(key)
                if swing:
                    ax.axhline(swing['price'], color=color, linestyle='--', linewidth=.7, alpha=.7)
            close_time = datetime.fromtimestamp(view.closed_at, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            ax.set_title(f'MEXC {bundle.symbol} | USDT perpetual | {view.label}\nClosed candles through {close_time}', color='#f8fafc', fontsize=11)
            ax.set_ylabel('USDT', color='#cbd5e1')
            volume_ax.set_ylabel('Volume', color='#cbd5e1')
            ticks = sorted(set([0, len(view.candles)//3, 2*len(view.candles)//3, len(view.candles)-1]))
            volume_ax.set_xticks(ticks)
            volume_ax.set_xticklabels([datetime.fromtimestamp(view.candles[i].timestamp, timezone.utc).strftime('%m-%d %H:%M') for i in ticks])
            ax.autoscale_view()
            ax.set_xlim(-1, len(view.candles))
            fig.text(.02, .01, f'PA rules: {view.features["definition_version"]} | snapshot {view.fingerprint[:16]} | historical formations, no return guarantee', color='#94a3b8', fontsize=7)
            fig.tight_layout(rect=(0, .035, 1, 1))
            temporary = path.with_suffix('.tmp.png')
            fig.savefig(temporary, format='png', facecolor=fig.get_facecolor())
            temporary.replace(path)
            fig.clear()
        result.append(ChartArtifact(str(path.resolve()), view.label, sha256(path.read_bytes()).hexdigest(), view.closed_at))
    return result
