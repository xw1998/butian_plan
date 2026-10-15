"""Plots are regenerated from recorded rows, never hand-entered scores."""
from pathlib import Path
import argparse, collections, csv, hashlib, json, sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
# Some bundled wheels include AppleDouble ._*.mplstyle resource forks.
# Ignore only those non-style files during import; do not modify the Python installation.
_original_glob = Path.glob
def _valid_style_files(self, pattern):
    return (p for p in _original_glob(self, pattern) if not (pattern == '*.mplstyle' and p.name.startswith('._')))
Path.glob = _valid_style_files
try:
    import matplotlib.pyplot as plt
finally:
    Path.glob = _original_glob
sys.path.insert(0, r'C:/Users/14905/AppData/Roaming/WPS 灵犀/serverdir/official_skills/paper-writer/scripts')
import scientific_visualization as sv
sv.setup_chart_font('en')
plt.rcParams.update({'font.size':9, 'axes.labelsize':9, 'legend.fontsize':7, 'figure.constrained_layout.use':True})
METHODS=['B0-scan','B0-index','B1-static','B2-full','A-no-version','A-no-write','A-no-persist']
COLORS=['#777777','#1f77b4','#ff7f0e','#008066','#d62728','#9467bd','#8c564b']

def write_json(p,v): p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def save(fig,folder,name):
    fig.savefig(folder/(name+'.png'),dpi=300)
    fig.savefig(folder/(name+'.pdf'))
    plt.close(fig)

