const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const markup = fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const declaredIds = new Set([...markup.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]));
const key = 'careerpilot-draft-edits';
function storage() {
  const values = new Map();
  return {getItem:k=>values.get(k) ?? null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
}
function boot(saved) {
  const nodes = new Map();
  function element(id='') {
    const classes = new Set(), handlers = new Map();
    return {id,value:'',textContent:'',open:false,children:[],disabled:false,readOnly:false,
      classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),
        toggle:(c,hide)=>hide ? classes.add(c) : classes.delete(c)},
      addEventListener:(type,fn)=>handlers.set(type,fn),emit:type=>handlers.get(type)?.({preventDefault(){}}),
      append(...children){this.children.push(...children)},replaceChildren(...children){this.children=children},
      querySelector(){return get('profileSubmit')},querySelectorAll(){return []},close(){this.open=false},showModal(){this.open=true},
      setCustomValidity(){},reportValidity(){return true},setAttribute(){},remove(){},prepend(){}};
  }
  function get(id) {
    assert.ok(id==='profileSubmit' || declaredIds.has(id),'UI element missing from HTML: '+id);
    if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id);
  }
  const context = vm.createContext({
    document:{getElementById:get,querySelectorAll:()=>[],querySelector:()=>null,createElement:()=>element()},
    window:{addEventListener(){},history:{replaceState(){}}},
    sessionStorage:saved,localStorage:storage(),URL,URLSearchParams,AbortController,structuredClone,
    setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,
    fetch:()=>new Promise(()=>{}),
    FormData:class {constructor(){this.fields=[]}append(key,value){this.fields.push([key,value])}},
  });
  vm.runInContext(source,context);
  return {context,nodes:get,run:code=>vm.runInContext(code,context)};
}
const action = account => ({id:'draft-1',state:'proposed',account_available:true,result:{},
  payload:{mode:'live',source_kind:'email',account,to:'recruiter@example.invalid',subject:'Saved subject',body:'Saved body'}});

test('model setup clears the password after success and requires fresh processing approvals',async()=>{
  const saved=storage(),app=boot(saved);
  app.run("session={mode:'live',csrf:'csrf',account:null,model_configured:false}; syncControls();");
  app.nodes('modelApiKey').value='synthetic-secret'; app.nodes('modelChoice').value='openai/gpt-oss-120b';
  app.nodes('processingConsent').checked=true; app.nodes('profileProcessingConsent').checked=true;
  app.context.requestedConfig=[];
  app.run(`api=async(path,method,body)=>{
    if(path==='/model/setup'){requestedConfig.push(body);return {message:'Connection verified'};}
    return {csrf:'csrf',mode:'live',account:null,model_configured:true,model_provider:'api.groq.com',model_name:'openai/gpt-oss-120b',model_version:'new',email_processing_consent:false};
  };`);
  await app.nodes('modelSetupForm').emit('submit');
  assert.equal(app.context.requestedConfig[0].api_key,'synthetic-secret');
  assert.equal(app.nodes('modelApiKey').value,'');
  assert.equal(app.nodes('processingConsent').checked,false);
  assert.equal(app.nodes('profileProcessingConsent').checked,false);
  assert.equal(app.nodes('processingConsent').disabled,false);
  assert.equal(app.nodes('modelSetup').classList.contains('hidden'),false);
  assert.equal(saved.getItem('modelApiKey'),null);
  assert.ok(app.nodes('modelStatus').textContent.includes('gpt-oss-120b'));
});

test('failed model setup clears the password and restores a usable retry',async()=>{
  const app=boot(storage());
  app.run("session={mode:'live',csrf:'csrf',account:null,model_configured:false}; api=async()=>{throw Error('Key rejected');}; syncControls();");
  app.nodes('modelApiKey').value='synthetic-secret';
  await app.nodes('modelSetupForm').emit('submit');
  assert.equal(app.nodes('modelApiKey').value,'');
  assert.equal(app.nodes('saveModel').disabled,false);
  assert.ok(app.nodes('modelSetupStatus').textContent.includes('Previous configuration is retained'));
  assert.ok(app.nodes('error').textContent.includes('Key rejected'));
});

