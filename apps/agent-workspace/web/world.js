// Pixel scene renderer. State belongs to the server, never to this canvas.
export class World {
 constructor(canvas,onSelect=()=>{}){this.canvas=canvas;this.assets={};for(const name of ['fieldwork-props','pump-flow','water-tank','researcher-south','researcher-north','researcher-east','researcher-west','researcher-walk-south','researcher-walk-east','researcher-walk-north','researcher-walk-west']){const img=new Image();img.src='/assets/fieldwork-v2/'+name+'.png';this.assets[name]=img;}this.ctx=canvas.getContext('2d');this.agents=[];this.positions=new Map();this.selected=null;this.onSelect=onSelect;this.bg=document.createElement('canvas');this.bg.width=320;this.bg.height=200;this.build();canvas.addEventListener('click',e=>{const r=canvas.getBoundingClientRect(),x=(e.clientX-r.left)*320/r.width,y=(e.clientY-r.top)*200/r.height;const a=this.agents.find(a=>Math.hypot(a.x-x,a.y-y)<12);if(a)this.onSelect(a.id);});this.frame=0;this.paint=()=>{if(!document.hidden && this.canvas.getBoundingClientRect().width)this.draw();this.frame=requestAnimationFrame(this.paint);};this.paint();}
 set(state,selected){this.state=state;this.agents=state?.agents||[];this.selected=selected;}
 build(){const c=this.bg.getContext('2d');c.imageSmoothingEnabled=false;const box=(x,y,w,h,color)=>{c.fillStyle=color;c.fillRect(x,y,w,h);};box(0,0,320,200,'#c4d19d');
  // Repeatable ground texture. This PRNG is decoration, not simulation state.
  let n=41;const rand=()=>{n=(n*1664525+1013904223)>>>0;return n/4294967296;};for(let i=0;i<1800;i++){const x=Math.floor(rand()*320),y=Math.floor(rand()*200);box(x,y,1+(i%3===0),1,i%2?'#b8c78d':'#d0dbaa');}
  box(0,0,320,15,'#9caf78');box(0,15,320,2,'#b5c58f');
  // Pond with stepped shore.
  for(const [x,y,w,h]of [[236,157,70,32],[247,151,50,42],[230,166,81,15]])box(x,y,w,h,'#aabb85');
  box(245,159,54,26,'#83b5b0');box(239,166,64,15,'#83b5b0');box(250,155,43,34,'#83b5b0');for(let i=0;i<13;i++)box(247+Math.floor(rand()*48),160+Math.floor(rand()*24),4,1,'#b0d4ca');box(264,168,4,2,'#668f65');box(284,177,5,2,'#668f65');
  // Connected footpaths follow the same route as the controller.
  box(0,121,320,12,'#c8b588');box(0,122,320,8,'#e0cc9f');for(const [x,y,h]of [[65,106,20],[157,59,68],[255,104,22]]){box(x,y,11,h,'#c8b588');box(x+1,y,8,h,'#e0cc9f');}for(let x=5;x<320;x+=13)box(x,125,3,1,'#cbb88d');
  // Garden, fenced and planted in regular rows.
  box(33,58,74,45,'#acbc80');for(let r=0;r<3;r++)for(let j=0;j<6;j++){box(41+j*10,63+r*12,7,8,'#947c55');box(43+j*10,63+r*12,3,5,'#527344');box(42+j*10,66+r*12,5,2,r===1?'#7e9850':'#66844a');if(r===2)box(44+j*10,64+r*12,2,2,'#d99557');}
  for(let x=29;x<=110;x+=9){box(x,98,2,11,'#ede0b9');box(x,97,2,1,'#f6ebcb');}box(29,101,83,2,'#d0bc94');box(29,106,83,2,'#d0bc94');
  const house=(x,y,w,h,roof)=>{box(x+3,y+h,w,4,'#98aa78');box(x,y,w,h,'#eee1b9');box(x+w-7,y,7,h,'#d3bf95');box(x-3,y-5,w+6,8,roof);box(x+1,y-10,w-2,6,roof);box(x+6,y-14,w-12,5,roof);box(x-3,y+2,w+6,2,'#655e4c');for(let xx=x+4;xx<x+w-6;xx+=11){box(xx,y+8,7,10,'#707f73');box(xx+1,y+9,5,6,'#a8c4b5');box(xx+3,y+9,1,7,'#eee1b9');}box(x+w/2-4,y+h-13,8,13,'#857458');box(x+w/2-3,y+h-12,5,11,'#5d6753');box(x+w/2+2,y+h-7,1,1,'#d9c486');};
  house(139,30,46,31,'#75877a');box(144,48,7,3,'#d1ad75');box(177,23,5,10,'#9d967e');
  house(230,69,56,37,'#af7558');box(243,97,21,3,'#807058');box(246,100,2,5,'#807058');box(260,100,2,5,'#807058');box(258,92,2,5,'#d6b976');box(240,92,6,4,'#92928a');box(277,88,4,9,'#b4a07a');
  // Trees: layered silhouettes and small highlights in pixel units.
  const tree=(x,y,size=1)=>{box(x-1,y,3,10,'#867653');box(x-7,y-13,15,18,'#608153');box(x-10,y-8,21,11,'#608153');box(x-5,y-17,10,5,'#6e8b57');box(x-7,y-10,15,10,'#77945c');box(x-4,y-14,8,9,'#88a16a');box(x-7,y-2,5,3,'#53764c');box(x+3,y-3,5,5,'#53764c');};
  for(const [x,y]of [[17,30],[42,26],[74,28],[103,27],[204,30],[231,26],[260,29],[295,29],[14,65],[15,99],[307,76],[303,112],[18,167],[42,182],[92,178],[127,184],[200,181],[213,161]])tree(x,y);
  box(143,147,37,4,'#968061');box(145,151,3,5,'#77684f');box(173,151,3,5,'#77684f');box(143,143,37,3,'#b09a75');box(149,147,2,3,'#c5ae82');
  // Signposts at each actual destination.
  for(const [x,y]of [[82,112],[174,78],[276,119]]){box(x,y-6,1,7,'#806e50');box(x-4,y-8,9,4,'#e8d8ae');}
  c.font='5px monospace';c.textAlign='center';c.fillStyle='#455839';c.fillText('GARDEN',70,52);c.fillText('LIBRARY',162,13);c.fillText('WORKSHOP',258,50);
 }
 drawSimulation(c){
  const sim=this.state?.simulation;if(!sim||sim.art_version!=='fieldwork-v2')return;
  const sheet=this.assets['fieldwork-props'];
  if(sheet.complete&&sheet.naturalWidth){
   c.fillStyle=sim.moisture<15?'#b39a6c':'#8a7455';c.fillRect(36,59,70,38);
   const stage=sim.moisture<15?3:sim.growth>=100?2:sim.growth>35?1:0;
   for(let row=0;row<2;row++)for(let col=0;col<4;col++)c.drawImage(sheet,stage*32,0,32,32,38+col*16,58+row*17,16,16);
   c.drawImage(sheet,32,32,32,32,230,103,16,16);
   if(sim.food>4)c.drawImage(sheet,64,32,32,32,92,105,16,16);
  }
  const tank=this.assets['water-tank'];if(tank.complete&&tank.naturalWidth)c.drawImage(tank,283,135,30,30);
  const flow=this.assets['pump-flow'];if(sim.pump==='working'&&flow.complete&&flow.naturalWidth){const frame=this.state.running?Math.floor(performance.now()/180)%4:this.state.tick%4;c.drawImage(flow,frame*32,0,32,32,261,143,24,24);}
  if(sim.pump==='broken'){c.fillStyle='#b8674b';c.fillRect(270,142,7,7);c.fillStyle='#fff0d3';c.font='7px monospace';c.fillText('!',274,148);}
  const phase=(this.state.tick%240)/240;
  if(phase>.68){c.fillStyle='rgba(30,43,70,.19)';c.fillRect(0,0,320,200);}
  if(sim.weather==='rain'){c.fillStyle='#98c5c2';for(let i=0;i<55;i++){const x=(i*53+this.state.tick*3)%320,y=(i*29+this.state.tick*5)%200;c.fillRect(x,y,1,4);}}
 }

