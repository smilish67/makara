const {Plugin, Notice, PluginSettingTab, Setting, Modal} = require('obsidian');
const fs = require('fs');
const http = require('http');
const crypto = require('crypto');
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
const protectedPath = path => typeof path === 'string' && (/^(me|projects)(\/|$)/.test(path));
const managed = path => /^(me|projects\/[A-Za-z0-9_-]+)\/(?:knowledge\/)?[A-Za-z0-9_-]+\.md$/.test(path);

module.exports = class Makara extends Plugin {
  async onload() {
    this.settings = Object.assign({tokenFile:'', port:18765}, await this.loadData());
    this.baselines = new Map();
    this.baselineBodies = new Map();
    this.saveQueues = new Map();
    const adapter = this.app.vault.adapter;
    const read = adapter.read.bind(adapter), write = adapter.write.bind(adapter);
    this.originalRead = adapter.read; this.originalWrite = adapter.write;
    this.wrappedRead = async (path) => {
      const text = await read(path);
      if (managed(path) && text.startsWith('<!-- makara\n') && !this.baselines.has(path)) {
        this.baselines.set(path, hash(text));
        this.baselineBodies.set(path,text.split('\n-->\n').slice(1).join('\n-->\n'));
      }
      return text;
    };
    const save = async (path, text, options) => {
      if (!managed(path)) return write(path,text,options);
      if (!text.startsWith('<!-- makara\n')) {
        await this.backup(path,text,write);
        throw new Error('Makara: canonical metadata is required; create new documents through a proposal');
      }
      if (!this.baselines.has(path)) {
        await this.backup(path,text,write);
        throw new Error('Makara: reload document to establish its revision before saving');
      }
      const separator = text.indexOf('\n-->\n');
      const scope = path.startsWith('me/') ? 'me' : path.split('/').slice(0,2).join('/');
      const key = path.slice(scope.length+1,-3);
      const body = text.slice(separator+5);
      try {
        const disk = await read(path);
        const diskBody = disk.split('\n-->\n').slice(1).join('\n-->\n');
        if (disk.startsWith('<!-- makara\n') && diskBody === body) {
          // Obsidian may autosave an unchanged body after an external refresh.
          this.baselines.set(path,hash(disk)); this.baselineBodies.set(path,body);
          return;
        }
        if (disk.startsWith('<!-- makara\n') && diskBody === this.baselineBodies.get(path)) this.baselines.set(path,hash(disk));
        const result = await this.call('manual-edit', {scope,key,body,base_version:this.baselines.get(path),request_id:crypto.randomUUID()});
        if (!result.version) throw new Error('Save not confirmed; reload after reviewing recovery copy');
        this.baselines.set(path,result.version);
        this.baselineBodies.set(path,body);
        // The service already wrote the canonical file. Never write the stale editor text over it.
      } catch (error) {
        await this.backup(path,text,write);
        new Notice('Makara: 저장 충돌/연결 실패. 복구 사본을 보존했습니다. 최신 문서와 비교하세요.',10000);
        throw error;
      }
    };
    this.wrappedWrite = (path,text,options) => {
      const previous=this.saveQueues.get(path)||Promise.resolve();
      const pending=previous.catch(()=>{}).then(()=>save(path,text,options));
      this.saveQueues.set(path,pending);
      pending.finally(()=>{if(this.saveQueues.get(path)===pending)this.saveQueues.delete(path);}).catch(()=>{});
      return pending;
    };
    adapter.read=this.wrappedRead; adapter.write=this.wrappedWrite;
    for (const file of (this.app.vault.getFiles?.() || [])) {
      if (managed(file.path)) await this.wrappedRead(file.path);
    }
    this.protectedMethods = {};
    for (const method of ['append','writeBinary','remove','rename','rmdir','trashSystem','trashLocal','copy']) {
      if (typeof adapter[method] !== 'function') continue;
      const original = adapter[method];
      const wrapped = async (...args) => {
        if (protectedPath(args[0]) || (['rename','copy'].includes(method) && protectedPath(args[1]))) throw new Error('Makara: use service proposals/deletion for canonical files');
        return original.apply(adapter,args);
      };
      this.protectedMethods[method] = {original,wrapped}; adapter[method] = wrapped;
    }
    this.addSettingTab(new Settings(this.app,this));
    this.addCommand({id:'review-proposals',name:'Review pending knowledge proposals',callback:async()=>{
      try {
        const status=await this.call('status',{});
        const items=[];
        for(const scope of status.scopes){for(const item of await this.call('proposals',{scope})){if(item.status==='pending')items.push(item);}}
        new Review(this.app,this,items).open();
      } catch(error){new Notice('Makara: '+error.message);}
    }});
    this.addCommand({id:'reload-revision',name:'Accept current disk revision after reviewing conflicts', callback:async()=>{
      const file=this.app.workspace.getActiveFile(); if(!file || !managed(file.path)) return;
      const text=await read(file.path); this.baselines.set(file.path,hash(text));
      this.baselineBodies.set(file.path,text.split('\n-->\n').slice(1).join('\n-->\n'));
      new Notice('Makara: 최신 디스크 버전을 기준으로 설정했습니다. 편집 내용과 비교한 뒤 저장하세요.');
    }});
  }
  async backup(path,text,write) {
    const dir='makara-conflicts';
    if(!await this.app.vault.adapter.exists(dir)) await this.app.vault.adapter.mkdir(dir);
    await write(`${dir}/${Date.now()}-${crypto.randomUUID()}-${path.replaceAll('/','_')}`,text);
  }
  call(action,data) {
    return new Promise((resolve,reject)=>{
      let token; try {token=fs.readFileSync(this.settings.tokenFile,'utf8').trim();} catch(error){reject(error);return;}
      const payload=JSON.stringify(data);
      const req=http.request({hostname:'127.0.0.1',port:this.settings.port,path:'/v1/'+action,method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json','Content-Length':Buffer.byteLength(payload)}},res=>{
        let output='';res.on('data',chunk=>output+=chunk);res.on('end',()=>{try{const value=JSON.parse(output);res.statusCode===200?resolve(value):reject(new Error(value.error));}catch(error){reject(error);}});
      });
      req.on('error',reject);req.setTimeout(15000,()=>req.destroy(new Error('Timeout')));req.end(payload);
    });
  }
  onunload(){const a=this.app.vault.adapter;if(a.read===this.wrappedRead)a.read=this.originalRead;if(a.write===this.wrappedWrite)a.write=this.originalWrite;for(const [method,value] of Object.entries(this.protectedMethods||{})){if(a[method]===value.wrapped)a[method]=value.original;}}
};
class Settings extends PluginSettingTab {
  display(){this.containerEl.empty();new Setting(this.containerEl).setName('Owner token file').setDesc('Absolute local path; the token itself is never stored in this vault.').addText(t=>t.setValue(this.plugin.settings.tokenFile).onChange(async value=>{this.plugin.settings.tokenFile=value;await this.plugin.saveData(this.plugin.settings);}));}
}