test('model setup password is cleared when the dialog closes',()=>{
  const app=boot(storage()); app.nodes('modelApiKey').value='synthetic-secret';
  app.nodes('modelHelpDialog').emit('close'); assert.equal(app.nodes('modelApiKey').value,'');
  assert.ok(markup.includes('id="modelApiKey" type="password"'));
});

test('another tab changing the configured model clears existing processing checks',async()=>{
  const app=boot(storage());
  app.run("session={mode:'live',csrf:'csrf',account:null,model_configured:true,model_version:'old',model_provider:'api.groq.com',model_name:'openai/gpt-oss-120b'};");
  app.nodes('processingConsent').checked=true; app.nodes('profileProcessingConsent').checked=true;
  app.run("api=async()=>({...session,model_version:'new'});");
  await app.run('refreshSession()');
  assert.equal(app.nodes('processingConsent').checked,false);
  assert.equal(app.nodes('profileProcessingConsent').checked,false);
});
function open(app,value,account='account-a@example.invalid') {
  app.context.fixtureAction=value;
  app.run(`session={account:${JSON.stringify(account)},compose:true,gmail_state:'connected'}; draft=fixtureAction; renderDraft({preserveEdits:true});`);
}
async function initializeSession(app,account) {
  app.context.testAccount=account;
  app.run(`api=async()=>({account:testAccount,csrf:'csrf',gmail_version:0,mode:'live',gmail_configured:true,model_configured:false});`);
  await app.run('refreshSession()');
}

test('full reload restores edited text for the same Gmail account',async()=>{
  const saved=storage(),first=boot(saved);
  open(first,action('account-a@example.invalid'));
  first.nodes('draftBody').value='Unsaved text survives reload';first.nodes('draftBody').emit('input');
  const reloaded=boot(saved);await initializeSession(reloaded,'account-a@example.invalid');
  open(reloaded,action('account-a@example.invalid'));
  assert.equal(reloaded.nodes('draftBody').value,'Unsaved text survives reload');
  assert.equal(reloaded.nodes('executeDraft').disabled,true);
});

test('switching accounts removes saved browser recovery data and hidden fields',async()=>{
  const saved=storage(),app=boot(saved);open(app,action('account-a@example.invalid'));
  app.nodes('draftBody').value='Private local edit';app.nodes('draftBody').emit('input');
  assert.ok(saved.getItem(key));
  app.run("session.csrf='csrf'; api=async()=>({account:'account-b@example.invalid',csrf:'csrf',gmail_version:1,mode:'live',gmail_configured:true});");
  await app.run('refreshSession()');
  assert.equal(saved.getItem(key),null);
  assert.equal(app.nodes('draftBody').value,'');
});

test('legacy mail with a missing account is still cleared on disconnect',()=>{
  const saved=storage(),app=boot(saved);open(app,action(''));
  app.nodes('draftBody').value='Private legacy edit';app.nodes('draftBody').emit('input');
  app.run('clearGmailViews()');
  assert.equal(saved.getItem(key),null);
  assert.equal(app.nodes('draftBody').value,'');
});

test('local-only draft edits recover without Gmail and discard removes recovery data',async()=>{
  const saved=storage(),first=boot(saved),local=action('');local.payload.source_kind='job';
  open(first,local,null);first.nodes('draftBody').value='Local edit';first.nodes('draftBody').emit('input');
  const reloaded=boot(saved);await initializeSession(reloaded,null);open(reloaded,local,null);
  assert.equal(reloaded.nodes('draftBody').value,'Local edit');
  await reloaded.nodes('discardDraftEdits').emit('click');
  assert.equal(reloaded.nodes('draftBody').value,'Saved body');
  assert.equal(saved.getItem(key),null);
});

