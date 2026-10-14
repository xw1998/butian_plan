import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const python=process.env.BUTIAN_TEST_PYTHON;assert(python);
const home=mkdtempSync(join(root,'.agent_tmp','official-extraction-'));const token=randomUUID();
const setup=JSON.parse(execFileSync(python,['-c',`import sys,json,hashlib; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); s.add_member('reader','Reader'); s.store.set_member_token('reader',sys.argv[2]); raw=b'Measured pressure was 2 Pa under room temperature.'; source=s.learning.add_source('manual','https://example.org/measured','Measured pressure',hashlib.sha256(raw).hexdigest(),owner_member_id='reader'); s.learning.save_source_snapshot(source,raw,owner_member_id='reader'); s.learning.verify_source_content(source,raw,owner_member_id='reader'); task=s.dispatch_learning_extractions()[0]['task_id']; print(json.dumps({'source':source,'task':task})); s.close()`,home,token],{cwd:root,encoding:'utf8'}));
async function connect(){const c=new Client({name:'official-extraction-acceptance',version:'1.0.0'});await c.connect(new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env:{...process.env,BUTIAN_HOME:home},stderr:'pipe'}));return c;}
async function call(c,name,args={}){const r=await c.callTool({name,arguments:{actor_member_id:'reader',member_id:'reader',token,...args}});assert.equal(r.isError,false);return JSON.parse(r.content.find(x=>x.type==='text').text);}
let c=await connect();
try{
 const original=await call(c,'butian_knowledge_source_read',{source_id:setup.source});
 const pkg=await call(c,'butian_execution_export',{task_id:setup.task});
 const claim={claim:'Measured pressure was 2 Pa',quote:original.content,conditions:['room temperature'],limitations:['single measured source; needs independent practice'],predictions:['repeat measurement reads 2 Pa'],falsifiers:['repeat measurement differs from 2 Pa']};
 const output=JSON.stringify({claims:[claim]});const path=join(home,'extraction-result.json');writeFileSync(path,output);
 const evidence=await call(c,'butian_evidence_record',{path});
 const request={task_id:setup.task,expected_revision:pkg.task_revision,problem_revision:pkg.problem_revision,submission_id:'source-extract-1',output,evidence_ids:[evidence.evidence_id]};
 await assert.rejects(call(c,'butian_execution_submit',{...request,output:JSON.stringify({claims:[{...claim,quote:'invented quotation'}]})}),/extraction_quote_not_in_source/);
 const delivered=await call(c,'butian_execution_submit',request);
 assert.deepEqual(await call(c,'butian_execution_submit',request),delivered);
 assert.equal((await call(c,'butian_task_review',{task_id:setup.task,revision:delivered.revision,verdict:'approve',reviewer_member_id:'reader'})).status,'done');
 console.log('PASS official SDK original read, fabricated quote rejection, delivery and approval');
}finally{await c.close();}
c=await connect();try{const tasks=await call(c,'butian_task_list');assert.equal(tasks.tasks.find(t=>t.task_id===setup.task).status,'done');}finally{await c.close();}
const verified=JSON.parse(execFileSync(python,['-c',`import sys,json; from butian.runtime.service import ButianService; from butian.runtime.extraction import ingest_reviewed_extractions; s=ButianService(sys.argv[1]); assert ingest_reviewed_extractions(s)==[]; claims=s.learning.claims_for_source(sys.argv[2],owner_member_id='reader'); assert len(claims)==1 and claims[0]['status']=='candidate' and claims[0]['confidence']==0; print(json.dumps({'claims':len(claims)})); s.close()`,home,setup.source],{cwd:root,encoding:'utf8'}));assert.equal(verified.claims,1);
console.log('PASS reviewed candidate persists once after standard SDK restart; no model inference claimed');console.log('ARTIFACT_HOME='+home);
