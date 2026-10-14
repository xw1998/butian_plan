import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdirSync,mkdtempSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const python=process.env.BUTIAN_TEST_PYTHON;
assert(python,'BUTIAN_TEST_PYTHON required');
mkdirSync(join(root,'.agent_tmp'),{recursive:true});
const home=mkdtempSync(join(root,'.agent_tmp','official-team-'));
const token=randomUUID();
execFileSync(python,['-c',`import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); [(s.add_member(m,m),s.store.set_member_token(m,sys.argv[2]+m)) for m in ('dispatcher','cm','mm')]; s.close()`,home,token],{cwd:root});
async function connect(){
 const client=new Client({name:'official-sdk-team-acceptance',version:'1.0.0'});
 await client.connect(new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env:{...process.env,BUTIAN_HOME:home},stderr:'pipe'}));
 return client;
}
async function call(client,member,name,args={}){
 const r=await client.callTool({name,arguments:{actor_member_id:member,token:token+member,...args}});
 assert.equal(r.isError,false,JSON.stringify(r));
 return JSON.parse(r.content.find(c=>c.type==='text').text);
}
let client=await connect(); let rootId;
try{
 const pending=await call(client,'dispatcher','butian_goal_submit',{goal:'Clarify measurement acceptance before planning',mode:'team',member_ids:['cm']});
 assert.equal(pending.status,'needs_clarification');
 await call(client,'dispatcher','butian_problem_update',{task_id:pending.task_id,expected_revision:pending.problem.revision,changes:{acceptance_criteria:['Keep measured evidence']},reason:'User clarified acceptance'});
 await call(client,'dispatcher','butian_problem_resume',{task_id:pending.task_id,subtasks:['Measure centimeters and preserve evidence']});
 const pendingTasks=await call(client,'dispatcher','butian_task_list',{member_id:'dispatcher'});
 const pendingChildren=pendingTasks.tasks.filter(t=>t.parent_id===pending.task_id);
 assert.equal(pendingChildren.length,1);assert.equal(pendingChildren[0].assignee_id,'cm');
 assert.equal((await call(client,'cm','butian_execution_export',{task_id:pendingChildren[0].task_id})).status,'awaiting_external_execution');
 console.log('PASS official SDK clarification then host plan resume without provider');
 const goal=await call(client,'dispatcher','butian_goal_submit',{goal:'Convert independent centimeter and millimeter measurements to meters and compare',mode:'team',member_ids:['cm','mm'],subtasks:['Convert 125 centimeters to meters with measured evidence','Convert 1250 millimeters to meters with measured evidence'],problem:{acceptance_criteria:['Both independent conversions equal 1.25 meters; preserve original inputs and evidence']}});
 rootId=goal.task_id;
 assert.equal(goal.status,'awaiting_host');
 for(const child of goal.children){
  const member=child.member_id;
  const pkg=await call(client,member,'butian_execution_export',{task_id:child.task_id});
  const input=member==='cm'?125:1250; const divisor=member==='cm'?100:1000;
  const result=JSON.parse(execFileSync(python,['-c','import json,sys; print(json.dumps({"input":int(sys.argv[1]),"divisor":int(sys.argv[2]),"meters":int(sys.argv[1])/int(sys.argv[2])}))',String(input),String(divisor)],{encoding:'utf8'}));
  assert.equal(result.meters,1.25);
  const path=join(home,member+'-measurement.json');writeFileSync(path,JSON.stringify(result));
  const ev=await call(client,member,'butian_evidence_record',{member_id:member,path});
  const delivered=await call(client,member,'butian_execution_submit',{task_id:child.task_id,expected_revision:pkg.task_revision,problem_revision:pkg.problem_revision,submission_id:member+'-measured',output:JSON.stringify(result),evidence_ids:[ev.evidence_id]});
  const reviewed=await call(client,'dispatcher','butian_task_review',{member_id:'dispatcher',task_id:child.task_id,revision:delivered.revision,verdict:'approve',reviewer_member_id:'dispatcher'});
  assert.equal(reviewed.status,'done');
 }
 const revised=await call(client,'dispatcher','butian_problem_update',{task_id:rootId,expected_revision:1,changes:{goal:'Convert NEW independent centimeter and millimeter samples to meters',acceptance_criteria:['Both independent new conversions equal 2.3 meters; preserve original inputs and evidence']},reason:'Replace measured sample'});
 assert.equal(revised.revision,2);
 await assert.rejects(client.callTool({name:'butian_task_team_run',arguments:{actor_member_id:'dispatcher',token:token+'dispatcher',member_id:'dispatcher',task_id:rootId}}),/team_child_problem_stale/);
 await call(client,'dispatcher','butian_problem_resume',{task_id:rootId});
 const all=await call(client,'dispatcher','butian_task_list',{member_id:'dispatcher'});
 const current=all.tasks.filter(t=>t.parent_id===rootId&&!JSON.parse(typeof t.payload==='string'?t.payload:JSON.stringify(t.payload)).superseded_by_problem_revision);
 assert.equal(current.length,2);
 for(const child of current){
  const member=child.assignee_id;
  const pkg=await call(client,member,'butian_execution_export',{task_id:child.task_id});
  assert(pkg.context.includes('NEW'));
  const input=member==='cm'?230:2300; const divisor=member==='cm'?100:1000;
  const result=JSON.parse(execFileSync(python,['-c','import json,sys; print(json.dumps({"input":int(sys.argv[1]),"divisor":int(sys.argv[2]),"meters":int(sys.argv[1])/int(sys.argv[2])}))',String(input),String(divisor)],{encoding:'utf8'}));
  assert.equal(result.meters,2.3);
  const path=join(home,member+'-new-measurement.json');writeFileSync(path,JSON.stringify(result));
  const ev=await call(client,member,'butian_evidence_record',{member_id:member,path});
  const delivered=await call(client,member,'butian_execution_submit',{task_id:child.task_id,expected_revision:pkg.task_revision,problem_revision:pkg.problem_revision,submission_id:member+'-new-measured',output:JSON.stringify(result),evidence_ids:[ev.evidence_id]});
  assert.equal((await call(client,'dispatcher','butian_task_review',{member_id:'dispatcher',task_id:child.task_id,revision:delivered.revision,verdict:'approve',reviewer_member_id:'dispatcher'})).status,'done');
 }
 const aggregate=await call(client,'dispatcher','butian_task_team_run',{member_id:'dispatcher',task_id:rootId});
 assert.equal(aggregate.status,'awaiting_external_execution');
 assert.equal(aggregate.aggregate.length,2);
 assert(aggregate.aggregate.every(item=>item.evidence_ids.length===1));
 assert.equal(aggregate.children.length,2);
 for(const child of aggregate.children){
  const item=aggregate.aggregate.find(item=>item.task_id===child.task_id);
  assert.deepEqual(child.evidence_ids,item.evidence_ids,'child evidence must match aggregate');
 }
 const results=aggregate.aggregate.map(item=>JSON.parse(item.output));
 assert.equal(results[0].meters,results[1].meters);
 const pkg=await call(client,'dispatcher','butian_execution_export',{task_id:rootId});
 const summary={measurements:results,agreement:results[0].meters===results[1].meters,meters:results[0].meters};
 const path=join(home,'team-comparison.json');writeFileSync(path,JSON.stringify(summary));
 const ev=await call(client,'dispatcher','butian_evidence_record',{member_id:'dispatcher',path});
 const delivered=await call(client,'dispatcher','butian_execution_submit',{task_id:rootId,expected_revision:pkg.task_revision,problem_revision:pkg.problem_revision,submission_id:'team-comparison',output:JSON.stringify(summary),evidence_ids:[ev.evidence_id]});
 assert.equal((await call(client,'dispatcher','butian_task_review',{member_id:'dispatcher',task_id:rootId,revision:delivered.revision,verdict:'approve',reviewer_member_id:'dispatcher'})).status,'done');
 console.log('PASS official SDK no-provider team, real subprocess measurements, child reviews, evidence-preserving comparison and final review');
}finally{await client.close();}
client=await connect();
try{
 const tasks=await call(client,'dispatcher','butian_task_list',{member_id:'dispatcher'});
 assert.equal(tasks.tasks.find(task=>task.task_id===rootId).status,'done');
 console.log('PASS official SDK persisted team after restart');console.log('ARTIFACT_HOME='+home);
}finally{await client.close();}
