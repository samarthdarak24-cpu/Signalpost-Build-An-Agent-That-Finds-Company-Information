import { readFileSync } from "fs";
const BASE="http://127.0.0.1:8787";
function makeEl(tag="div"){return{tagName:tag,children:[],attrs:{},dataset:{},style:{},_html:"",hidden:false,
 classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},contains(c){return this._s.has(c)}},
 get innerHTML(){return this._html},set innerHTML(v){this._html=String(v)},textContent:"",
 querySelector(s){const id=String(s).match(/^#([\w-]+)$/);const cl=String(s).match(/^\.([\w-]+)$/);
  const re=id?new RegExp(`id="${id[1]}"`):(cl?new RegExp(`class="[^"]*\\b${cl[1]}\\b`):null);
  return (re&&re.test(this._html))?makeEl("div"):null},
 querySelectorAll(s){const o=[];const cl=String(s).match(/^\.([\w-]+)$/);
  if(cl){const n=(this._html.match(new RegExp(`class="[^"]*\\b${cl[1]}\\b`,"g"))||[]).length;for(let i=0;i<n;i++)o.push(makeEl("div"))}return o},
 addEventListener(){},appendChild(c){this.children.push(c);return c},remove(){},setAttribute(){},getAttribute(){return null},
 removeAttribute(){},insertAdjacentHTML(){},scrollIntoView(){},focus(){},blur(){},closest(){return null},contains(){return false},
 lastElementChild:null,offsetWidth:1,value:"",removeChild(){},insertBefore(){},replaceChild(){}}}
const reg=new Map();
const getEl=id=>{if(!reg.has(id))reg.set(id,makeEl(id==="main"?"main":"div"));return reg.get(id)};
const win=globalThis; win.addEventListener=()=>{}; win.removeEventListener=()=>{}; win.__lastFocus=null;
globalThis.window=win;
globalThis.document={documentElement:makeEl("html"),body:makeEl("body"),title:"",
 querySelector:s=>getEl(String(s).replace(/^#/,"")),querySelectorAll:()=>[],createElement:t=>makeEl(t),
 addEventListener:()=>{},activeElement:null};
globalThis.location={hash:"#/overview",href:BASE};
globalThis.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v}};
globalThis.matchMedia=()=>({matches:false,addEventListener(){}});
globalThis.requestAnimationFrame=f=>setTimeout(f,0);
const rf=globalThis.fetch; globalThis.fetch=(p,o)=>rf(String(p).startsWith("http")?p:BASE+p,o);
globalThis.console.error=(...a)=>{throw new Error("console.error: "+a.join(" "))};
const src=readFileSync("web-next/app.js","utf8");
const views=new Function(src+";return {viewOverview,viewSearch,viewCompany,viewCoverage,viewTrace,viewAsk,viewReports,viewSources,viewAuditor,viewLive,state,ensureProfiles};")();
await new Promise(r=>setTimeout(r,900));
const which=process.argv[2]||"overview";
const map={overview:()=>views.viewOverview(),search:()=>views.viewSearch(),
 company:()=>views.viewCompany("923609016","overview"),coverage:()=>views.viewCoverage(),
 trace:()=>views.viewTrace("923609016"),reports:()=>views.viewReports(),
 sources:()=>views.viewSources(),auditor:()=>views.viewAuditor(),live:()=>views.viewLive()};
const html=await map[which]();
// strip tags so we read the human content
const text=html.replace(/<script[\s\S]*?<\/script>/g,"").replace(/<[^>]+>/g," ").replace(/&amp;/g,"&")
  .replace(/&quot;/g,'"').replace(/\s+/g," ").trim();
console.log(text.slice(0,1600));