 draw(){const c=this.ctx;c.imageSmoothingEnabled=false;c.setTransform(2,0,0,2,0,0);c.clearRect(0,0,320,200);c.drawImage(this.bg,0,0);this.drawSimulation(c);for(const a of [...this.agents].sort((a,b)=>a.y-b.y)){let p=this.positions.get(a.id);if(!p){p={x:a.x,y:a.y};this.positions.set(a.id,p);}const moving=Math.hypot(a.x-p.x,a.y-p.y)>.4;p.x+=(a.x-p.x)*.15;p.y+=(a.y-p.y)*.15;const x=Math.round(p.x),y=Math.round(p.y);if(a.id===this.selected){c.fillStyle='#f7f1cc';c.fillRect(x-7,y+1,15,3);c.fillStyle='#658454';c.fillRect(x-7,y+3,15,1);}const art=this.assets['researcher-'+(a.facing||'south')];if(this.state?.simulation?.art_version==='fieldwork-v2'&&art?.complete&&art.naturalWidth){const walk=this.assets['researcher-walk-'+(a.facing||'south')];if(moving&&walk?.complete&&walk.naturalWidth){const size=walk.naturalHeight,frame=Math.floor(performance.now()/130)%4;c.drawImage(walk,frame*size,0,size,size,x-12,y-23,24,24);}else c.drawImage(art,x-12,y-23,24,24);c.fillStyle=a.color;c.fillRect(x-3,y+2,6,2);}else sprite(c,x,y,a.color,moving?Math.floor(performance.now()/180)%2:0);if(a.working){c.fillStyle='#faf6e8';c.fillRect(x-8,y-29,16,3);c.fillStyle='#6b9861';c.fillRect(x-8,y-29,Math.min(16,a.working),3);}c.font='5px monospace';const width=c.measureText(a.name).width;c.fillStyle='#faf6e8';c.fillRect(x-width/2-3,y-22,width+6,8);c.fillStyle='#354a37';c.textAlign='center';c.fillText(a.name,x,y-16);}}
}
export function sprite(c,x,y,color,step=0){const box=(dx,dy,w,h,col)=>{c.fillStyle=col;c.fillRect(x+dx,y+dy,w,h);};box(-4,1,9,2,'#829765');box(-3,-13,7,7,'#d8b78d');box(-4,-14,8,3,'#514a3d');box(-4,-11,2,3,'#514a3d');box(2,-10,1,1,'#414537');box(-4,-6,9,7,color);box(-6,-5,2,5,'#d8b78d');box(5,-5,2,5,'#d8b78d');box(-3,1,3,3-step,'#535b50');box(2,1,3,2+step,'#535b50');}
export function portrait(canvas,color){const c=canvas.getContext('2d');c.clearRect(0,0,80,80);c.setTransform(4,0,0,4,0,0);sprite(c,10,16,color);c.setTransform(1,0,0,1,0,0);}
