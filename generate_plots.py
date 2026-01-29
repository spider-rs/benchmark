"""Generate benchmark plots from official_results data."""

import json
import colorsys
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D

# Register custom font
FONT_PATH = Path(__file__).parent / "fonts" / "GeistMono-Medium.otf"
font_manager.fontManager.addfont(str(FONT_PATH))
plt.rcParams["font.family"] = "Geist Mono"

RESULTS_DIR = Path(__file__).parent / "official_results"
OUTPUT_DIR = Path(__file__).parent / "official_plots"
N_BOOTSTRAP = 1000
EXPECTED_TASKS = 100
HIGHLIGHT_MODEL = "ChatBrowserUse-2"


@dataclass
class Theme:
    name: str
    background: str
    foreground: str
    border: str
    primary: str


LIGHT = Theme(
    name="light",
    background="#FAFAFA",
    foreground="#1A1A1A",
    border="#E5E5E5",
    primary="#F97316",
)

DARK = Theme(
    name="dark",
    background="#0A0A0A",
    foreground="#FAFAFA",
    border="#2A2A2A",
    primary="#FB923C",
)


def index_to_color(index: int, total: int, theme: Theme, muted: bool = False) -> str:
    """Generate evenly-spaced color based on index. Muted version has lower saturation."""
    hue = index / total
    
    if theme.name == "dark":
        sat = 0.30 if muted else 0.65
        light = 0.40 if muted else 0.55
    else:
        sat = 0.35 if muted else 0.70
        light = 0.50 if muted else 0.45
    
    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


def build_model_colors(models: list[str], theme: Theme) -> dict[str, str]:
    """Build color mapping for all models. Highlighted model gets primary, others get evenly-spaced hues."""
    colors = {}
    non_highlighted = sorted([m for m in models if m != HIGHLIGHT_MODEL])
    for i, model in enumerate(non_highlighted):
        colors[model] = index_to_color(i, len(non_highlighted), theme, muted=True)
    if HIGHLIGHT_MODEL in models:
        colors[HIGHLIGHT_MODEL] = theme.primary
    return colors


def load_results() -> dict[str, list[dict]]:
    """Load all results files, filtering incomplete runs."""
    results = {}
    for f in RESULTS_DIR.glob("*.json"):
        model = f.stem.split("_model_")[-1]
        runs = json.loads(f.read_text())
        valid_runs = []
        for run in runs:
            if run["tasks_completed"] != EXPECTED_TASKS:
                print(f"WARNING: Incomplete run for {model} ({run['run_start']}): {run['tasks_completed']}/{EXPECTED_TASKS} tasks")
            else:
                valid_runs.append(run)
        results[model] = valid_runs
    return results


def compute_accuracies(runs: list[dict]) -> list[float]:
    return [r["tasks_successful"] / r["tasks_completed"] for r in runs if r["tasks_completed"] > 0]


def compute_tasks_per_hour(runs: list[dict]) -> list[float]:
    return [3600 * r["tasks_completed"] / r["total_duration"] for r in runs if r["tasks_completed"] > 0 and r["total_duration"] > 0]


def bootstrap_ci(values: list[float], n: int = N_BOOTSTRAP) -> tuple[float, float, float]:
    """Returns (mean, lower, upper) with 95% CI."""
    arr = np.array(values)
    means = [np.mean(np.random.choice(arr, size=len(arr), replace=True)) for _ in range(n)]
    return float(np.mean(arr)), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def apply_theme(ax, theme: Theme):
    """Apply minimal theme styling."""
    ax.set_facecolor(theme.background)
    ax.figure.set_facecolor(theme.background)
    ax.tick_params(colors=theme.foreground, which="both", labelsize=9)
    ax.xaxis.label.set_color(theme.foreground)
    ax.yaxis.label.set_color(theme.foreground)
    ax.title.set_color(theme.foreground)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color(theme.border)
    ax.spines["left"].set_color(theme.border)
    ax.yaxis.grid(True, color=theme.border, linestyle="-", linewidth=0.5, alpha=0.5)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)


def add_legend(ax, items: list[tuple[str, str]], theme: Theme):
    """Custom legend with solid circles (no error bar icons)."""
    handles = [Line2D([0], [0], marker="o", color="none", markerfacecolor=color, 
                      markersize=8, markeredgecolor="none") for _, color in items]
    labels = [name for name, _ in items]
    ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(1.02, 1), 
              frameon=False, fontsize=9, labelcolor=theme.foreground)