def main(root):
    rows=json.loads((root/'rows.json').read_text(encoding='utf-8'))
    summary=json.loads((root/'summary.json').read_text(encoding='utf-8'))
    seeds=summary['seeds']; rounds=range(1,13)
    by=collections.defaultdict(list)
    for r in rows: by[r['method'],r['round']].append(r)
    keys=[(r['seed'],r['round'],r['entity'],r['method']) for r in rows]
    assert len(keys)==len(set(keys))==len(seeds)*12*12*len(METHODS)
    assert all(r['correct']+r['wrong']+r['unknown']==1 for r in rows)
    for key in {(r['seed'],r['round'],r['entity']) for r in rows}:
        group=[r for r in rows if (r['seed'],r['round'],r['entity'])==key]
        assert len(group)==7 and len({json.dumps(r['reference'],sort_keys=True) for r in group})==1
        assert len({(r['raw'],r['version'],r['source_available']) for r in group})==1
    # Cold start and causality: experience is acquired only after source lookup, no answer cache prefill.
    assert all(not r['memory_hit'] for r in rows if r['round']==1)
    assert all(not r['writes'] for r in rows if r['method']=='A-no-write')
    assert all(not r['writes'] for r in rows if r['method']=='B1-static' and r['round']>1)
    assert all(r['correct'] for r in rows if r['method']=='B2-full')
    assert all(not r['source_reads'] for r in rows if not r['source_available'])
    assert all(r['unknown'] for r in rows if r['method']=='B0-index' and not r['source_available'])
    audits=json.loads((root/'version-audits.json').read_text(encoding='utf-8'))
    assert all(a['valid'] for a in audits)
    figures=root/'figures';figures.mkdir(exist_ok=True)
    fig,axs=plt.subplots(2,1,figsize=(6.9,4.9),sharex=True)
    for m in ['B0-index','B1-static','B2-full','A-no-version']:
        color=COLORS[METHODS.index(m)]
        axs[0].plot(list(rounds),[100*np.mean([r['correct'] for r in by[m,t]]) for t in rounds],label=m,color=color,marker='o',markersize=3)
        axs[1].plot(list(rounds),[np.mean([r['source_reads'] for r in by[m,t]]) for t in rounds],label=m,color=color,marker='o',markersize=3)
    for ax in axs:
        ax.axvline(5,ls=':',color='0.5');ax.axvline(9,ls=':',color='0.5');ax.axvspan(5.5,7.5,alpha=.09,color='gray');ax.grid(alpha=.2)
    axs[0].set_ylabel('First-pass correctness (%)');axs[0].set_ylim(35,103);axs[0].legend(ncol=2)
    axs[1].set_ylabel('Source reads / task');axs[1].set_xlabel('Round');axs[1].set_xticks(list(rounds))
    save(fig,figures,'fig1_round_dynamics')
    fig,ax=plt.subplots(figsize=(3.4,2.5))
    for m in ['B0-index','B1-static','B2-full','A-no-persist']:
        ax.plot(list(rounds),np.cumsum([sum(r['source_reads'] for r in by[m,t])/len(seeds) for t in rounds]),label=m,color=COLORS[METHODS.index(m)])
    ax.set_xlabel('Round');ax.set_ylabel('Cumulative source reads / seed');ax.grid(alpha=.2);ax.legend()
    save(fig,figures,'fig2_cumulative_reads')
    fig,ax=plt.subplots(figsize=(6.9,2.5))
    offsets=np.arange(len(METHODS));base=np.zeros(len(METHODS))
    for key,color,label in [('source_reads','#555555','Source reads'),('queries','#6baed6','Context queries'),('writes','#74c476','Verified commits')]:
        vals=np.array([summary['summary'][m][key] for m in METHODS])/len(seeds)
        ax.bar(offsets,vals,bottom=base,label=label,color=color)
        base+=vals
    ax.set_yscale('log');ax.set_ylim(1,5000);ax.set_xticks(offsets,METHODS,rotation=15);ax.set_ylabel('Logical operations / seed (log)');ax.legend(ncol=3)
    save(fig,figures,'fig3_operation_accounting')
    fig,ax=plt.subplots(figsize=(3.4,2.7))
    for i,m in enumerate(METHODS):
        vals=[1000*r['elapsed_s'] for r in rows if r['method']==m]
        ax.boxplot(vals,positions=[i],widths=.55,showfliers=False,patch_artist=True,boxprops={'facecolor':COLORS[i]})
    ax.set_yscale('log');ax.set_xticks(range(len(METHODS)),METHODS,rotation=60,ha='right');ax.set_ylabel('Measured local time (ms; log)')
    save(fig,figures,'fig4_local_latency')
    mat=np.array([[100*np.mean([r['correct'] for r in rows if r['method']==m and r['family']==f]) for f in ['calibration','schema','tool']] for m in METHODS])
    fig,ax=plt.subplots(figsize=(3.4,2.8));im=ax.imshow(mat,cmap='Blues',vmin=75,vmax=100,aspect='auto')
    ax.set_xticks(range(3),['Calibration','Schema','Tool']);ax.set_yticks(range(7),METHODS)
    for i in range(7):
        for j in range(3):ax.text(j,i,f'{mat[i,j]:.1f}',ha='center',va='center',color='white' if mat[i,j]>94 else 'black',fontsize=8)
    fig.colorbar(im,ax=ax,label='Correctness (%)')
    save(fig,figures,'fig5_family_ablation')
    sens=json.loads((root/'sensitivity.json').read_text(encoding='utf-8'))
    matrix=np.array([[100*np.mean([r['hit'] for r in sens if r['size']==n and r['budget']==b]) for b in [128,512,2048]] for n in [8,32,96]])
    fig,ax=plt.subplots(figsize=(3.4,2.6));im=ax.imshow(matrix,vmin=0,vmax=100,cmap='Greens',aspect='auto')
    ax.set_xticks(range(3),[128,512,2048]);ax.set_yticks(range(3),[8,32,96]);ax.set_xlabel('L3 character budget');ax.set_ylabel('Stored rules')
    for i in range(3):
        for j in range(3):ax.text(j,i,f'{matrix[i,j]:.0f}%',ha='center',va='center',color='white' if matrix[i,j]>70 else 'black')
    save(fig,figures,'fig6_budget_sensitivity')
    fig,ax=plt.subplots(figsize=(3.4,2.5))
    ratios=np.geomspace(.05,100,250)
    n=summary['summary']['B0-index']['tasks']; full=summary['summary']['B2-full']; bb=summary['summary']['B0-index']
    # Normalize lookup cost c_q=1; one logical verified commit costs c_w=2.
    delta=(bb['source_reads']-full['source_reads'])*ratios-full['queries']-2*full['writes']
    threshold=(full['queries']+2*full['writes'])/(bb['source_reads']-full['source_reads'])
    ax.plot(ratios,delta/n,color='#008066');ax.axhline(0,color='gray',ls='--');ax.axvline(threshold,color='gray',ls=':')
    ax.set_xscale('log');ax.set_xlabel('Source-read / recall cost ratio');ax.set_ylabel('Modeled savings / task');ax.text(.07,max(delta/n)*.6,f'Break-even: {threshold:.2f}',fontsize=8)
    save(fig,figures,'fig7_cost_threshold')
    # Independent seed averages and bootstrap intervals; no turn-level pseudo-replication.
    fig,ax=plt.subplots(figsize=(3.4,2.5))
    baselines=['B0-index','B1-static','A-no-version'];effects=[];lo=[];hi=[]
    for b in baselines:
        obj=summary['paired_seed_bootstrap'][b]['accuracy_delta'];effects.append(obj['mean']*100);lo.append((obj['mean']-obj['cluster_bootstrap_95_percentile'][0])*100);hi.append((obj['cluster_bootstrap_95_percentile'][1]-obj['mean'])*100)
    ax.errorbar(effects,range(3),xerr=[lo,hi],fmt='o',color='#008066',capsize=4);ax.set_yticks(range(3),baselines);ax.set_xlabel('B2 correctness gain (percentage points)');ax.grid(axis='x',alpha=.2)
    save(fig,figures,'fig8_paired_effects')
    data=[]
    for m in METHODS:
        for t in rounds:
            rr=by[m,t];data.append({'method':m,'round':t,'tasks':len(rr),'correct':sum(r['correct'] for r in rr),'wrong':sum(r['wrong'] for r in rr),'unknown':sum(r['unknown'] for r in rr),'source_reads':sum(r['source_reads'] for r in rr),'hits':sum(r['memory_hit'] for r in rr)})
    with (root/'round-summary.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(data[0]));writer.writeheader();writer.writerows(data)
    write_json(root/'analysis.json',{'records_validated':len(rows),'paired_tasks':len(rows)//7,'version_chains_validated':len(audits),'figures':8,'source_read_reduction_vs_index':1-full['source_reads']/bb['source_reads'],'cost_threshold_source_to_query_when_write_cost_two':threshold,'modeled_cost_warning':'Figure 7 uses assumed cost weights; not measured dollars or network latency','sensitivity_queries':len(sens)})
    print('Validated',len(rows),'records;',len(audits),'version chains; 8 figures. Cost threshold:',threshold)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root');main(Path(p.parse_args().root))