test('unknown write shows the exact saved operation, retaining other edits separately',()=>{
  const saved=storage(),app=boot(saved),value=action('account-a@example.invalid');open(app,value);
  app.nodes('draftBody').value='Different unsaved content';app.nodes('draftBody').emit('input');
  value.state='unknown';app.context.fixtureAction=value;app.run('draft=fixtureAction; renderDraft({preserveEdits:true});');
  assert.equal(app.nodes('draftBody').value,'Saved body');
  assert.equal(app.nodes('draftBody').readOnly,true);
  assert.equal(app.nodes('executeDraft').disabled,true);
  assert.equal(app.nodes('viewDraftEdits').classList.contains('hidden'),false);
  assert.ok(saved.getItem(key).includes('Different unsaved content'));
});

test('synthetic draft recovery remains available without a real Gmail connection',async()=>{
  const saved=storage(),first=boot(saved),synthetic=action('demo@example.invalid');synthetic.payload.mode='synthetic';
  open(first,synthetic,null);first.nodes('draftBody').value='Synthetic edits';first.nodes('draftBody').emit('input');
  const reloaded=boot(saved);await initializeSession(reloaded,null);open(reloaded,synthetic,null);
  assert.equal(reloaded.nodes('draftBody').value,'Synthetic edits');
  assert.ok(reloaded.nodes('draftMode').textContent.includes('SYNTHETIC SIMULATION'));
});

test('blocked browser storage keeps edits in memory with an actionable notice',()=>{
  const blocked={getItem(){throw Error('blocked')},setItem(){throw Error('blocked')},removeItem(){throw Error('blocked')}};
  const app=boot(blocked);open(app,action('account-a@example.invalid'));
  app.nodes('draftBody').value='Keep this text';app.nodes('draftBody').emit('input');
  app.run('renderDraft({preserveEdits:true});');
  assert.equal(app.nodes('draftBody').value,'Keep this text');
  assert.ok(app.nodes('draftStorageNote').textContent.includes('Save edits'));
});

test('multiline CV facts survive confirmation while edited fields remain editable',()=>{
  const app=boot(storage());
  app.context.extractedTestProfile={name:'Jane Doe',skills:[{name:'PLC Programming',level:'declared',evidence:'PLC Programming'}],
    target_roles:['Workshop Engineer'],locations:['Egypt'],remote:true,willing_to_learn:true,seniority:'entry',
    projects:['Graduation Project: USV\nA robot for collecting waste.'],
    experience:['Workshop Engineer 2026 – Present\nExample Garage\nDiagnosed electrical faults.'],
    education:['B.Sc. Engineering\nExample University'],languages:['Arabic (Native)','English (Fluent)'],
    achievements:['Research award 2025\nUniversity research forum','Safety certificate 2026']};
  app.run("loadProfileForm({id:'profile-test',profile:extractedTestProfile});");
  let collected=JSON.parse(app.run('JSON.stringify(collectProfile())'));
  assert.deepEqual(collected.experience,app.context.extractedTestProfile.experience);
  assert.deepEqual(collected.projects,app.context.extractedTestProfile.projects);
  assert.deepEqual(collected.achievements,app.context.extractedTestProfile.achievements);
  assert.equal(app.run('profileDirty()'),false);
  app.nodes('projects').value='Edited project\nSecond project';
  collected=JSON.parse(app.run('JSON.stringify(collectProfile())'));
  assert.deepEqual(collected.projects,['Edited project','Second project']);
  assert.deepEqual(collected.education,app.context.extractedTestProfile.education);
  assert.equal(app.run('profileDirty()'),true);
});

test('achievement facts are editable and old profiles default to an empty list',()=>{
  const app=boot(storage());
  app.run("loadProfileForm({id:'old',profile:{skills:[],target_roles:['Nurse'],locations:['Canada'],remote:true,seniority:'entry'}});");
  assert.equal(app.nodes('achievements').value,'');
  assert.deepEqual(JSON.parse(app.run('JSON.stringify(collectProfile().achievements)')),[]);
  app.nodes('achievements').value='Nursing innovation award\nPatient care scholarship';
  assert.deepEqual(JSON.parse(app.run('JSON.stringify(collectProfile().achievements)')),['Nursing innovation award','Patient care scholarship']);
  assert.equal(app.run('profileDirty()'),true);
});

