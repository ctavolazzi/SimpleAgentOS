import {experiments} from './experiments.js';
import {insights} from './insights.js';
import {World,portrait} from './world.js';
const $=id=>document.getElementById(id);
const client=sessionStorage.getItem('fieldwork-client')||crypto.randomUUID();sessionStorage.setItem('fieldwork-client',client);
let state=null,wid=null,selected=null,editing=false,busy=false,polling=false,noticeTimer;
const world=new World($('world'),selectAgent),preview=new World($('preview'));
preview.set({agents:[{id:'sample',name:'Mira',x:142,y:126,color:'#cf734c'}]});
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
function fail(error){const dialog=document.querySelector('dialog[open]');if(dialog){let alert=dialog.querySelector('.dialog-error');if(!alert){alert=el('p',undefined,'dialog-error');alert.setAttribute('role','alert');dialog.prepend(alert);}alert.textContent=error.message||String(error);}$('error').hidden=false;$('error').textContent=error.message||String(error);}
function toast(text){$('toast').textContent=text;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$('toast').textContent='',3500);}
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Client':'1'},body:JSON.stringify({...body,client})});const data=await response.json();if(!response.ok)throw Error(data.error||'Request failed.');return data;}
async function task(fn){if(busy)return;busy=true;document.querySelectorAll('.dialog-error').forEach(n=>n.remove());$('error').hidden=true;try{await fn();}catch(error){fail(error);}finally{busy=false;}}
async function overview(){const data=await api('/api/workspaces');$('saved-list').replaceChildren();if(!data.workspaces.length)$('saved-list').append(el('div','No saved workspaces yet. Start with a small field study.','empty-saves'));for(const s of data.workspaces){const b=el('button',undefined,'save-card');b.dataset.workspaceId=s.id;b.append(el('strong',s.name),el('small',`${s.agents} agent${s.agents===1?'':'s'} · Tick ${s.tick}`),el('small',`Saved ${new Date(s.updated*1000).toLocaleString()}`));b.onclick=()=>task(()=>open(s.id));$('saved-list').append(b);}return data;}
async function open(id){state=await api('/api/open',{id});wid=id;selected=null;location.hash='workspace='+encodeURIComponent(id);$('home').hidden=true;$('workspace').hidden=false;render();if(state.profile.tutorial==='new')$('guide-dialog').showModal();}
async function refresh(){if(!wid)return;const requested=wid;if(state?.controller && state?.running)await api('/api/heartbeat',{id:wid});const next=await api('/api/state?id='+encodeURIComponent(wid)+'&client='+encodeURIComponent(client));if(wid!==requested || (state && next.revision < state.revision))return;state=next;render();}
async function command(action,body={}){await api('/api/command',{id:wid,action,command_id:crypto.randomUUID(),...body});await refresh();}
async function selectAgent(id){return task(async()=>{selected=id;await command('inspect',{agent_id:id});if(matchMedia('(max-width:700px)').matches){$('agent-inspector').scrollIntoView({block:'start'});$('agent-inspector').focus({preventScroll:true});}});}
function render(){if(!state)return;$('workspace-name').textContent=state.name;$('saved-state').textContent='All changes saved locally';$('run-state').textContent=state.running?'Running':'Paused';$('run-button').textContent=state.running?'Pause':'Resume';$('world-clock').textContent='Tick '+state.tick;$('agent-count').textContent=state.agents.length;$('entry-count').textContent=state.entry_count+' recorded events';world.set(state,selected);renderSimulation();
 const focusedAgent=document.activeElement?.dataset.agentId;
 const roster=$('agents');roster.replaceChildren();for(const a of state.agents){const b=el('button',undefined,'roster-agent');b.dataset.agentId=a.id;b.setAttribute('aria-pressed',a.id===selected);b.setAttribute('aria-label','Inspect '+a.name);const swatch=el('span',undefined,'swatch');swatch.style.background=a.color;const words=el('span');words.append(el('strong',a.name),el('small',a.role||a.behavior));b.append(swatch,words);b.onclick=()=>selectAgent(a.id);roster.append(b);}if(!state.agents.length)roster.append(el('p','No agents yet.','help'));
 if(focusedAgent){const target=[...roster.children].find(b=>b.dataset.agentId===focusedAgent);target?.focus({preventScroll:true});}
 $('inspector-prompt').textContent=state.agents.length?'Select an agent.':'Meet your first agent.';
 $('inspector-hint').textContent=state.agents.length?'Choose a name in Agents to inspect its profile and saved journal.':'Create someone to explore this environment.';
 const a=state.agents.find(a=>a.id===selected);$('empty-inspector').hidden=!!a;$('selected-agent').hidden=!a;if(a){portrait($('portrait'),a.color);$('selected-name').textContent=a.name;$('selected-personality').textContent=a.personality;$('selected-activity').textContent=a.activity;$('selected-behavior').textContent=a.role||a.behavior;$('selected-visits').textContent=a.visits;$('agent-sim-details').textContent=a.role?`Energy ${a.energy}% · ${a.completed_tasks} tasks completed · ${Object.keys(a.knowledge||{}).length} remembered facts`:'';$('role-label').hidden=!a.role;if(a.role && document.activeElement!==$('agent-role'))$('agent-role').value=a.role;$('identity').textContent=`Agent: ${a.id}\nJournal: ${a.journal_id}\nPersona revision: ${a.persona_revision}\nSource: local rule-based policy; human decisions labelled separately`;const journal=$('journal');journal.replaceChildren();for(const e of state.entries.filter(e=>e.author_agent_id===a.id).slice(0,20)){const row=el('div',undefined,'journal-entry');row.append(el('small',`TICK ${e.tick} / ${e.kind.toUpperCase()} / ${e.source} / ${e.actor_name || a.name}`),el('p',e.text));journal.append(row);}}
 const feed=$('activity-feed');feed.replaceChildren();for(const e of state.entries.slice(0,5)){const row=el('div',undefined,'feed-row'),body=el('div'),name=state.agents.find(a=>a.id===e.author_agent_id)?.name||'Agent';body.append(el('strong',name),el('span',e.kind),el('p',e.text));row.append(el('span',undefined,'feed-dot'),body,el('small','T'+e.tick));feed.append(row);}if(!state.entries.length)feed.append(el('p','A quiet place, waiting for its first explorer.','help'));
 renderGuide();if(state.runtime_error)fail(Error(state.runtime_error));
}
function renderGuide(){const p=state.profile;const steps={1:['1 of 4 · Create your first agent','Use + beside Agents. Give your agent a name, personality, and local behavior.'],2:['2 of 4 · Meet your agent','Select the agent in the list or click its sprite to open its profile and journal.'],3:['3 of 4 · Take one step','Press Step to advance the simulation. Your agent chooses a destination and records its intent.'],4:['4 of 4 · Save a checkpoint','Save a checkpoint. You can later restore it as a separate workspace with its history intact.']};$('tutorial').hidden=p.tutorial!=='active';if(steps[p.step]){const [a,b]=steps[p.step];$('tutorial-title').textContent=a;$('tutorial-copy').textContent=b;}}
function agentDialog(edit=false){editing=edit;const f=$('agent-form');f.reset();$('agent-dialog-title').textContent=edit?'Edit agent':'Create agent';$('agent-submit').textContent=edit?'Save changes':'Create agent';$('behavior-label').hidden=edit;if(edit){const a=state.agents.find(a=>a.id===selected);f.elements.name.value=a.name;f.elements.personality.value=a.personality;}else{f.elements.personality.value='Curious, patient, and drawn to small details.';}$('agent-dialog').showModal();f.elements.name.focus();}
$('create-workspace').onclick=()=>$('workspace-dialog').showModal();
$('workspace-form').onsubmit=e=>{e.preventDefault();task(async()=>{const data=await api('/api/create',{name:e.target.elements.name.value});$('workspace-dialog').close();e.target.reset();await open(data.id);});};
$('agent-form').onsubmit=e=>{e.preventDefault();task(async()=>{const f=e.target;await command(editing?'edit_agent':'add_agent',{name:f.elements.name.value,personality:f.elements.personality.value,behavior:f.elements.behavior.value,agent_id:selected});$('agent-dialog').close();toast(editing?'Persona revision saved.':'Agent created. Select it to inspect its journal.');});};
for(const id of ['add-agent','first-agent'])$(id).onclick=()=>agentDialog();$('edit-agent').onclick=()=>agentDialog(true);
$('step-button').onclick=()=>task(()=>command('step'));
$('run-button').onclick=()=>task(async()=>{state=await api('/api/run',{id:wid,running:!state.running});render();});
$('save-button').onclick=()=>task(async()=>{const wasGuide=state.profile.tutorial==='active';await command('checkpoint');toast(wasGuide&&state.profile.tutorial==='complete'?'Introduction complete. Your checkpoint is saved.':'Checkpoint saved.');});
$('checkpoints-button').onclick=()=>{const list=$('checkpoint-list');list.replaceChildren();for(const c of state.checkpoints){const row=el('div',undefined,'checkpoint-row'),b=el('button','Restore as new branch');row.append(el('strong',c.label),el('small',new Date(c.created*1000).toLocaleString()));b.onclick=()=>task(async()=>{const data=await api('/api/restore',{checkpoint_id:c.id});$('checkpoints-dialog').close();await open(data.id);toast('Restored into a new workspace branch.');});row.append(b);list.append(row);}if(!state.checkpoints.length)list.append(el('p','No checkpoints yet. Use Save checkpoint to create one.'));$('checkpoints-dialog').showModal();};
async function home(){if(wid&&state?.controller)await api('/api/run',{id:wid,running:false});wid=null;state=null;location.hash='';$('workspace').hidden=true;$('home').hidden=false;await overview();}
$('back').onclick=()=>task(home);$('home-link').onclick=e=>{e.preventDefault();task(home);};
$('guide-button').onclick=()=>{if(wid)$('guide-dialog').showModal();else toast('Create or open a workspace to start the introduction.');};
$('guide-start').onclick=()=>task(async()=>{await api('/api/tutorial',{status:'active'});$('guide-dialog').close();await refresh();});
for(const id of ['guide-skip','tutorial-skip'])$(id).onclick=()=>task(async()=>{await api('/api/tutorial',{status:'skipped'});$('guide-dialog').close();await refresh();});
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
setInterval(async()=>{if(!wid||busy||polling||document.hidden)return;polling=true;try{await refresh();}catch(error){fail(error);$('saved-state').textContent='Disconnected';}finally{polling=false;}},750);
task(async()=>{await overview();const id=new URLSearchParams(location.hash.slice(1)).get('workspace');if(id)await open(id);});

