const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const crypto=require('node:crypto');
const hash=s=>crypto.createHash('sha256').update(s).digest('hex');
const metadata='<!-- makara\n{"id":"test"}\n-->\n';
const files=new Map([['me/now.md',metadata+'Old body']]);
let writes=0,callCount=0;
const adapter={read:async p=>files.get(p),write:async(p,s)=>{files.set(p,s);writes++;},exists:async()=>true,mkdir:async()=>{},remove:async p=>files.delete(p),rename:async()=>{},append:async()=>{},writeBinary:async()=>{}};
class Plugin{async loadData(){return {};}addSettingTab(){}addCommand(){} }
const context={module:{exports:{}},require:name=>name==='obsidian'?{Plugin,Notice:class{},PluginSettingTab:class{},Setting:class{},Modal:class{}}:require(name)};
vm.runInNewContext(fs.readFileSync('obsidian-plugin/main.js','utf8'),context);
(async()=>{
 const plugin=new context.module.exports();plugin.app={vault:{adapter},workspace:{}};
 await plugin.onload();
 await adapter.read('me/now.md');
 plugin.call=async(action,data)=>{
  callCount++;assert.equal(action,'manual-edit');
  if(data.base_version!==hash(files.get('me/now.md')))throw new Error('409 conflict');
  const canonical=metadata+data.body;files.set('me/now.md',canonical);return {version:hash(canonical)};
 };
 await adapter.write('me/now.md',metadata+'User edit');
 assert.equal(writes,0,'No direct canonical file write bypasses the service');
 assert.equal(files.get('me/now.md'),metadata+'User edit');
 files.set('me/now.md',metadata+'Concurrent agent edit');
 await assert.rejects(adapter.write('me/now.md',metadata+'Stale editor contents'));
 assert.equal(files.get('me/now.md'),metadata+'Concurrent agent edit');
 assert([...files.keys()].some(p=>p.startsWith('makara-conflicts/')));
 const before=callCount;
 await adapter.write('me/now.md',metadata+'Concurrent agent edit');
 assert.equal(callCount,before,'Unchanged autosave must not conflict or create a new revision');
 await Promise.all([adapter.write('me/now.md',metadata+'Edit one'),adapter.write('me/now.md',metadata+'Edit two')]);
 assert.equal(files.get('me/now.md'),metadata+'Edit two','Overlapping saves are serialized');
 await assert.rejects(adapter.write('me/now.md','Removed metadata'));
 await assert.rejects(adapter.remove('me/now.md'));
 await assert.rejects(adapter.append('me/now.md','bypass'));
 plugin.onunload();
 console.log('Obsidian save, conflict recovery, metadata and mutation guards passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