test('zero coverage shows insufficient evidence and a recovery action for learning',()=>{
  const app=boot(storage());
  app.context.sampleScan={state:'succeeded',progress:'Scan complete',profile_current:true,mode:'live',result:{
    jobs:[{id:'job',title:'Nurse',company:'Test',mode:'live',location:'Canada',score:0,coverage:0,recommendation:'verify',matches:[],gaps:[],blockers:[],unknowns:['No supported required-skill list.']}],
    emails:[],actions:[],errors:[],market:{learning:[],sample_size:0},coverage:{gmail:{state:'disconnected'}}}};
  app.run('session={mode:"live",account:null}; renderScan(sampleScan);');
  assert.equal(app.nodes('jobs').children[0].children[3].textContent,'Insufficient evidence to score fit');
  assert.ok(app.nodes('market').children[0].textContent.includes('Review source coverage'));
});

test('disconnect clears private Gmail errors while keeping employer and model failures visible',()=>{
  const app=boot(storage());
  app.context.sampleScan={state:'partial',progress:'Complete',profile_current:true,mode:'live',result:{
    jobs:[],emails:[],actions:[],market:{},coverage:{gmail:{state:'read'}},gmail_account:'account-a@example.invalid',
    errors:['Employer unavailable','Model fallback used','Private mail error'],
    error_sources:[{source:'jobs',message:'Employer unavailable'},{source:'model',message:'Model fallback used'},{source:'gmail',message:'Private mail error'}]}};
  app.run('session={mode:"live",account:null}; currentScan=sampleScan; clearGmailViews();');
  assert.deepEqual(JSON.parse(app.run('JSON.stringify(currentScan.result.errors)')),['Employer unavailable','Model fallback used']);
  assert.ok(app.nodes('scanErrors').textContent.includes('Employer unavailable'));
  assert.ok(!app.nodes('scanErrors').textContent.includes('Private mail error'));
  assert.ok(!app.run('JSON.stringify(currentScan)').includes('account-a'));
});

test('consent becomes selectable only when prerequisites exist and explains every gate',()=>{
  const app=boot(storage());
  app.run("session={mode:'live',model_configured:false,account:null}; syncControls();");
  assert.equal(app.nodes('processingConsent').disabled,true);
  assert.equal(app.nodes('emailConsent').disabled,true);
  assert.ok(app.nodes('processingConsentHint').textContent.includes('No model is configured'));
  assert.ok(app.nodes('emailConsentHint').textContent.includes('connected Gmail account'));
  app.run("session.model_configured=true; syncControls();");
  assert.equal(app.nodes('processingConsent').disabled,false);
  assert.equal(app.nodes('emailConsent').disabled,true);
  assert.ok(app.nodes('emailConsentHint').textContent.includes('Connect Gmail first'));
  app.nodes('emailConsent').checked=false;
  app.run("session.account='test@example.invalid'; syncControls();");
  assert.equal(app.nodes('emailConsent').disabled,false);
  assert.equal(app.nodes('emailConsent').checked,false);
  app.run("session.mode='fixture'; syncControls();");
  assert.equal(app.nodes('processingConsent').disabled,true);
  assert.equal(app.nodes('emailConsent').disabled,true);
  assert.ok(app.nodes('processingConsentHint').textContent.includes('Synthetic demo'));
});

