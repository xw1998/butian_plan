import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {execFileSync} from 'node:child_process';
import {mkdirSync,mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const python=process.env.BUTIAN_TEST_PYTHON;assert(python);
mkdirSync(join(root,'.agent_tmp'),{recursive:true});
const home=mkdtempSync(join(root,'.agent_tmp','official-source-'));const token=randomUUID();
execFileSync(python,['-c',"import sys; from butian.runtime.service import ButianService; s=ButianService(sys.argv[1]); [(s.add_member(m,m),s.store.set_member_token(m,sys.argv[2]+m)) for m in ('reader','other')]; s.close()",home,token],{cwd:root});
async function connect(){const c=new Client({name:'official-source-acceptance',version:'1.0.0'});await c.connect(new StdioClientTransport({command:python,args:['-m','butian.cli','mcp'],cwd:root,env:{...process.env,BUTIAN_HOME:home},stderr:'pipe'}));return c;}
async function call(c,name,args={},member='reader'){const r=await c.callTool({name,arguments:{actor_member_id:member,member_id:member,token:token+member,...args}});assert.equal(r.isError,false);return JSON.parse(r.content.find(x=>x.type==='text').text);}
let c=await connect();let source,first;
try{
 source=await call(c,'butian_knowledge_source_capture',{url:'https://docs.python.org/3/library/json.html',title:'Python official JSON documentation'});
 assert.equal(source.verification.status,'verified');
 first=await call(c,'butian_knowledge_source_read',{source_id:source.source_id,limit:12000});
 assert(first.content.includes('json'));assert.equal(first.sha256,source.sha256);assert(first.next_offset>0);
 const second=await call(c,'butian_knowledge_source_read',{source_id:source.source_id,offset:first.next_offset,limit:12000});assert.equal(second.offset,first.next_offset);assert(second.content.length>0);
 await assert.rejects(call(c,'butian_knowledge_source_read',{source_id:source.source_id},'other'),/knowledge_source_not_found/);
 console.log('PASS official SDK public source capture, verified original pagination and member isolation');
}finally{await c.close();}
c=await connect();
try{const recovered=await call(c,'butian_knowledge_source_read',{source_id:source.source_id,limit:12000});assert.equal(recovered.content,first.content);assert.equal(recovered.sha256,first.sha256);console.log('PASS original source snapshot after restart');console.log('ARTIFACT_HOME='+home);}finally{await c.close();}
