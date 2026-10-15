/* Member names are public login choices; credentials never enter persistent storage. */
(()=>{
let localMode=false;
let options=[], loading=true, optionError='', submitting=false, selected='', message='';
const originalRender=render;
const friendly=code=>code==='member_authentication_failed'?'访问口令不正确，请检查所选数字人和口令。':'暂时无法连接服务，请稍后重试。';
authPanel=function(){
 const active=state.authenticated;
 const choices=options.map(m=>`<option value="${esc(m.member_id)}" ${m.member_id===(selected||state.dispatcherMember)?'selected':''}>${esc(m.name)} · ${esc(m.role)}${localMode||m.configured?'':'（尚未设置口令）'}</option>`).join('');
 const member=options.find(m=>m.member_id===(selected||state.dispatcherMember));
 if(localMode)return `<section class="panel auth-panel"><div class="section-title"><h2>${active?'当前数字人':'选择数字人'}</h2><span>本机工作台 · 无需口令</span></div><p class="small-note">选择数字人后直接进入工作台，可随时切换。</p><label class="field"><span>数字人</span><select id="dispatcherMember" ${loading||submitting?'disabled':''}><option value="">${loading?'正在加载数字人…':'请选择数字人'}</option>${choices}</select></label>${optionError?'<p role="alert">列表加载失败，请重试。</p><button type="button" id="retryAuthOptions">重新加载</button>':''}<p id="authFeedback" class="auth-feedback" role="status">${esc(message)}</p><button class="primary" type="button" id="saveDispatcherAuth" ${loading||submitting||!member?'disabled':''}>${submitting?'正在进入…':active?'切换数字人':'进入工作台'}</button></section>`;
 return `<section class="panel auth-panel"><div class="section-title"><h2>${active?'当前访问身份':'进入工作台'}</h2><span>${active?'已认证':'请选择数字人身份'}</span></div><p class="small-note">选择要使用的数字人，然后输入该身份的访问口令。选择身份本身不会授予访问权限。</p><div class="form-grid"><label class="field"><span>数字人</span><select id="dispatcherMember" autocomplete="username" ${loading||submitting?'disabled':''}><option value="">${loading?'正在加载数字人…':'请选择数字人'}</option>${choices}</select></label><label class="field"><span>访问口令</span><input id="dispatcherToken" type="password" autocomplete="current-password" placeholder="输入此数字人的访问口令" ${submitting?'disabled':''}><small>口令由部署者配置，不能用数字人姓名代替。</small></label></div>${member&&!member.configured?`<div class="auth-feedback" role="status"><p>该数字人尚未设置访问口令。请在部署本项目的电脑上打开终端，进入项目目录后运行：</p><code>python -m butian.auth --member ${esc(member.member_id)}</code><p>按终端提示设置口令，再点击“重新加载列表”。系统不会创建默认口令。</p></div><button type="button" class="text-button" id="retryAuthOptions">重新加载列表</button>`:''}${optionError?`<p class="auth-feedback" role="alert">数字人列表加载失败，请检查服务连接。</p><button type="button" class="text-button" id="retryAuthOptions">重新加载列表</button>`:''}<p id="authFeedback" class="auth-feedback" role="status">${esc(message)}</p><button class="primary" type="button" id="saveDispatcherAuth" ${loading||submitting||!member||!member.configured?'disabled':''}>${submitting?'正在验证…':active?'切换身份并刷新':'进入工作台'}</button></section>`;
};
render=function(){originalRender();if(state.view==='overview'&&state.authenticated){const target=document.querySelector('#content');target.insertAdjacentHTML('afterbegin',authPanel())}};
async function loadOptions(){loading=true;optionError='';render();try{const result=await api('/api/auth/options');options=result.members||[];localMode=!!result.local_mode;state.localMode=localMode}catch(e){optionError=e.message}finally{loading=false;render()}}
async function login(){
 if(submitting)return;
 const id=document.querySelector('#dispatcherMember')?.value||'';
 const token=document.querySelector('#dispatcherToken')?.value||'';
 const feedback=document.querySelector('#authFeedback');
 if(!id||(!localMode&&!token)){if(feedback)feedback.textContent=!id?'请先选择数字人。':'请输入所选数字人的访问口令。';return}
 selected=id;submitting=true;message=localMode?'正在加载工作台…':'正在验证身份…';render();
 try{const response=await fetch('/api/dashboard',{headers:{'X-Butian-Member':id,'X-Butian-Token':token}});const result=await response.json();if(!response.ok||result.status==='error')throw Error(result.error||'request_failed');state.dispatcherMember=id;state.token=token;state.authenticated=true;message='认证成功，已加载工作台。';await sync()}catch(e){message=friendly(e.message)}finally{submitting=false;render()}
}
document.addEventListener('click',e=>{if(e.target.closest('#saveDispatcherAuth')){e.preventDefault();e.stopImmediatePropagation();login()}if(e.target.closest('#retryAuthOptions'))loadOptions()},true);
document.addEventListener('change',e=>{if(e.target.id==='dispatcherMember'){selected=e.target.value;message='';render()}});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id==='dispatcherToken'){e.preventDefault();login()}});
loadOptions();
})();