test('selected CV has immediate loading feedback and a persistent success label',async()=>{
  const app=boot(storage());let finish;
  app.context.testUpload=()=>new Promise(resolve=>{finish=resolve});
  app.run("session={mode:'live',model_configured:false}; api=()=>testUpload();");
  app.nodes('pdfFile').files=[{name:'Engineering CV.pdf',size:300189}];
  const pending=app.nodes('pdfFile').emit('change');
  assert.ok(app.nodes('cvFileStatus').textContent.includes('Engineering CV.pdf'));
  assert.ok(app.nodes('cvFileStatus').textContent.includes('extracting'));
  assert.equal(app.nodes('uploadPdf').textContent,'Extracting CV…');
  assert.equal(app.nodes('profileSubmit').disabled,true);
  finish({profile:{name:'Jane Doe',skills:[{name:'PLC Programming',level:'declared',evidence:'PLC Programming'}]},notice:'One declared skill extracted locally.'});
  await pending;
  assert.equal(app.nodes('profileName').value,'Jane Doe');
  assert.equal(app.nodes('skills').value,'PLC Programming');
  assert.ok(app.nodes('cvFileStatus').textContent.includes('CV loaded: Engineering CV.pdf'));
  assert.ok(app.nodes('cvFileStatus').textContent.includes('1 skills extracted'));
  assert.equal(app.nodes('pdfFile').value,'');
  assert.equal(app.nodes('uploadPdf').disabled,false);
  assert.equal(app.nodes('profileSubmit').disabled,false);
});

test('failed CV extraction names the file, preserves prior fields and restores retry controls',async()=>{
  const app=boot(storage());
  app.run("session={mode:'live',model_configured:false}; formProfile({name:'Saved Candidate',skills:[{name:'Python'}]}); api=async()=>{throw Error('Cannot parse this PDF. Paste its readable text.');};");
  app.nodes('pdfFile').files=[{name:'Unreadable CV.pdf',size:512}];
  await app.nodes('pdfFile').emit('change');
  assert.equal(app.nodes('profileName').value,'Saved Candidate');
  assert.equal(app.nodes('skills').value,'Python');
  assert.ok(app.nodes('cvFileStatus').textContent.includes('CV extraction failed: Unreadable CV.pdf'));
  assert.ok(app.nodes('cvFileStatus').textContent.includes('retained'));
  assert.ok(app.nodes('extractionNotice').textContent.includes('not applied'));
  assert.equal(app.nodes('uploadPdf').disabled,false);
  assert.equal(app.nodes('profileSubmit').disabled,false);
});

test('compound extracted skills retain evidence through confirmation and edits',()=>{
  const app=boot(storage());
  const skills=[{name:'Dialects (French, German)',level:'declared',evidence:'Dialects (French, German)'},
    {name:'Singing (Bari-Tenor, 7 Years)',level:'declared',evidence:'Singing (Bari-Tenor, 7 Years)'}];
  app.context.compoundProfile={skills,target_roles:['Actor'],locations:['Canada'],remote:true,seniority:'any'};
  app.run("loadProfileForm({id:'compound',profile:compoundProfile});");
  assert.deepEqual(JSON.parse(app.run('JSON.stringify(collectProfile().skills)')),skills);
  app.nodes('skills').value += ', Stage combat';
  const changed=JSON.parse(app.run('JSON.stringify(collectProfile().skills)'));
  assert.deepEqual(changed.slice(0,2),skills);
  assert.equal(changed[2].name,'Stage combat');
  assert.equal(changed[2].level,'user_confirmed');
});


test('successful draft remains successful when activity refresh fails',async()=>{
  const app=boot(storage());open(app,action('account-a@example.invalid'));
  app.context.savedSuccess={...action('account-a@example.invalid'),state:'succeeded',result:{verified:true,draft_id:'verified-draft'}};
  app.run(`api=async()=>savedSuccess; refreshSession=async()=>false;
    refreshActivity=async()=>{throw Error('Activity unavailable')}; refreshHistory=async()=>{};`);
  await assert.doesNotReject(app.run("draftOperation('/execute','POST')"));
  assert.ok(app.nodes('draftState').textContent.includes('succeeded'));
  assert.ok(app.nodes('draftHint').textContent.includes('workspace'));
  assert.equal(app.nodes('executeDraft').disabled,true);
  assert.equal(app.nodes('error').textContent,'');
});

