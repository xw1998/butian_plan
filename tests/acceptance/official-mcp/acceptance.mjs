import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdirSync,mkdtempSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../../../',import.meta.url));
mkdirSync(join(root,'.agent_tmp'),{recursive:true});
const python=process.env.BUTIAN_TEST_PYTHON;
assert(python, 'BUTIAN_TEST_PYTHON must specify the absolute Python executable');
const home=mkdtempSync(join(root,'.agent_tmp','official-host-'));
const token=randomUUID();
execFileSync(python,['-c',`import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); s.add_member('sdk-worker','SDK worker'); s.store.set_member_token('sdk-worker',sys.argv[2]); s.close()`,home,token],{cwd:root});
const auth={actor_member_id:'sdk-worker',token};
async function connect(){
 const client=new Client({name:'official-sdk-business-acceptance',version:'1.0.0'});
 const transport=new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env:{...process.env,BUTIAN_HOME:home},stderr:'pipe'});
 await client.connect(transport);
 return client;
}
async function call(client,name,args={}){
 const r=await client.callTool({name,arguments:{...auth,...args}});
 assert.equal(r.isError,false,JSON.stringify(r));
 return JSON.parse(r.content.find(c=>c.type==='text').text);
}
let client=await connect(); let taskId;
try{
 const tools=await client.listTools();
 assert(tools.tools.some(t=>t.name==='butian_context_recover'));
 for(const name of ['butian_tool_bundle_export','butian_tool_bundle_import','butian_tool_workflow_create','butian_tool_workflow_execute','butian_tool_workflow_verify']) {
  const tool=tools.tools.find(t=>t.name===name);
  assert(tool, 'Missing tool: '+name);
  for(const field of ['member_id','actor_member_id','token']) assert(tool.inputSchema.required.includes(field), name+' missing authentication field '+field);
 }
 const goal=await call(client,'butian_goal_submit',{goal:'Compute measured conversion from 125 cm to meters',problem:{acceptance_criteria:['Result must equal 1.25 meters with evidence']}});
 taskId=goal.task_id;
 const exported=await call(client,'butian_execution_export',{task_id:taskId});
 assert(exported.context.includes('1.25'));
 const evidencePath=join(home,'conversion.json');
 const measurement={input_centimeters:125,result_meters:125/100};
 assert.equal(measurement.result_meters,1.25);
 writeFileSync(evidencePath,JSON.stringify(measurement));
 const evidence=await call(client,'butian_evidence_record',{member_id:'sdk-worker',path:evidencePath});
 const delivered=await call(client,'butian_execution_submit',{task_id:taskId,expected_revision:exported.task_revision,problem_revision:exported.problem_revision,submission_id:'sdk-conversion-1',output:JSON.stringify(measurement),evidence_ids:[evidence.evidence_id]});
 assert.equal(delivered.status,'review');
 const recovered=await call(client,'butian_context_recover',{member_id:'sdk-worker',task_id:taskId,payload_field:'provider_output'});
 assert.deepEqual(JSON.parse(recovered.value),measurement);
 assert.match(recovered.sha256,/^[0-9a-f]{64}$/);
 const rework=await call(client,'butian_task_review',{member_id:'sdk-worker',task_id:taskId,revision:delivered.revision,verdict:'rework',reviewer_member_id:'sdk-worker',notes:'Repeat measurement and include the conversion divisor'});
 assert.equal(rework.rework_count,1);
 const revised=await call(client,'butian_execution_export',{task_id:taskId});
 assert(revised.task_revision>exported.task_revision);
 const repeated={input_centimeters:125,divisor:100,result_meters:125/100};
 assert.equal(repeated.result_meters,1.25);
 const repeatedPath=join(home,'conversion-repeated.json');
 writeFileSync(repeatedPath,JSON.stringify(repeated));
 const repeatedEvidence=await call(client,'butian_evidence_record',{member_id:'sdk-worker',path:repeatedPath});
 assert.notEqual(repeatedEvidence.evidence_id,evidence.evidence_id);
 const resubmitted=await call(client,'butian_execution_submit',{task_id:taskId,expected_revision:revised.task_revision,problem_revision:revised.problem_revision,submission_id:'sdk-conversion-2',output:JSON.stringify(repeated),evidence_ids:[repeatedEvidence.evidence_id]});
 assert.equal(resubmitted.status,'review');
 const revisedRecovery=await call(client,'butian_context_recover',{member_id:'sdk-worker',task_id:taskId,payload_field:'provider_output'});
 assert.deepEqual(JSON.parse(revisedRecovery.value),repeated);
 const review=await call(client,'butian_task_review',{member_id:'sdk-worker',task_id:taskId,revision:resubmitted.revision,verdict:'approve',reviewer_member_id:'sdk-worker'});
 assert.equal(review.status,'done');
 console.log('PASS official SDK tools discovery, production stdio, measured business result, evidence, recovery, rework, resubmission and review');
}finally{await client.close();}
client=await connect();
try{
 const tasks=await call(client,'butian_task_list',{member_id:'sdk-worker'});
 assert.equal(tasks.tasks.find(t=>t.task_id===taskId).status,'done');
 console.log('PASS official SDK restart and persisted reviewed task');
 console.log('ARTIFACT_HOME='+home);
}finally{await client.close();}
