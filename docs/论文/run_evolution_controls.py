"""Analytic/synthetic controls, not neural-model training or LLM benchmarks."""
from pathlib import Path
import json,random,hashlib,subprocess,platform,math
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
out=Path(__file__).parent/'experiment_results/evolution-controls-20261015-r2';out.mkdir(exist_ok=False)
r=random.Random(1103)
# Explicit finite-capacity table: P bits can encode at most P independent binary facts.
capacity=[{'P_bits':p,'latent_facts':128,'retention_upper_bound':min(1,p/128)} for p in [8,16,32,64,128,256]]
# Conjugate label supervision: optimum Bernoulli CE q*=label frequency.
supervision=[{'label_error':e,'optimal_wrong_answer_probability':e} for e in [0,.1,.2,.4,.5]]
# Occupancy gain from use count vs independent labels. Closed form checked with Monte Carlo.
coverage=[]
for n in [0,10,50,100,500,1000]:
 trials=[]
 for seed in range(200):
  rr=random.Random(seed);seen=set(rr.randrange(100) for _ in range(n));trials.append(len(seen)/100)
 expected=1-(.99**n)
 coverage.append({'uses':n,'expected_coverage':expected,'monte_carlo_mean':sum(trials)/len(trials)})
 assert abs(coverage[-1]['monte_carlo_mean']-expected)<.015
# Correlated repetitions have no diversity value; noisy user judgments are not ground truth.
noise=[]
for eta in [.1,.3,.49,.6]:
 for n in [1,3,9,31]:
  acc=sum(math.comb(n,k)*(1-eta)**k*eta**(n-k) for k in range(n//2+1,n+1))
  noise.append({'independent_labels':n,'error_probability':eta,'majority_accuracy':acc})
results={'kind':'synthetic_control','code_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'python':platform.python_version(),'capacity':capacity,'supervision':supervision,'coverage':coverage,'label_majority':noise,'limitations':['Finite lookup model is not a transformer capacity law.','Synthetic coverage is not measured LLM self-improvement.','No model weights were trained or changed.','Use count needs diversity, correct feedback, review, and retrieval; quantity alone is insufficient.']}
(out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
f=Figure(figsize=(10,4));FigureCanvasAgg(f);a,b=f.subplots(1,2);a.plot([x['P_bits'] for x in capacity],[x['retention_upper_bound'] for x in capacity],marker='o');a.set(xlabel='Available bits (toy capacity)',ylabel='Retention bound');b.plot([x['uses'] for x in coverage],[x['expected_coverage'] for x in coverage],label='Expected coverage');b.plot([x['uses'] for x in coverage],[x['monte_carlo_mean'] for x in coverage],'o',label='Monte Carlo');b.set(xlabel='Independent task uses',ylabel='Task coverage');b.legend();f.tight_layout();f.savefig(out/'capacity_coverage.png',dpi=300)
(out/'manifest.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()},indent=2),encoding='utf-8');print(json.dumps(results,indent=2))