test('lost execution response recovers a confirmed success without another mutation',async()=>{
  const app=boot(storage());open(app,action('account-a@example.invalid'));
  app.context.savedSuccess={...action('account-a@example.invalid'),state:'succeeded',result:{verified:true,draft_id:'verified-draft'}};
  app.context.mutationCount=0;
  app.run(`api=async(path,method='GET')=>{if(method==='POST'){mutationCount++;throw Error('Lost response')}return savedSuccess};
    refreshSession=async()=>false; refreshActivity=async()=>{}; refreshHistory=async()=>{};`);
  await assert.doesNotReject(app.run("draftOperation('/execute','POST')"));
  assert.equal(app.context.mutationCount,1);
  assert.ok(app.nodes('draftState').textContent.includes('succeeded'));
  assert.equal(app.nodes('error').textContent,'');
});


test('supporting history failure leaves a confirmed workspace usable',async()=>{
  const app=boot(storage());
  app.run(`session={mode:'fixture'};hasProfile=true;initialized=true;
    refreshSession=async()=>false;refreshProfile=async()=>{};
    refreshActivity=async()=>{throw Error('Activity unavailable')};refreshHistory=async()=>{};`);
  await assert.doesNotReject(app.run('refreshWorkspace()'));
  assert.equal(app.run('offline'),false);
  assert.equal(app.nodes('scanButton').disabled,false);
  assert.ok(app.nodes('workspaceHint').textContent.includes('Activity could not refresh'));
  assert.equal(app.nodes('retryStatus').classList.contains('hidden'),false);
});


test('completed scan stays terminal and usable when activity refresh fails',async()=>{
  const app=boot(storage());
  app.context.completedScan={id:'scan',state:'succeeded',progress:'Scan complete',profile_current:true,mode:'fixture',
    result:{jobs:[],emails:[],actions:[],errors:[],market:{sample_size:0,learning:[]},coverage:{gmail:{state:'synthetic'}}}};
  app.run(`session={mode:'fixture'};hasProfile=true;scanId='scan';
    refreshSession=async()=>false;api=async()=>completedScan;
    refreshActivity=async()=>{throw Error('Activity unavailable')};refreshHistory=async()=>{};`);
  await app.run('poll()');
  assert.equal(app.run('offline'),false);
  assert.equal(app.nodes('progress').textContent,'Scan complete');
  assert.equal(app.nodes('scanButton').disabled,false);
  assert.equal(app.nodes('error').textContent,'');
});


test('one recovery summary counts unique actions and preserves every history item',async()=>{
  const app=boot(storage());
  const one={...action('account-a@example.invalid'),state:'unknown',created:'2026-10-10T10:00:00Z'};
  const two={...one,id:'draft-2',state:'executing'};
  app.context.rows=[one,two,one];app.run('api=async()=>rows;');
  await app.run('refreshHistory()');
  const recovery=app.nodes('draftRecovery');
  assert.equal(recovery.children.length,1);
  assert.match(recovery.children[0].children[0].textContent,/2 drafts need attention: 1 uncertain, 1 pending/);
  assert.equal(recovery.children[0].children.filter(n=>n.textContent==='Open Draft history').length,1);
  assert.equal(app.nodes('draftHistory').children.length,2);
  app.context.rows=[{...one,state:'succeeded'},{...two,state:'succeeded'}];await app.run('refreshHistory()');
  assert.equal(recovery.children.length,0);assert.equal(recovery.classList.contains('hidden'),true);
});

test('recovery count retains unavailable operations without leaking their content',async()=>{
  const app=boot(storage());app.context.rows=[{...action(''),account_available:false,state:'unknown',created:'2026-10-10',payload:{},result:{}}];
  app.run('api=async()=>rows;');await app.run('refreshHistory()');
  assert.match(app.nodes('draftRecovery').children[0].children[0].textContent,/1 draft needs attention: 1 uncertain/);
  assert.ok(app.nodes('draftHistory').children[0].children.some(n=>n.textContent==='Draft from another Gmail connection'));
});