def plot_accuracy_by_model(results: dict[str, list[dict]], theme: Theme):
    """Bar chart with highlighted model in primary, others in unique muted colors."""
    model_colors = build_model_colors(list(results.keys()), theme)
    data = []
    for model, runs in results.items():
        accs = compute_accuracies(runs)
        if not accs:
            continue
        mean, lo, hi = bootstrap_ci(accs)
        color = model_colors[model]
        data.append({"model": model, "mean": mean * 100, "err_lo": (mean - lo) * 100, "err_hi": (hi - mean) * 100, "color": color})
    
    if not data:
        return
    
    data.sort(key=lambda x: x["mean"], reverse=True)

    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(data))
    err_color = "#666666" if theme.name == "light" else "#888888"
    
    ax.bar(x, [d["mean"] for d in data], 
           yerr=[[d["err_lo"] for d in data], [d["err_hi"] for d in data]], 
           capsize=3, color=[d["color"] for d in data], edgecolor="none", ecolor=err_color, width=0.7)
    
    ax.set_xticks(x)
    ax.set_xticklabels([d["model"] for d in data], rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("Score (%)", fontsize=10)
    ax.set_ylim(0, 100)
    
    apply_theme(ax, theme)
    fig.tight_layout()
    ax.text(0.5, 0.95, "Success Rate", transform=ax.transAxes, ha="center", va="top", fontsize=16, color=theme.foreground)
    fig.savefig(OUTPUT_DIR / f"accuracy_by_model_{theme.name}.png", dpi=150, facecolor=theme.background)
    plt.close(fig)


def plot_accuracy_vs_throughput(results: dict[str, list[dict]], theme: Theme):
    """Scatter plot with highlighted model prominent, others in unique muted colors."""
    model_colors = build_model_colors(list(results.keys()), theme)
    data = []
    for model, runs in results.items():
        accs = compute_accuracies(runs)
        tph = compute_tasks_per_hour(runs)
        if not accs or not tph:
            continue
        acc_mean, acc_lo, acc_hi = bootstrap_ci(accs)
        tph_mean, tph_lo, tph_hi = bootstrap_ci(tph)
        color = model_colors[model]
        data.append({
            "model": model, "color": color,
            "acc": acc_mean * 100, "acc_lo": (acc_mean - acc_lo) * 100, "acc_hi": (acc_hi - acc_mean) * 100,
            "tph": tph_mean, "tph_lo": tph_mean - tph_lo, "tph_hi": tph_hi - tph_mean,
        })
    
    if not data:
        return
    
    err_color = "#666666" if theme.name == "light" else "#888888"
    fig, ax = plt.subplots(figsize=(10, 6))
    
    legend_items = []
    
    # Plot non-highlighted models first
    for d in data:
        if d["model"] == HIGHLIGHT_MODEL:
            continue
        ax.errorbar(d["tph"], d["acc"], xerr=[[d["tph_lo"]], [d["tph_hi"]]], yerr=[[d["acc_lo"]], [d["acc_hi"]]],
                    fmt="o", capsize=3, color=d["color"], ecolor=err_color, markersize=8)
        legend_items.append((d["model"], d["color"]))
    
    # Plot highlighted model last (on top)
    highlighted = next((d for d in data if d["model"] == HIGHLIGHT_MODEL), None)
    if highlighted:
        ax.errorbar(highlighted["tph"], highlighted["acc"], 
                    xerr=[[highlighted["tph_lo"]], [highlighted["tph_hi"]]], 
                    yerr=[[highlighted["acc_lo"]], [highlighted["acc_hi"]]],
                    fmt="o", capsize=3, color=highlighted["color"], ecolor=err_color, markersize=10)
        legend_items.append((highlighted["model"], highlighted["color"]))
    
    ax.set_xlabel("Tasks per Hour", fontsize=10)
    ax.set_ylabel("Score (%)", fontsize=10)
    ax.set_ylim(0, 100)
    
    add_legend(ax, legend_items, theme)
    apply_theme(ax, theme)
    fig.tight_layout()
    ax.text(0.5, 0.95, "Success vs. Throughput", transform=ax.transAxes, ha="center", va="top", fontsize=16, color=theme.foreground)
    fig.savefig(OUTPUT_DIR / f"accuracy_vs_throughput_{theme.name}.png", dpi=150, facecolor=theme.background)
    plt.close(fig)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = load_results()
    
    for theme in [LIGHT, DARK]:
        plot_accuracy_by_model(results, theme)
        plot_accuracy_vs_throughput(results, theme)
    
    print(f"Saved plots to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
