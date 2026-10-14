import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdirSync,mkdtempSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const python=process.env.BUTIAN_TEST_PYTHON;
assert(python);
mkdirSync(join(root,'.agent_tmp'),{recursive:true});
const home=mkdtempSync(join(root,'.agent_tmp','official-workflow-'));
const token=randomUUID();
const recipientToken=randomUUID();
const setup=`import sys,json
from pathlib import Path
from butian.runtime.service import ButianService
s=ButianService(sys.argv[1]);s.add_member('worker','SDK worker');s.store.set_member_token('worker',sys.argv[2]);s.add_member('recipient','Migration recipient');s.store.set_member_token('recipient',sys.argv[3])
m={'input_contract':{'type':'object'},'output_contract':{'type':'object'},'permissions':['execute'],'risk_level':'low','failure_conditions':['invalid input']}
t=s.propose_tool('worker','Increment','Add one to a measured number',reuse_lower=3,manual_cost_upper=10,call_cost_lower=1,build_cost_upper=1,maintenance_cost_upper=1,risk_cost_upper=1,manual_cost_lower=10,call_cost_upper=1,cost_unit='minute')['tool_id']
s.add_tool_version('worker',t,'import json,sys; x=json.load(sys.stdin); print(json.dumps({"value":x["value"]+1}))',m)
for i in range(4):
 session=s.store.create_session('solo','worker',None,{})
 task=s.store.create_task(session,'Measured increment','worker')
 run=s.execute_tool('worker',t,1,{'value':i},task_id=task,verification_only=True)
 assert run['expected_output']=={'value':i+1}
 p=Path(sys.argv[1])/('setup-'+str(i)+'.json');p.write_text(json.dumps({'input':i,'observed':run['expected_output'],'execution_id':run['execution_id']}),encoding='utf-8')
 eid=s.record_evidence(p,owner_member_id='worker',actor_member_id='worker')
 s.verify_tool('worker',t,1,'success',independent_key=str(i),task_id=task,environment={'case':i%2},evidence_ids=[eid],execution_id=run['execution_id'],input_hash=run['input_hash'],expected_output=run['expected_output'])
assert s.tools._tool(t)['status']=='active'
print(json.dumps({'tool_id':t}));s.close()
`;
let fixture;
try {
 fixture=JSON.parse(execFileSync(python,['-c',setup,home,token,recipientToken],{cwd:root,encoding:'utf8',env:{...process.env,BUTIAN_MAINTENANCE_ENABLED:'0'}}));
} catch(error) {
 throw new Error('Fixture preparation failed: '+String(error.stderr ?? '').slice(0,1000));
}
const auth={member_id:'worker',actor_member_id:'worker',token};
async function connect(){
 const client=new Client({name:'official-workflow-acceptance',version:'1.0.0'});
 await client.connect(new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env:{...process.env,BUTIAN_HOME:home,BUTIAN_MAINTENANCE_ENABLED:'0'},stderr:'pipe'}));
 return client;
}
async function call(client,name,args={}){
 const result=await client.callTool({name,arguments:{...auth,...args}});
 assert.equal(result.isError,false,JSON.stringify(result));
 return JSON.parse(result.content.find(x=>x.type==='text').text);
}
let client=await connect();
let workflow;
try{
 const bundle=await call(client,'butian_tool_bundle_export',{tool_id:fixture.tool_id});
 assert.equal(bundle.versions.length,1);
 assert(!JSON.stringify(bundle).includes('source_path'));
 const imported=await call(client,'butian_tool_bundle_import',{member_id:'recipient',actor_member_id:'recipient',token:recipientToken,bundle,license:'MIT',scan_status:'passed'});
 assert.equal(imported.status,'draft');assert.equal(imported.requires_reverification,true);
 const manifest={input_contract:{type:'object'},output_contract:{type:'object'},permissions:['execute'],failure_conditions:['step_failure'],timeout_seconds:10,max_output_bytes:65536,steps:[{tool_id:fixture.tool_id,version:1},{tool_id:fixture.tool_id,version:1}]};
 workflow=await call(client,'butian_tool_workflow_create',{name:'Measured double increment',manifest});
 assert.equal(workflow.status,'candidate');
 for(let i=0;i<2;i++){
  const goal=await call(client,'butian_goal_submit',{goal:'Increment '+i+' twice',problem:{acceptance_criteria:['Result equals '+(i+2)]}});
  const run=await call(client,'butian_tool_workflow_execute',{workflow_id:workflow.workflow_id,version:1,input:{value:i},task_id:goal.task_id,verification_only:true});
  assert.equal(run.status,'success');assert.deepEqual(run.output,{value:i+2});assert.equal(run.steps.length,2);
  const path=join(home,'workflow-proof-'+i+'.json');
  writeFileSync(path,JSON.stringify({input:i,output:run.output,run_id:run.run_id}));
  const evidence=await call(client,'butian_evidence_record',{path});
  const verified=await call(client,'butian_tool_workflow_verify',{workflow_id:workflow.workflow_id,version:1,run_id:run.run_id,evidence_ids:[evidence.evidence_id],expected_output:run.output});
  assert.equal(verified.status,i===0?'candidate':'active');
 }
 const goal=await call(client,'butian_goal_submit',{goal:'Production double increment',problem:{acceptance_criteria:['Result equals 12']}});
 const production=await call(client,'butian_tool_workflow_execute',{workflow_id:workflow.workflow_id,version:1,input:{value:10},task_id:goal.task_id});
 assert.equal(production.status,'success');assert.deepEqual(production.output,{value:12});
 const retired=await call(client,'butian_tool_workflow_retire',{workflow_id:workflow.workflow_id,version:1,reason:'Completed acceptance composition'});
 assert.equal(retired.status,'retired');
 console.log('PASS official SDK source-only migration, real two-step execution, independent composition admission, production output and audited retirement');
}finally{await client.close();}
execFileSync(python,['-c',"import sys;from butian.runtime.service import ButianService;s=ButianService(sys.argv[1]);assert s.tool_workflows.get('worker',sys.argv[2],1)['status']=='retired';s.close()",home,workflow.workflow_id],{cwd:root});
console.log('PASS persisted retired composition after restart');
console.log('ARTIFACT_HOME='+home);