test('Gmail link opens a guarded local route in a new tab and is hidden for other accounts and synthetic mail',()=>{
  const app=boot(storage());app.run("session={account:'account-a@example.invalid'};currentScan={id:'scan-1',mode:'live',result:{gmail_account:session.account}};");
  const link=app.run("gmailLink({id:'thread/id',mode:'live',reply_message_id:'<x@example.invalid>'})");
  assert.equal(link.href,'/api/scans/scan-1/emails/thread%2Fid/open');assert.equal(link.target,'_blank');
  assert.equal(app.run("gmailLink({id:'synthetic',mode:'synthetic'})"),null);
  app.run("session.account='different@example.invalid'");assert.equal(app.run("gmailLink({id:'real',mode:'live'})"),null);
});

test('undo tracking sends the expected state, prevents concurrent clicks and honors a lost response',async()=>{
  const app=boot(storage());let posts=0,release;
  const gate=new Promise(r=>release=r);app.context.gate=gate;app.context.post=()=>posts++;
  app.context.job={id:'tracked',application_status:{state:'applied_by_user',previous_state:'saved'}};
  app.run("currentScan={id:'scan',result:{jobs:[job]}};renderScan=()=>{};refreshSupportingViews=async()=>{};api=async(path,method,body)=>{if(method==='POST'){post();await gate;throw Error('lost response');}if(path.startsWith('/opportunities/status/'))return {state:'saved'};return currentScan;};");
  const pending=app.run("changeApplicationStatus(job,'undo_applied')");await app.run("changeApplicationStatus(job,'undo_applied')");release();await pending;
  assert.equal(posts,1);assert.equal(app.context.job.application_status.state,'saved');assert.equal(app.run('busy.size'),0);
});

test('saved application state survives a failed scan refresh without making the workspace offline',async()=>{
  const app=boot(storage());app.context.job={id:'job',title:'Role',blockers:[],recommendation:'verify',matches:[],unknowns:[],score:80};
  app.run("currentScan={id:'scan',result:{jobs:[job],actions:[]}};renderScan=()=>{};api=async(path,method)=>{if(method==='POST')return {state:'applied_by_user',previous_state:'untracked'};throw Error('refresh unavailable');};");
  await app.run("changeApplicationStatus(job,'applied_by_user')");assert.equal(app.context.job.application_status.state,'applied_by_user');assert.equal(app.run('offline'),false);
  assert.match(app.nodes('workspaceHint').textContent,/Application tracking saved/);
});



test('question sources use readable titles and clear with account-bound views',()=>{
  const app=boot(storage());app.run("currentScan={mode:'fixture',result:{jobs:[{id:'opaque-hash',title:'AI Engineer',company:'Example Employer'}],emails:[]}};renderAnswer({answer:'Supported result',source_ids:['opaque-hash','opaque-hash']});");
  assert.equal(app.nodes('answer').textContent,'Supported result');assert.equal(app.nodes('answerSources').children.length,1);
  assert.equal(app.nodes('answerSources').children[0].children[0].textContent,'AI Engineer · Example Employer');
  app.run('clearGmailViews()');assert.equal(app.nodes('answerSources').children.length,0);
});

test('follow-up questions carry current permission and prior citations then reset with workspace context',async()=>{
  const app=boot(storage());app.context.calls=[];
  app.run("session={mode:'live',model_configured:true};scanId='scan';currentScan={mode:'live',profile_current:true,state:'succeeded',result:{jobs:[],emails:[{id:'mail',subject:'Controlled request'}]}}; api=async(path,method,body)=>{calls.push(body);return {answer:'Supported answer',source_ids:['mail']};};");
  app.nodes('processingConsent').checked=true;
  app.nodes('question').value='Which recruiter needs a reply?';await app.nodes('questionForm').emit('submit');
  app.nodes('question').value='What did they ask for?';await app.nodes('questionForm').emit('submit');
  assert.equal(app.context.calls[1].processing_consent,true);
  assert.equal(app.context.calls[1].previous_question,'Which recruiter needs a reply?');
  assert.deepEqual(Array.from(app.context.calls[1].previous_source_ids),['mail']);
  app.run('clearAnswer()');app.nodes('question').value='Fresh question';await app.nodes('questionForm').emit('submit');
  assert.equal(app.context.calls[2].previous_question,'');
});

