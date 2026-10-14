import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const python=process.env.BUTIAN_TEST_PYTHON;assert(python);
const home=mkdtempSync(join(root,'.agent_tmp','official-queue-'));
const token=randomUUID();
const env={...process.env,BUTIAN_HOME:home,BUTIAN_MAINTENANCE_ENABLED:'0'};
execFileSync(python,['-c',"import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); [(s.add_member(m,m),s.store.set_member_token(m,sys.argv[2]+m)) for m in ('reader','other')]; s.close()",home,token],{cwd:root,env});
async function connect(){const c=new Client({name:'official-queue-acceptance',version:'1.0.0'});await c.connect(new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env,stderr:'pipe'}));return c;}
async function call(c,name,args={}){const r=await c.callTool({name,arguments:{actor_member_id:'reader',member_id:'reader',token:token+'reader',...args}});assert.equal(r.isError,false);return JSON.parse(r.content.find(x=>x.type==='text').text);}
let c=await connect();let id;
try{
 const tools=(await c.listTools()).tools;
 for(const name of ['butian_knowledge_source_enqueue','butian_knowledge_source_queue']){
  const schema=tools.find(t=>t.name===name).inputSchema;
  assert(schema.required.includes('actor_member_id')&&schema.required.includes('token'));
 }
 id=(await call(c,'butian_knowledge_source_enqueue',{url:'https://docs.python.org/3/library/json.html'})).queue_id;
 assert.equal((await call(c,'butian_knowledge_source_enqueue',{url:'https://docs.python.org/3/library/json.html'})).queue_id,id);
 await assert.rejects(call(c,'butian_knowledge_source_queue',{member_id:'other'}));
 assert.equal((await call(c,'butian_knowledge_source_queue')).queue[0].owner_member_id,'reader');
}finally{await c.close();}
c=await connect();
try{assert.equal((await call(c,'butian_knowledge_source_queue')).queue[0].queue_id,id);console.log('PASS official SDK authenticated URL queue, duplicate protection, isolation and restart');console.log('ARTIFACT_HOME='+home);}finally{await c.close();}
