import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID,createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const python=process.env.BUTIAN_TEST_PYTHON;assert(python);
const home=mkdtempSync(join(root,'.agent_tmp','official-dream-dispatch-')),token=randomUUID();
const setup=JSON.parse(execFileSync(python,['-c',`import sys,json; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); s.add_member('reader','Reader'); s.store.set_member_token('reader',sys.argv[2]); m=s.memory('reader'); [m.put(str(i),'lesson',text,status='candidate') for i,text in enumerate(['125 cm divided by 100 is 1.25 m','230 cm divided by 100 is 2.3 m','Unit conversion requires the original unit'])]; m.close(); r=s.maintenance_tick(); print(json.dumps(r['dream_reasoning_tasks'][0])); s.close()`,home,token],{cwd:root,encoding:'utf8',env:{...process.env,BUTIAN_MAINTENANCE_ENABLED:'0'}}));
const c=new Client({name:'official-dream-dispatch-acceptance',version:'1.0.0'});
async function call(name,args={}){const r=await c.callTool({name,arguments:{actor_member_id:'reader',member_id:'reader',token,...args}});assert.equal(r.isError,false);return JSON.parse(r.content.find(x=>x.type==='text').text);}
try{
 await c.connect(new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env:{...process.env,BUTIAN_HOME:home,BUTIAN_MAINTENANCE_ENABLED:'0'},stderr:'pipe'}));
 const pkg=await call('butian_execution_export',{task_id:setup.task_id});
 const directory=new Set((await c.listTools()).tools.map(t=>t.name));
 for(const name of pkg.context.match(/butian_[a-z_]+/g)||[])assert(directory.has(name),`Unknown instruction tool: ${name}`);
 const sources=await call('butian_context_recover',{task_id:setup.task_id,payload_field:'dream_source_snapshot'});
 const material=await call('butian_context_recover',{task_id:setup.task_id,payload_field:'candidate_content'});
 for(const item of [sources,material])assert.equal(item.sha256,createHash('sha256').update(JSON.stringify(item.value)).digest('hex'));
 assert.equal(sources.value.length,3);assert(material.value.includes('125 cm'));
 const ids=sources.value.map(x=>x.memory_id);
 await call('butian_dream_distill',{memory_ids:ids,summary:'Convert centimeters by dividing by 100; preserve original units',conflicts:[],discarded_items:[],information_loss:0});
 await call('butian_dream_abstract',{memory_ids:ids,pattern:'Centimeters to meters divide by 100',applicability:['Input explicitly in centimeters'],failure_conditions:['Unknown unit or non-length input'],counterexamples:[]});
 let hypothesis=await call('butian_dream_propose',{candidate_id:setup.candidate_id,statement:'125 centimeters equals 1.25 meters',prior_reliability:0.5,argument:'Bounded independent calculation'});
 hypothesis=typeof hypothesis==='object'?hypothesis.hypothesis_id:hypothesis;
 const action=await call('butian_dream_schedule',{hypothesis_id:hypothesis,description:'Independent bounded local length conversion',cost:1,risk:0,expected_information_gain:1,authorized:true});
 assert.equal((await call('butian_dream_start',{action_id:action.action_id})).status,'running');
 const output=execFileSync(python,['-c','print(125/100)'],{encoding:'utf8'}).trim();assert.equal(output,'1.25');
 const path=join(home,'calculation.txt');writeFileSync(path,`Input: 125 cm; output: ${output} m\n`);
 const evidence=await call('butian_evidence_record',{path});
 assert.equal((await call('butian_dream_verify',{hypothesis_id:hypothesis,action_id:action.action_id,outcome:'supported',evidence_refs:[evidence.evidence_id],new_memory_id:'conversion-result',new_content:'Independent Python calculation converted 125 cm to 1.25 m; input unit must be cm'})).status,'supported');
 const delivered=await call('butian_execution_submit',{task_id:setup.task_id,expected_revision:pkg.task_revision,problem_revision:pkg.problem_revision,submission_id:'dream-host-1',output:'Source conditions retained; bounded calculation supports conversion; no automatic promotion.',evidence_ids:[evidence.evidence_id]});
 assert.equal((await call('butian_task_review',{task_id:setup.task_id,revision:delivered.revision,verdict:'approve',reviewer_member_id:'reader'})).status,'done');
 console.log('PASS official SDK maintenance task, export, hash-checked recovery, distill, abstract, propose, authorized verification, real evidence and reviewed delivery');
 for(const [index,cm,divisor,outcome] of [[0,230,100,'success'],[1,340,100,'success'],[2,300,10,'failure']]){
  const goal=await call('butian_goal_submit',{goal:'Convert a length in centimeters using the learned method',problem:{acceptance_criteria:['A meter equals one hundred centimeters; preserve input unit']}});
  const work=await call('butian_execution_export',{task_id:goal.task_id,method_ids:['conversion-result']});
  assert(work.context.includes('input unit must be cm'));
  const measured=Number(execFileSync(python,['-c',`print(${cm}/${divisor})`],{encoding:'utf8'}).trim());
  const expected=cm/100;assert.equal(measured===expected,outcome==='success');
  const proof=join(home,`reuse-${index}.json`);writeFileSync(proof,JSON.stringify({centimeters:cm,observed_meters:measured,expected_meters:expected,divisor,controlled_fault:outcome==='failure'}));
  const ev=await call('butian_evidence_record',{path:proof});
  const result=await call('butian_execution_submit',{task_id:goal.task_id,expected_revision:work.task_revision,problem_revision:work.problem_revision,submission_id:`reuse-${index}`,output:JSON.stringify({measured,expected,outcome}),evidence_ids:[ev.evidence_id],method_uses:[{memory_id:'conversion-result',outcome,evidence_id:ev.evidence_id}]});
  assert.equal((await call('butian_task_review',{task_id:goal.task_id,revision:result.revision,verdict:'approve',reviewer_member_id:'reader'})).status,'done');
  execFileSync(python,['-c',`import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); m=s.memory('reader'); assert m.get('conversion-result')['status']==sys.argv[2]; m.close(); s.close()`,home,['candidate','warm','disputed'][index]],{cwd:root,env:{...process.env,BUTIAN_MAINTENANCE_ENABLED:'0'}});
 }
 console.log('PASS official SDK later tasks use learned content; two independently calculated successes warm memory; controlled counterevidence disputes it');
}finally{await c.close();}
execFileSync(python,['-c',`import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); assert s.dispatch_dream_tasks()==[]; m=s.memory('reader'); assert m.get('conversion-result')['status']=='disputed'; assert m.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='conversion-result'").fetchone()[0]==3; m.close(); s.close()`,home],{cwd:root,env:{...process.env,BUTIAN_MAINTENANCE_ENABLED:'0'}});
console.log('PASS restart preserves dispatch idempotency, disputed status and three observations; no model inference claimed');
console.log('ARTIFACT_HOME='+home);