test('revoking model processing clears follow-up context and informs the server',async()=>{
  const app=boot(storage());app.context.calls=[];
  app.run("questionContext={question:'Earlier question',source_ids:['mail']};api=async(path,method,body)=>calls.push({path,body});");
  app.nodes('processingConsent').checked=false;await app.nodes('processingConsent').emit('change');
  assert.equal(app.context.calls[0].path,'/processing/consent');
  assert.equal(app.context.calls[0].body.enabled,false);
  assert.equal(app.run('questionContext.question'),'');
});

test('complete CV source, summary and activities survive review and confirmation',()=>{
  const app=boot(storage());app.context.cv={name:'Independent Candidate',skills:[{name:'Bookkeeping',evidence:'Bookkeeping',level:'declared'}],summary:['Declared accounting background.'],activities:['Volunteer treasurer\nAnnual club records.'],projects:[],experience:[],education:[],languages:[],achievements:[],source_text:'Complete source\nUnmapped activity.',unmapped_text:['Unmapped activity.'],extraction_method:'model_source_ranges',extraction_notes:['Literal nearby source retained.']};
  app.run('formProfile(cv)');const collected=app.run('collectProfile()');
  assert.equal(collected.source_text,app.context.cv.source_text);
  assert.deepEqual(Array.from(collected.activities),app.context.cv.activities);
  assert.deepEqual(Array.from(collected.summary),app.context.cv.summary);
  assert.deepEqual(Array.from(collected.unmapped_text),app.context.cv.unmapped_text);
});

test('starting a scan during an older in-flight poll schedules the new scan status',async()=>{
  const app=boot(storage());let release;
  app.context.gate=new Promise(r=>release=r);app.context.scheduled=[];
  app.context.setTimeout=(fn)=>{app.context.scheduled.push(fn);return app.context.scheduled.length;};
  app.run("scanId='old';hasProfile=true;session={mode:'fixture'};currentScan={id:'old',profile_current:true,state:'succeeded',result:{jobs:[],emails:[]}};refreshSession=async()=>false;refreshSupportingViews=async()=>{};renderScan=value=>{currentScan=value};api=async(path,method)=>{if(method==='POST')return {id:'new',state:'queued'};if(path==='/scans/old'){await gate;return {id:'old',state:'succeeded'}};return {id:'new',state:'succeeded',profile_current:true,result:{jobs:[],emails:[]}};};");
  const oldPoll=app.run('poll()');await Promise.resolve();await app.run('startScan()');
  release();await oldPoll;
  assert.equal(app.context.scheduled.length,1);
  await app.context.scheduled[0]();
  assert.equal(app.run('currentScan.id'),'new');assert.equal(app.run('currentScan.state'),'succeeded');
  assert.equal(app.nodes('scanButton').disabled,false);
});

test('editing question text during a slow answer keeps the actual answered question in follow-up context',async()=>{
  const app=boot(storage());let release;
  app.context.gate=new Promise(r=>release=r);
  app.run("session={mode:'live',model_configured:true};scanId='scan';currentScan={mode:'live',profile_current:true,state:'succeeded',result:{jobs:[],emails:[]}};api=async()=>{await gate;return {answer:'First supported answer',source_ids:[]};};");
  app.nodes('processingConsent').checked=true;app.nodes('answer').textContent='Older answer';
  app.nodes('question').value='First question';const pending=app.nodes('questionForm').emit('submit');
  await Promise.resolve();assert.equal(app.nodes('answer').textContent,'');
  app.nodes('question').value='Different draft question';release();await pending;
  assert.equal(app.run('questionContext.question'),'First question');
  assert.match(app.nodes('answer').textContent,/Answer to: First question/);
});
