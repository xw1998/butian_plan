import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,writeFileSync,copyFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const python=process.env.BUTIAN_TEST_PYTHON;assert(python);
const home=mkdtempSync(join(root,'.agent_tmp','official-dream-'));const token=randomUUID();
execFileSync(python,['-c',`import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); s.add_member('reader','Reader'); s.add_member('other','Other'); s.store.set_member_token('reader',sys.argv[2]); m=s.memory('reader'); m.put('cm','lesson','Centimeters convert to meters by dividing by 100',status='candidate'); m.put('mm','lesson','Millimeters convert to meters by dividing by 1000',status='candidate'); m.close(); s.close()`,home,token],{cwd:root});
async function connect(){const c=new Client({name:'official-dream-acceptance',version:'1.0.0'});await c.connect(new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env:{...process.env,BUTIAN_HOME:home},stderr:'pipe'}));return c;}
async function call(c,name,args={}){const r=await c.callTool({name,arguments:{actor_member_id:'reader',member_id:'reader',token,...args}});assert.equal(r.isError,false);return JSON.parse(r.content.find(x=>x.type==='text').text);}
let c=await connect();let hypothesis,actions;
try{
 const distilled=await call(c,'butian_dream_distill',{memory_ids:['cm','mm'],summary:'Check length units before conversion'});
 hypothesis=await call(c,'butian_dream_propose',{candidate_id:distilled.distillation_id,statement:'125 centimeters equals 1.25 meters',prior_reliability:0.5,argument:'Validate the conversion using an independent calculation'});
 if(typeof hypothesis==='object')hypothesis=hypothesis.hypothesis_id;
 actions=[];
 for(const description of ['Measure 125 cm','Measure 230 cm']){const action=await call(c,'butian_dream_schedule',{hypothesis_id:hypothesis,description,cost:1,risk:0,expected_information_gain:1,authorized:true});actions.push(action.action_id);}
 await assert.rejects(call(c,'butian_dream_verify',{hypothesis_id:hypothesis,action_id:actions[0],outcome:'supported',evidence_refs:[]}),/verification_running_action_required/);
 for(const action_id of actions)assert.equal((await call(c,'butian_dream_start',{action_id})).status,'running');
 const output=execFileSync(python,['-c','print(125 / 100)'],{encoding:'utf8'}).trim();assert.equal(output,'1.25');
 const path=join(home,'measurement.txt');writeFileSync(path,`125 centimeters converted to meters: ${output}\n`);
 const evidence=await call(c,'butian_evidence_record',{path});
 await assert.rejects(call(c,'butian_dream_verify',{hypothesis_id:hypothesis,outcome:'supported',evidence_refs:[evidence.evidence_id]}),/verification_action_ambiguous/);
 await assert.rejects(call(c,'butian_dream_verify',{member_id:'other',hypothesis_id:hypothesis,action_id:actions[0],outcome:'supported',evidence_refs:[evidence.evidence_id]}));
 assert.equal((await call(c,'butian_dream_verify',{hypothesis_id:hypothesis,action_id:actions[0],outcome:'supported',evidence_refs:[evidence.evidence_id],new_memory_id:'validated-conversion',new_content:'125 cm converted to 1.25 m in independent Python calculation'})).status,'supported');
 await assert.rejects(call(c,'butian_dream_verify',{hypothesis_id:hypothesis,action_id:actions[0],outcome:'supported',evidence_refs:[evidence.evidence_id]}),/verification_running_action_required/);
 console.log('PASS official SDK authorized dream start, real calculation evidence, exact action completion and isolation');
}finally{await c.close();}
execFileSync(python,['-c',`import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); m=s.memory('reader'); m.db.execute("UPDATE dream_verification_evidence SET fingerprint='legacy-path-fingerprint'"); m.close(); s.close()`,home],{cwd:root});
c=await connect();try{
 const history=await call(c,'butian_dream_history');assert(history.hypotheses.some(x=>x.hypothesis_id===hypothesis&&x.status==='supported'));
 const registeredAgain=await call(c,'butian_evidence_record',{path:join(home,'measurement.txt')});
 await assert.rejects(call(c,'butian_dream_verify',{hypothesis_id:hypothesis,action_id:actions[1],outcome:'supported',evidence_refs:[registeredAgain.evidence_id]}),/verification_evidence_not_independent/);
 const copiedPath=join(home,'measurement-copy.txt');copyFileSync(join(home,'measurement.txt'),copiedPath);
 const copied=await call(c,'butian_evidence_record',{path:copiedPath});
 await assert.rejects(call(c,'butian_dream_verify',{hypothesis_id:hypothesis,action_id:actions[1],outcome:'supported',evidence_refs:[copied.evidence_id]}),/verification_evidence_not_independent/);
 console.log('PASS official SDK restart rejects re-registered and copied identical evidence as independent verification');
}finally{await c.close();}
const persisted=JSON.parse(execFileSync(python,['-c',`import sys,json; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); m=s.memory('reader'); print(json.dumps({'actions':[r[0] for r in m.db.execute('SELECT status FROM verification_actions ORDER BY created_at')], 'memory':m.get('validated-conversion')})); m.close(); s.close()`,home],{cwd:root,encoding:'utf8'}));
assert.deepEqual(persisted.actions,['completed','running']);assert.equal(persisted.memory.status,'candidate');
console.log('PASS official SDK restart preserves exact action and candidate memory; no model inference claimed');console.log('ARTIFACT_HOME='+home);