class Review extends Modal {
  constructor(app,plugin,items){super(app);this.plugin=plugin;this.items=items;}
  onOpen(){
    this.contentEl.createEl('h2',{text:'Makara 변경 제안 검토'});
    if(!this.items.length)this.contentEl.createEl('p',{text:'검토할 제안이 없습니다.'});
    for(const item of this.items){
      const section=this.contentEl.createDiv();
      section.createEl('h3',{text:item.scope+'/'+item.key});
      section.createEl('p',{text:item.kind+' · '+item.confirmation+' · '+item.reason});
      section.createEl('pre',{text:item.body});
      const evidence=section.createEl('button',{text:'출처 확인'});
      evidence.onclick=async()=>{try{for(const id of item.sources){const source=await this.plugin.call('inspect-source',{id});section.createEl('pre',{text:source.recorded_at+'\n'+source.text});}}catch(error){new Notice(error.message);}};
      const approve=section.createEl('button',{text:'이 변경 승인'});
      approve.onclick=async()=>{try{await this.plugin.call('approve',{id:item.id,request_id:crypto.randomUUID()});section.remove();new Notice('Makara: 변경을 반영했습니다.');}catch(error){new Notice('Makara: '+error.message);}};
      const reject=section.createEl('button',{text:'반영하지 않기'});
      reject.onclick=async()=>{try{await this.plugin.call('reject',{id:item.id,reason:'Owner rejected in Obsidian',request_id:crypto.randomUUID()});section.remove();}catch(error){new Notice('Makara: '+error.message);}};
    }
  }
  onClose(){this.contentEl.empty();}
}
