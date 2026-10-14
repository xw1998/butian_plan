"""Deterministic numerical controls; never actual-model observations."""
from pathlib import Path
import sys, json, hashlib, platform, subprocess, itertools, random
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from butian.core.math_models import BetaBelief, bayesian_evidence_update, expected_information_gain, allocate_by_utility, Utility, queue_metrics, ThompsonBandit
from scipy.stats import beta, t
import matplotlib
matplotlib.use('Agg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

def main():
    out = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).parent/'experiment_results'/'math-controls-20261015'
    out.mkdir(parents=True, exist_ok=False)
    rows=[]
    for weight in [.1,.25,.5,1.0]:
        b=BetaBelief()
        for n in range(1,21):
            b=bayesian_evidence_update(b,outcome='success',evidence_quality=weight)
            lo,hi=b.interval()
            assert max(abs(lo-beta.ppf(.025,b.alpha,b.beta)),abs(hi-beta.ppf(.975,b.alpha,b.beta)))<1e-9
            rows.append(dict(weight=weight,n=n,mean=b.mean,lower=lo,upper=hi))
    rng=random.Random(1103); checks=[]
    for trial in range(200):
        opts={str(i):Utility(rng.uniform(0,10),rng.uniform(0,3),rng.uniform(0,1),0,0) for i in range(8)}
        budget=rng.uniform(0,10); selected=allocate_by_utility(opts,budget=budget)
        score=sum(opts[k].score() for k in selected)
        brute=max(sum(opts[k].score() for k in opts if mask>>int(k)&1) for mask in range(256) if sum(opts[k].cost for k in opts if mask>>int(k)&1)<=budget)
        assert abs(score-brute)<1e-9
        checks.append(dict(trial=trial,budget=budget,score=score,brute=brute))
    bandits=[]
    for seed in [1103,2207,3301]:
        r=random.Random(seed); b=ThompsonBandit(['low','high'],seed=seed); counts={'low':0,'high':0}; reward=0
        for _ in range(1000):
            a=b.choose(); y=int(r.random()<({'low':.3,'high':.7}[a])); b.observe(a,y); counts[a]+=1; reward+=y
        bandits.append(dict(seed=seed,pulls=counts,rewards=reward,horizon=1000))
    source=dict(mean=3.0,sample_std=5**.5/2**.5,df=4)
    margin=float(t.ppf(.975,4)*(2.5**.5)/(5**.5)); source['ci95']=[3-margin,3+margin]
    results=dict(execution_kind='mechanism_control',actual_model_runs=0,seed=[1103,2207,3301],commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),python=platform.python_version(),platform=platform.platform(),beta=rows,allocation_checks=checks,bandit=bandits,information_gain=expected_information_gain([.5,.5],[[1,0],[0,1]],[.5,.5]),queue=[queue_metrics(x,4) for x in [2,4,6]],reference_recalculation={'repeat_pa':3400,'transfer_pa':1700,'transfer_kpa':1.7,'source_practice':source,'conflict':{'current':.34,'firmware':'2.0','missing_version':'unknown'},'recovery_velocity_m_s':2.3},limitations=['Synthetic controls do not estimate agent performance.','Bandit simulation has only three seeds; no general superiority claim.','Reference recalculation is not a model task execution.'])
    (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    import csv
    with (out/'beta.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    fig=Figure(); FigureCanvasAgg(fig); ax=fig.subplots()
    for weight in [.1,.25,.5,1.0]:
        rs=[x for x in rows if x['weight']==weight];ax.plot([x['n'] for x in rs],[x['mean'] for x in rs],label=f'weight={weight}')
    ax.set_xlabel('Success observations');ax.set_ylabel('Posterior mean');ax.legend();fig.tight_layout();fig.savefig(out/'beta_sensitivity.png',dpi=300)
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}
    files['script']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (out/'manifest.json').write_text(json.dumps(files,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(out),'beta_checks':len(rows),'allocation_checks':len(checks),'bandit':bandits,'source':source},ensure_ascii=False))
if __name__=='__main__': main()
