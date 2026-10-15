/* Content-free team dashboard, using the existing authentication boundary. */
(()=>{
let data=null, error='', busy=false, generation=0;
const originalOverview=views.overview;
const labels={experience:'经验',lesson:'教训',knowledge:'知识',candidate:'候选',pending_validation:'待验证',warm:'成熟',hot:'活跃',cold:'冷却',disputed:'有争议',success:'实践成功',failure:'实践失败',unknown:'结果未知',done:'完成',running:'执行中',review:'待审核',ready:'就绪',todo:'待办',failed:'失败',cancelled:'已取消'};
const n=v=>Number(v||0).toLocaleString('zh-CN');
function metric(title,value,note){return `<article class="effect-metric"><span>${esc(title)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></article>`}
function distribution(title,items){const entries=Object.entries(items);const max=Math.max(1,...entries.map(x=>x[1]));return `<section class="panel"><div class="section-title"><h2>${esc(title)}</h2></div>${entries.length?entries.map(([key,count])=>`<div class="effect-row"><span>${esc(labels[key]||key)}</span><meter min="0" max="${max}" value="${Number(count)}">${n(count)}</meter><strong>${n(count)}</strong></div>`).join(''):empty('暂无记录')}</section>`}
views.overview=()=>{
 if(!state.dispatcherMember||(!state.localMode&&!state.token))return `${authPanel()}${empty('选择数字人后即可查看团队效果总看板。')}`;
 const controls=`<div class="section-title"><h2>工具效果总看板</h2><button type="button" class="text-button" id="dashboardRefresh" ${busy?'disabled':''}>${busy?'正在读取…':'刷新看板'}</button></div>`;
 if(busy&&!data)return controls+empty('正在读取真实运行数据…');
 if(error)return controls+empty('统计读取失败：'+error+'。请刷新重试。');
 if(!data)return controls+empty('点击“刷新看板”读取统计。');
 const t=data.totals;
 return controls+`<p class="small-note">团队累计数量 · 不展示成员私有正文 · 更新于 ${esc(new Date(data.generated_at*1000).toLocaleString())}</p>${data.partial?'<div class="notice">部分记忆库不可读取，以下为已读取部分，不能视作完整总量。</div>':''}<div class="effect-grid">${metric('数字人 / 注册成员',n(data.members),'注册身份，不等于在线人数')}${metric('记忆总量',n(t.memories),'当前记忆条目，不含历史版本')}${metric('经验',n(data.memory_kinds.experience),'按 experience 类型计数')}${metric('教训',n(data.memory_kinds.lesson),'按 lesson 类型计数')}${metric('待验证经验与记忆',n(t.pending_validation),'候选及 pending_validation 状态')}${metric('帮助避免错误','尚未记录','需要独立避错证据，目前无法统计')}${metric('有用反馈',n(t.useful_evidence),'按记忆和证据摘要去重')}${metric('有害反馈',n(t.harmful_evidence),'用于识别需要纠正的经验')}${metric('决策召回',n(t.recall_decisions),'有召回编号的决策次数')}${metric('召回命中 / 采用',n(t.recall_hits)+' / '+n(t.used_hits),'日志条数，不等于成功任务')}${metric('已反馈决策',n(t.feedback_decisions),'完成使用反馈的召回')}${metric('技能 / 验证批次',n(t.skills)+' / '+n(t.validation_batches),'技能状态与验证批次记录')}</div><div class="effect-columns">${distribution('记忆生命周期',data.memory_states)}${distribution('记忆类型',data.memory_kinds)}${distribution('实践验证结果',data.practice_outcomes)}${distribution('我的可见任务',data.task_states)}</div><section class="panel"><h2>统计口径与覆盖</h2><p>成功读取 ${n(data.coverage.readable)} 个记忆库，未初始化 ${n(data.coverage.not_initialized)} 个，不可读取 ${n(data.coverage.unavailable)} 个。历史版本 ${n(t.versions)} 条，冲突记录 ${n(t.conflicts)} 条。</p><p>${esc(data.avoided_errors.reason)}</p><p>记忆总量包含初始知识，不能将总量当作已验证经验。待验证条目按记忆状态统计；实践结果按独立观察记录统计。任务沿用当前身份的访问范围，团队记忆仅提供数量汇总。没有记录表示当前库为空，不表示已完成验证。</p></section>${originalOverview()}`;
};
async function load(){const current=++generation;data=null;error='';if(!state.dispatcherMember||(!state.localMode&&!state.token)){busy=false;return}busy=true;if(state.view==='overview')render();try{const result=await api('/api/dashboard');if(current===generation)data=result}catch(e){if(current===generation)error=e.message}finally{if(current===generation){busy=false;if(state.view==='overview')render()}}}
const priorSync=sync;
sync=async function(){await priorSync();await load()};
document.addEventListener('click',e=>{if(e.target.closest('#dashboardRefresh'))load()});
if(state.dispatcherMember&&(state.localMode||state.token))load();else if(state.view==='overview')render();
})();
