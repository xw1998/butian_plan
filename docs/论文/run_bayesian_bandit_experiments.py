"""Reproducible Beta-Bernoulli and Thompson-sampling controls.

This is a mechanism experiment, not a language-model experiment and not full
Bayesian reinforcement learning: arms have no state transition or action
consequence beyond Bernoulli reward.
"""
from pathlib import Path
import csv, hashlib, json, platform, random, subprocess, sys
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/论文/experiment_results/bayesian-bandit-20261015"
sys.path.insert(0, str(ROOT))
from butian.core.math_models import ThompsonBandit
SEEDS = [1103, 2207, 3301, 4409, 5513, 6619, 7723, 8831, 9941, 10007]
HORIZON = 1000
ARMS = {"low": 0.30, "high": 0.70}


def sample_beta(rng, a, b):
    return rng.betavariate(a, b)


def run(seed, policy):
    rng = random.Random(seed)
    alpha = {name: 1.0 for name in ARMS}
    beta_p = {name: 1.0 for name in ARMS}
    pulls = {name: 0 for name in ARMS}
    rewards = {name: 0 for name in ARMS}
    rows = []
    bandit = ThompsonBandit(list(ARMS), seed=seed)
    pseudo_regret = 0.0
    for t in range(1, HORIZON + 1):
        if policy == "thompson":
            chosen = bandit.choose()
        elif policy == "epsilon_greedy":
            if rng.random() < 0.1:
                chosen = rng.choice(list(ARMS))
            else:
                chosen = max(ARMS, key=lambda name: alpha[name] / (alpha[name] + beta_p[name]))
        elif policy == "uniform":
            chosen = rng.choice(list(ARMS))
        else:
            raise ValueError(policy)
        reward = int(rng.random() < ARMS[chosen])
        if policy == "thompson":
            bandit.observe(chosen, reward)
        pseudo_regret += max(ARMS.values()) - ARMS[chosen]
        pulls[chosen] += 1
        rewards[chosen] += reward
        alpha[chosen] += reward
        beta_p[chosen] += 1 - reward
        rows.append({"seed": seed, "round": t, "policy": policy, "arm": chosen,
                     "reward": reward, "cumulative_reward": sum(rewards.values()),
                     "posterior_high_mean": alpha["high"] / (alpha["high"] + beta_p["high"])})
    return rows, {"seed": seed, "policy": policy, "pulls": pulls,
                  "rewards": rewards, "total_reward": sum(rewards.values()),
                  "regret": pseudo_regret}


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    rows, summaries = [], []
    for policy in ("thompson", "epsilon_greedy", "uniform"):
        for seed in SEEDS:
            detail, summary = run(seed, policy)
            rows.extend(detail)
            summaries.append(summary)
    with (OUT / "rows.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader(); writer.writerows(rows)
    grouped = {}
    for policy in ("thompson", "epsilon_greedy", "uniform"):
        vals = [x["total_reward"] for x in summaries if x["policy"] == policy]
        regrets = [x["regret"] for x in summaries if x["policy"] == policy]
        grouped[policy] = {"seeds": len(vals), "mean_reward": sum(vals) / len(vals),
                           "mean_regret": sum(regrets) / len(regrets),
                           "min_reward": min(vals), "max_reward": max(vals)}
    # Deterministic PNG charts without relying on the host Matplotlib style registry.
    def chart(name, title, series, ymin=0.0, ymax=1.0):
        w, h, left, top, right, bottom = 1800, 1050, 150, 120, 80, 150
        image = Image.new("RGB", (w, h), "white"); draw = ImageDraw.Draw(image)
        draw.text((left, 30), title, fill="black", font_size=36)
        for tick in range(6):
            y=h-bottom-tick*(h-top-bottom)/5
            value=ymin+(ymax-ymin)*tick/5
            draw.text((15,int(y)-15), f'{value:.2f}', fill='black', font_size=28)
            draw.line((left,int(y),w-right,int(y)), fill=(220,220,220),width=1)
            x=left+tick*(w-left-right)/5
            draw.text((int(x)-25,h-bottom+20),str(1+round(tick*(HORIZON-1)/5)),fill='black',font_size=28)
        draw.text((w//2,h-65),'Round',fill='black',font_size=32)
        draw.line((left, top, left, h-bottom), fill="black", width=3)
        draw.line((left, h-bottom, w-right, h-bottom), fill="black", width=3)
        colors = [(30,90,180), (210,70,40), (40,140,70), (150,80,160)]
        for si, (label, values) in enumerate(series.items()):
            pts=[]
            for i, value in enumerate(values):
                x=left + i* (w-left-right-1)/max(1,len(values)-1)
                y=(h-bottom) - (float(value)-ymin)/(ymax-ymin)*(h-top-bottom)
                pts.append((int(x), int(y)))
            if len(pts)>1: draw.line(pts, fill=colors[si%len(colors)], width=5)
            draw.text((w-right-500, top+si*45), label, fill=colors[si%len(colors)], font_size=28)
        image.save(OUT/name, dpi=(300,300))
    representative = [x for x in rows if x["policy"] == "thompson" and x["seed"] == SEEDS[0]]
    chart("fig_posterior_convergence.png", "Beta posterior convergence", {"posterior high arm":[x["posterior_high_mean"] for x in representative], "true probability":[ARMS["high"]]*HORIZON})
    curves = {}
    for policy in ("thompson", "epsilon_greedy", "uniform"):
        per_seed = [[x["cumulative_reward"] for x in rows if x["policy"] == policy and x["seed"] == seed] for seed in SEEDS]
        curves[policy] = [sum(curve[i] for curve in per_seed)/len(per_seed) for i in range(HORIZON)]
    chart("fig_cumulative_reward.png", "Mean cumulative reward", curves, 0, HORIZON)
    chart("fig_pseudo_regret.png", "Mean pseudo-regret", {p:[grouped[p]["mean_regret"]]*HORIZON for p in grouped}, 0, HORIZON*0.8)
    result = {"experiment": "beta_bernoulli_thompson_sampling", "actual_model_runs": 0,
              "seeds": SEEDS, "horizon": HORIZON, "true_reward_probabilities": ARMS,
              "summary": grouped, "limitations": [
                  "No language-model calls; this is a mechanism control.",
                  "Context-free Bernoulli bandit, not full Bayesian reinforcement learning.",
                  "Ten seeds support descriptive comparison only; no superiority theorem."],
              "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "python": platform.python_version()}
    (OUT / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file()}
    (OUT / "manifest.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    print(json.dumps(grouped, ensure_ascii=False))


if __name__ == "__main__":
    main()