insights({api,task,current:()=>({state,wid,selected}),refresh});
experiments({api,task,current:()=>({state,wid}),command});
for(const link of document.querySelectorAll('.workspace-nav a'))link.onclick=e=>{e.preventDefault();const target=document.querySelector(link.getAttribute('href'));target.scrollIntoView({block:'start'});target.focus({preventScroll:true});};

function renderSimulation(){
 const sim=state.simulation;
 $('start-simulation').hidden=!!sim;
 $('sim-dashboard').hidden=!sim;
 $('take-control').hidden=state.can_control!==false;
 $('run-button').disabled=state.can_control===false;
 $('sim-speed').disabled=state.can_control===false;
 $('step-button').disabled=state.can_control===false||state.running;
 if(document.activeElement!==$('sim-speed'))$('sim-speed').value=state.speed||1;
 if(!sim)return;
 const day=Math.floor((state.tick-sim.started_tick)/240)+1;
 $('sim-day').textContent=`Day ${day} · ${sim.weather} · ${state.running?'Live':'Paused'}`;
 const values={water:sim.water,timber:sim.wood,food:sim.food,moisture:sim.moisture+'%',growth:sim.growth+'%',pump:sim.pump};
 for(const [key,value]of Object.entries(values))$('metric-'+key).textContent=value;
 $('sim-outcomes').textContent=`${sim.repairs} repairs · ${sim.harvests} harvests · ${sim.exchanges} knowledge exchanges`;
 $('sim-objective').textContent=sim.harvests?'Harvest complete. Keep the field station supplied through the next growing cycle.':sim.pump==='working'?'Pump repaired. Keep the soil watered until the first harvest.':'Repair the pump: the scout finds a guide, shares it, and the engineer uses two timber.';
}
$('start-simulation').onclick=()=>task(async()=>{await command('start_simulation');toast('Living simulation started. Assign roles, then Resume.');});
$('agent-role').onchange=()=>task(()=>command('set_role',{agent_id:selected,role:$('agent-role').value}));
$('intervention-form').onsubmit=e=>{e.preventDefault();task(()=>command('intervene',{intervention:$('intervention').value}));};
$('sim-speed').onchange=()=>task(async()=>{state=await api('/api/run',{id:wid,running:!!state.running,speed:Number($('sim-speed').value)});render();});
$('take-control').onclick=()=>task(async()=>{state=await api('/api/run',{id:wid,running:true,takeover:true,speed:Number($('sim-speed').value)});render();});

$('create-demo').onclick=()=>task(async()=>{
 const data=await api('/api/create',{name:'Living field station'});
 for(const [name,personality]of [['Mira','Curious field scout, attentive to shared discoveries.'],['Sol','Practical engineer who works from recorded instructions.'],['Fern','Patient gardener tending water and food.']]){
  await api('/api/command',{id:data.id,action:'add_agent',command_id:crypto.randomUUID(),name,personality,behavior:'curious'});
 }
 await api('/api/command',{id:data.id,action:'start_simulation',command_id:crypto.randomUUID()});
 await api('/api/tutorial',{status:'skipped'});
 await open(data.id);
 toast('Scout, engineer and gardener are ready. Press Resume to start their day.');
});
